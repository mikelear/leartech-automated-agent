"""Authed remote-MCP connections for the agent.

The agent's *other* "MCP servers" (pipeline, tekton, criteria, artifacts) are
in-process SDK shims (`create_sdk_mcp_server`) that shell out to `kubectl`/`gh`
locally — they are not network MCP clients. This module is the agent's FIRST
real remote-MCP client: it registers external MCP servers (Streamable-HTTP)
reachable over the network, authenticated with an ``aud=leartech-mcp`` bearer
token minted via the OAuth2 ``client_credentials`` grant.

It mirrors the controller's plan-6 wiring (`authedHTTPClientForAudience(
"leartech-mcp")` in leartech-orchestrator-controller) so the agent and the
controller are the *same kind* of internal MCP client — one audience, one
internal FQDN, one token flow. Adding another remote MCP later is a single
line in ``REMOTE_MCPS`` (this is the general mechanism, not an open_pr one-off).

Config — all from env, injected into the agent Job by the controller's
``jobspawn`` (which projects the same secret the controller uses):

  LEARTECH_MCP_URL             internal MCP host base, e.g.
                               http://leartech-mcp-servers.jx-staging.svc.cluster.local
  LEARTECH_AUTH_TOKEN_URL      Hydra token endpoint (…/oauth2/token)
  LEARTECH_AUTH_CLIENT_ID      client_credentials client id
  LEARTECH_AUTH_CLIENT_SECRET  client secret (projected from a K8s Secret)
  LEARTECH_AUTH_SCOPE          OAuth scope (default leartechapi.internal_services)

``MCP_AUDIENCE`` is fixed to ``leartech-mcp`` — the internal MCP enforces it.

Graceful degradation: when the config is absent (laptop / preview / not-yet-
wired), ``build_remote_mcp_servers()`` returns ``{}`` and logs a single warning
— the agent runs with no remote MCPs, exactly like the controller degrades to
``NoopPROutcomeFetcher``, rather than crashing. The agent's system prompt then
finds ``open_pr`` unavailable and halts cleanly (never falling back to
``gh pr create``).
"""

from __future__ import annotations

import logging
import os
import sys

import httpx
from claude_agent_sdk.types import McpStdioServerConfig

log = logging.getLogger(__name__)

# The audience the internal MCP host enforces on bearer tokens. Fixed — matches
# leartech-orchestrator-controller's controller.MCPAudience.
MCP_AUDIENCE = 'leartech-mcp'

DEFAULT_SCOPE = 'leartechapi.internal_services'
_TOKEN_TIMEOUT = 15.0

# The remote MCP servers this agent role WANTS, by the SERVER name the host
# advertises on ``/mcps`` (ground truth, e.g. ``pr_context`` with an underscore).
# This is role scoping only — it declares intent, NOT paths/URLs. The host is the
# single source of truth for HOW to reach each server: build_remote_mcp_servers
# discovers the live ``mounts`` ({name, path}) from ``/mcps`` and wires each
# wanted server at the host's advertised path VERBATIM. The agent never
# constructs or guesses a URL, so a path/name drift between this repo and the
# deployed host can no longer silently 404 the agent (the bug that stranded
# open_pr). Add a server to this set to consume it; nothing else.
#
#   * pr_context  — open_pr (+ get_pr_metadata/diff, list_changed_files).
#   * tekton      — step-aware PipelineRun inspection (6 tools); the former
#     in-process shim is gone. classify_step_failure + rebase_branch_on_base
#     stay in-process under `leartech-agent-local` (LLM diagnosis + workspace git).
#   * jx3_flow    — aggregate PR-check status (list_pr_checks, wait_for_terminal,
#     wait_for_first_failure_or_all_pass); replaces the old pipeline_server shim.
WANTED_MCP_SERVERS: frozenset[str] = frozenset({'pr_context', 'tekton', 'jx3_flow'})

_DISCOVERY_TIMEOUT = 15.0


def _agent_mcp_name(server_name: str) -> str:
    """Agent-facing MCP name for a host server name — ``pr_context`` ->
    ``leartech-pr-context``. Deterministic (not config), so the LLM tool names
    (``mcp__leartech-pr-context__open_pr``) + MCP_ALLOWED_TOOLS stay stable."""
    return 'leartech-' + server_name.replace('_', '-')


def discover_mounts(base: str, token: str) -> dict[str, str] | None:
    """GET ``<base>/mcps`` and return ``{server-name: mount-path}`` from the
    host's authoritative ``mounts`` array — the paths used VERBATIM (no guessing).

    Payload: ``{"servers":[...], "mounts":[{"name":"pr_context","path":"/mcp/pr_context"},...]}``.
    Transitional fallback: an older host that only returns ``servers`` (no
    ``mounts``) yields ``{name: "/mcp/"+name}`` with a warning — removed once
    every host publishes ``mounts``. Returns ``None`` on any failure so the
    caller degrades rather than wiring blind.
    """
    try:
        resp = httpx.get(
            f'{base}/mcps',
            headers={'Authorization': f'Bearer {token}'},
            timeout=_DISCOVERY_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.warning('remote-MCP discovery (/mcps) failed (transport): %s', exc)
        return None
    if resp.status_code != 200:
        log.warning('remote-MCP discovery (/mcps) failed: HTTP %s', resp.status_code)
        return None
    payload = resp.json()
    mounts = payload.get('mounts')
    if isinstance(mounts, list) and mounts:
        out: dict[str, str] = {}
        for m in mounts:
            name, path = m.get('name'), m.get('path')
            if isinstance(name, str) and isinstance(path, str) and path:
                out[name] = path
        if out:
            return out
    # Transitional: host predates the `mounts` field (only `servers`). Derive
    # the conventional path so the agent keeps working until every host ships
    # `mounts`; warn so we notice + can drop this branch.
    servers = payload.get('servers')
    if isinstance(servers, list) and servers:
        log.warning(
            'remote-MCP host /mcps has no "mounts" (path source of truth) — '
            'falling back to /mcp/<name> convention for %d server(s). Update the '
            'host to publish mounts.',
            len(servers),
        )
        return {str(s): f'/mcp/{s}' for s in servers}
    log.warning('remote-MCP discovery (/mcps) returned neither mounts nor servers')
    return None


def mint_mcp_token() -> str | None:
    """Mint an ``aud=leartech-mcp`` bearer token via client_credentials.

    Returns the access token, or ``None`` when the auth env is unset or the
    grant fails (caller degrades to "no remote MCPs" rather than crashing).
    """
    token_url = os.environ.get('LEARTECH_AUTH_TOKEN_URL')
    client_id = os.environ.get('LEARTECH_AUTH_CLIENT_ID')
    client_secret = os.environ.get('LEARTECH_AUTH_CLIENT_SECRET')
    scope = os.environ.get('LEARTECH_AUTH_SCOPE', DEFAULT_SCOPE)
    if not (token_url and client_id and client_secret):
        return None
    # Deliberately SYNCHRONOUS: this runs exactly once during agent startup —
    # inside the synchronous ClaudeAgentOptions construction, BEFORE the async
    # `query()` message loop begins. No other coroutines are scheduled yet, so
    # the blocking grant does not stall concurrent work. Making it async would
    # force the whole options-construction path (sync by SDK contract) to
    # become async for no runtime benefit.
    try:
        resp = httpx.post(
            token_url,
            data={
                'grant_type': 'client_credentials',
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': scope,
                # RFC 8707 resource-audience binding — Hydra stamps aud on the
                # access token so the internal MCP's aud gate accepts it.
                'audience': MCP_AUDIENCE,
            },
            timeout=_TOKEN_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.warning('remote-MCP token mint failed (transport): %s', exc)
        return None
    if resp.status_code != 200:
        # Never log the body — it can echo the request; log status only.
        log.warning('remote-MCP token mint failed: HTTP %s', resp.status_code)
        return None
    token = resp.json().get('access_token')
    if not isinstance(token, str) or not token:
        log.warning('remote-MCP token mint returned no usable access_token')
        return None
    return token


def build_remote_mcp_servers() -> dict[str, McpStdioServerConfig]:
    """Build authed MCP server configs for the SDK — DISCOVERED, not hardcoded.

    Phase 2: each wanted remote MCP is wired as a ``McpStdioServerConfig`` that
    spawns the in-repo ``gate.mcp_servers.stdio_bridge`` — a stdio↔authed-HTTP
    proxy that WE own. The Claude Code CLI (spawned by claude-agent-sdk) does not
    forward a static ``Authorization`` header for ``type: http`` MCPs, so we no
    longer hand it the http URL; instead it speaks plain stdio to the bridge,
    which holds the authenticated streamable-HTTP connection to the deployed
    server. Fixes the open_pr 401→OAuth-404 failure and decouples tools from the
    Anthropic runtime (portability). The bearer + downstream URL are passed to
    the bridge via env (not argv, so they don't show in ``ps``).

    Flow: mint an aud=leartech-mcp token → DISCOVER the live server set from the
    host's ``/mcps`` → wire each wanted MCP (``REMOTE_MCPS``) at
    ``<base>/mcp/<server-name>`` using the host's own server name, but ONLY if the
    host advertises it. A wanted-but-absent MCP is logged LOUDLY and skipped (so
    e.g. open_pr being unavailable is diagnosed here at wiring, not as a mystery
    404 at call time — the bug that stranded the PR-capture flow).

    Returns ``{agent-name: McpStdioServerConfig}``; ``{}`` (with a warning) when
    unconfigured, so callers can splat it into ``ClaudeAgentOptions`` uncondition-
    ally. ``McpStdioServerConfig`` is the SDK's own TypedDict (no bare ``Any``).
    """
    base = os.environ.get('LEARTECH_MCP_URL', '').rstrip('/')
    if not base:
        log.warning(
            'LEARTECH_MCP_URL unset — agent runs with NO remote MCPs '
            '(open_pr et al. unavailable; agent will halt at PR-open rather '
            'than fall back to gh pr create).'
        )
        return {}
    token = mint_mcp_token()
    if not token:
        log.warning(
            'LEARTECH_MCP_URL is set but no aud=%s token could be minted — '
            'remote MCPs disabled. Check LEARTECH_AUTH_* env / client secret.',
            MCP_AUDIENCE,
        )
        return {}

    mounts = discover_mounts(base, token)
    if mounts is None:
        # Discovery itself failed (host unreachable / auth). Rather than wire
        # blind against an unverified host, degrade to no remote MCPs — the
        # agent halts cleanly at PR-open instead of racking up 404s.
        log.warning(
            'remote-MCP discovery failed against %s — wiring NO remote MCPs '
            '(cannot obtain server paths; agent will halt at open_pr).',
            base,
        )
        return {}

    servers: dict[str, McpStdioServerConfig] = {}
    missing: list[str] = []
    for server_name in sorted(WANTED_MCP_SERVERS):
        path = mounts.get(server_name)
        if not path:
            missing.append(server_name)
            continue
        # Host path VERBATIM — the agent never constructs the URL beyond joining
        # its configured base with the host-advertised path. The URL + bearer go
        # to the stdio bridge via env; the bridge (not the CLI) makes the authed
        # streamable-HTTP call, so the header is actually sent.
        servers[_agent_mcp_name(server_name)] = McpStdioServerConfig(
            type='stdio',
            command=sys.executable,
            args=['-m', 'gate.mcp_servers.stdio_bridge'],
            env={
                'LEARTECH_MCP_BRIDGE_URL': f'{base}{path}',
                # Pass the auth CONFIG, NOT a static token: the aud=leartech-mcp
                # token is short-lived (~300s), so a token minted here at agent
                # startup expires long before a multi-minute run reaches open_pr.
                # The bridge mints a FRESH token per call from these. (The token
                # minted above is only for the one-shot /mcps discovery.)
                'LEARTECH_AUTH_TOKEN_URL': os.environ.get('LEARTECH_AUTH_TOKEN_URL', ''),
                'LEARTECH_AUTH_CLIENT_ID': os.environ.get('LEARTECH_AUTH_CLIENT_ID', ''),
                'LEARTECH_AUTH_CLIENT_SECRET': os.environ.get('LEARTECH_AUTH_CLIENT_SECRET', ''),
                'LEARTECH_AUTH_SCOPE': os.environ.get('LEARTECH_AUTH_SCOPE', DEFAULT_SCOPE),
            },
        )
    if missing:
        log.warning(
            'wanted remote MCP(s) NOT advertised by %s/mcps: %s — skipped. '
            'Host advertises: %s',
            base,
            ', '.join(missing),
            ', '.join(sorted(mounts)),
        )
    log.info(
        'wired %d/%d remote MCP(s) from /mcps discovery: %s',
        len(servers),
        len(WANTED_MCP_SERVERS),
        ', '.join(sorted(servers)) or '(none)',
    )
    return servers
