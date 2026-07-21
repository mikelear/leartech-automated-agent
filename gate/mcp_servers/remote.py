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
from typing import Any

import httpx

log = logging.getLogger(__name__)

# The audience the internal MCP host enforces on bearer tokens. Fixed — matches
# leartech-orchestrator-controller's controller.MCPAudience.
MCP_AUDIENCE = 'leartech-mcp'

DEFAULT_SCOPE = 'leartechapi.internal_services'
_TOKEN_TIMEOUT = 15.0

# Registry of remote MCP servers the agent connects to, name -> path on the
# internal MCP host. Each becomes an authed Streamable-HTTP MCP server the LLM
# calls natively (tools surface as ``mcp__<name>__<tool>``). Add a line here to
# wire another remote MCP — no other code changes.
REMOTE_MCPS: dict[str, str] = {
    'leartech-pr-context': '/mcp/pr_context',
}


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
    if not token:
        log.warning('remote-MCP token mint returned no access_token')
        return None
    return token


def build_remote_mcp_servers() -> dict[str, Any]:
    """Build authed Streamable-HTTP MCP server configs for the SDK.

    Returns ``{name: McpHttpServerConfig}`` for each entry in ``REMOTE_MCPS``,
    or ``{}`` (with a single warning) when unconfigured — so callers can splat
    it into ``ClaudeAgentOptions(mcp_servers={...})`` unconditionally.
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
    headers = {'Authorization': f'Bearer {token}'}
    servers: dict[str, Any] = {
        name: {'type': 'http', 'url': f'{base}{path}', 'headers': dict(headers)}
        for name, path in REMOTE_MCPS.items()
    }
    log.info('wired %d remote MCP(s): %s', len(servers), ', '.join(sorted(servers)))
    return servers
