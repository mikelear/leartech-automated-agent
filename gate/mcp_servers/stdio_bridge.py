"""Authed stdio↔streamable-HTTP MCP bridge (Phase 2 of the ai-gateway migration).

WHY THIS EXISTS
---------------
The agent runs via ``claude-agent-sdk``, which spawns the Claude Code CLI as a
subprocess and hands it the MCP server configs. For a ``type: http`` MCP the CLI
is the http client — and it does NOT forward the static ``Authorization: Bearer``
header we set on ``McpHttpServerConfig`` → 401 → CLI OAuth-discovery fallback →
``/.well-known`` 404 → ``open_pr`` unreachable. So we OWN the MCP client instead
(also the portability direction — standard client-side MCP, decoupled from the
Anthropic runtime). This module is that client, packaged as a ``type: stdio`` MCP
the CLI spawns:

    CLI  ──stdio (local, no auth)──▶  this bridge ──authed streamable-HTTP──▶  /mcp/<server>

FRESH TOKEN + FRESH CONNECTION PER CALL (both learned the hard way, 2026-07-22)
------------------------------------------------------------------------------
1. TOKEN: the aud=leartech-mcp bearer is short-lived (~300s / 5 min). A token
   minted once at agent startup EXPIRES long before a multi-minute run reaches
   open_pr → server "token is expired" → the call fails. So the bridge does NOT
   receive a static token; it holds the auth CONFIG and mints a FRESH token for
   every operation.
2. CONNECTION: an idle downstream streamable-HTTP session opened once gets closed
   by the server/LB, and a later call_tool on it AWAITS FOREVER (a 15-min hang was
   observed). So the bridge holds NO long-lived session either — fresh connect per
   op (the mcp host is Stateless, so each request is independent anyway), inside a
   TIMEOUT. A dead/slow/expired downstream therefore becomes a clean ``isError``
   tool result — the agent's retry→FAIL hardening fires instead of a hang or a
   silent success.

Otherwise transparent: forwards ``tools/list`` + returns the downstream
``CallToolResult`` verbatim (content + isError + structuredContent).

CONFIG (env, injected by ``build_remote_mcp_servers`` in ``remote.py``):
  LEARTECH_MCP_BRIDGE_URL       full downstream URL (…/mcp/<server>)
  LEARTECH_MCP_BRIDGE_TIMEOUT   per-op downstream timeout seconds (default 180)
  LEARTECH_AUTH_TOKEN_URL / _CLIENT_ID / _CLIENT_SECRET / _SCOPE
                                client_credentials creds — the bridge mints an
                                aud=leartech-mcp token from these PER OP.

Kept self-contained (own minimal token mint via httpx — no claude_agent_sdk or
gate.agent imports) so the bridge stays provider-neutral.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable

import httpx
import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger('leartech.mcp_bridge')

_BRIDGE_NAME = 'leartech-mcp-bridge'
# The audience the internal MCP host enforces (matches remote.py / the controller).
_MCP_AUDIENCE = 'leartech-mcp'
_DEFAULT_SCOPE = 'leartechapi.internal_services'
_TOKEN_TIMEOUT = 15.0
# open_pr does git + GitHub API work; generous but BOUNDED so a hung downstream
# surfaces as a tool error rather than an indefinite agent hang.
_DEFAULT_CALL_TIMEOUT = 180.0

CallToolFn = Callable[[str, dict[str, object]], Awaitable[mcp_types.CallToolResult]]


def _mint_token() -> str | None:
    """Mint a FRESH aud=leartech-mcp bearer via client_credentials. Called per op
    because tokens are short-lived (~300s). Minimal duplicate of remote.py's mint
    so the bridge stays decoupled. Returns None on any failure."""
    token_url = os.environ.get('LEARTECH_AUTH_TOKEN_URL')
    client_id = os.environ.get('LEARTECH_AUTH_CLIENT_ID')
    client_secret = os.environ.get('LEARTECH_AUTH_CLIENT_SECRET')
    scope = os.environ.get('LEARTECH_AUTH_SCOPE', _DEFAULT_SCOPE)
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
                'audience': _MCP_AUDIENCE,  # RFC 8707 — Hydra stamps aud on the token
            },
            timeout=_TOKEN_TIMEOUT,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    token = resp.json().get('access_token')
    return token if isinstance(token, str) and token else None


def build_bridge_server(tools: list[mcp_types.Tool], call_tool_fn: CallToolFn) -> Server:
    """Build the upstream stdio ``Server``. ``tools`` are advertised on
    ``tools/list``; each ``tools/call`` is delegated to ``call_tool_fn`` (factored
    so delegation is unit-testable with a fake caller — no real transports)."""
    server: Server = Server(_BRIDGE_NAME)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return tools

    # validate_input=False: the downstream server is the single schema authority.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        return await call_tool_fn(name, arguments)

    return server


async def _with_downstream(url: str, timeout: float, op):  # type: ignore[no-untyped-def]
    """Mint a FRESH token, open a FRESH authed streamable-HTTP session, run
    ``op(session)``, close it — all bounded by ``timeout``. Fresh-per-op is safe
    (Stateless host) and dodges both token expiry and stale-connection hangs."""
    token = await asyncio.to_thread(_mint_token)
    if not token:
        raise RuntimeError('could not mint aud=leartech-mcp token (check LEARTECH_AUTH_* env)')
    headers = {'Authorization': f'Bearer {token}'}
    async with asyncio.timeout(timeout):
        async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)


def _make_caller(url: str, timeout: float) -> CallToolFn:
    """Production ``call_tool_fn``: fresh token + fresh connection + timeout per
    call; any failure is returned as an ``isError`` result so the CLI/agent sees a
    clean tool failure instead of a hang or a crashed stdio loop."""

    async def _call(name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        try:
            return await _with_downstream(url, timeout, lambda s: s.call_tool(name, arguments))
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, never hang/crash the loop
            detail = _flatten(exc)
            log.warning('downstream MCP call %r failed: %s', name, detail)
            return mcp_types.CallToolResult(
                content=[
                    mcp_types.TextContent(type='text', text=f'bridge: downstream MCP call {name!r} failed: {detail}')
                ],
                isError=True,
            )

    return _call


def _flatten(exc: BaseException) -> str:
    """Flatten an ExceptionGroup ('unhandled errors in a TaskGroup') to its real
    inner messages so failures are diagnosable, not opaque."""
    subs = getattr(exc, 'exceptions', None)
    if subs:
        return f'{type(exc).__name__}: [' + '; '.join(_flatten(s) for s in subs) + ']'
    return f'{type(exc).__name__}: {exc}'


async def _run() -> int:
    url = os.environ.get('LEARTECH_MCP_BRIDGE_URL', '').strip()
    if not url:
        log.error('LEARTECH_MCP_BRIDGE_URL unset — bridge cannot start')
        return 2
    timeout = float(os.environ.get('LEARTECH_MCP_BRIDGE_TIMEOUT', _DEFAULT_CALL_TIMEOUT))

    # Startup: fetch the tool list (fresh token + connection) so the CLI can
    # discover tools. No token/session is held past this point.
    list_result = await _with_downstream(url, timeout, lambda s: s.list_tools())
    tools = list_result.tools
    log.info('bridge ready for %s — proxying %d tool(s) (fresh token + connection per call)', url, len(tools))

    server = build_bridge_server(tools, _make_caller(url, timeout))
    async with stdio_server() as (stdio_read, stdio_write):
        await server.run(stdio_read, stdio_write, server.create_initialization_options())
    return 0


def main() -> None:
    logging.basicConfig(level=os.environ.get('LEARTECH_MCP_BRIDGE_LOG', 'INFO'), stream=sys.stderr)
    try:
        raise SystemExit(asyncio.run(_run()))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — top-level guard: log + non-zero exit
        log.error('bridge failed: %s', _flatten(exc))
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
