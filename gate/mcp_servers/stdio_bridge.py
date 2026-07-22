"""Authed stdio↔streamable-HTTP MCP bridge (Phase 2 of the ai-gateway migration).

WHY THIS EXISTS
---------------
The agent runs via ``claude-agent-sdk``, which spawns the Claude Code CLI as a
subprocess and hands it the MCP server configs. For a ``type: http`` MCP the CLI
is the http client — and it does NOT forward the static ``Authorization: Bearer``
header we set on ``McpHttpServerConfig``. Against our authed internal MCP host
that means 401 → the CLI's OAuth-discovery fallback → ``/.well-known`` 404 →
``open_pr`` (and every remote tool) unreachable. See memory
``project_mcp_discovery_source_of_truth``.

The fix is to stop delegating http-MCP auth to the opaque CLI and OWN the MCP
client ourselves (also the portability direction — tools via standard client-side
MCP, decoupled from the Anthropic runtime; see ``AI-GATEWAY-AND-PORTABILITY.md``).
This module is that client, packaged as a ``type: stdio`` MCP the CLI spawns:

    CLI  ──stdio (no auth needed, local subprocess)──▶  this bridge
                                                          │
                          authed streamable-HTTP (Bearer) ▼
                                            deployed /mcp/<server> (leartech-mcp-servers)

CONNECTION MODEL — fresh per call (learned the hard way, 2026-07-22)
-------------------------------------------------------------------
An agent typically does many minutes of *local* work (edits, tests) before it
calls a *remote* tool like ``open_pr``. A downstream streamable-HTTP connection
opened once at startup and held idle across that gap gets closed by the server /
LB, and a later ``call_tool`` on the dead connection AWAITS FOREVER — hanging the
whole agent run (observed: 15+ min stuck on open_pr, no result). So the bridge
holds NO long-lived downstream session: it opens a FRESH connection per operation
(the mcp host is ``Stateless: true``, so each request is independent anyway) and
wraps every downstream op in a TIMEOUT. A dead/slow downstream therefore becomes a
clean ``isError`` tool result — the agent's retry→FAIL hardening fires instead of
the run hanging. The bridge is otherwise transparent: it forwards ``tools/list``
and returns the downstream ``CallToolResult`` verbatim (content + isError +
structuredContent), so tool names + behaviour match talking to the server directly.

CONFIG (env, injected by ``build_remote_mcp_servers`` in ``remote.py``):
  LEARTECH_MCP_BRIDGE_URL      full downstream URL, e.g.
                               http://leartech-mcp-servers.jx-staging.svc.cluster.local/mcp/pr_context
  LEARTECH_MCP_BRIDGE_TOKEN    aud=leartech-mcp bearer (minted at agent startup)
  LEARTECH_MCP_BRIDGE_TIMEOUT  per-call downstream timeout seconds (default 180)

One bridge process proxies ONE downstream server (so the CLI namespaces its tools
under that server's name); the agent wires one ``McpStdioServerConfig`` per wanted
remote MCP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Awaitable, Callable

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger('leartech.mcp_bridge')

_BRIDGE_NAME = 'leartech-mcp-bridge'
# open_pr does git + GitHub API work; generous but BOUNDED so a hung downstream
# surfaces as a tool error rather than an indefinite agent hang.
_DEFAULT_CALL_TIMEOUT = 180.0

CallToolFn = Callable[[str, dict[str, object]], Awaitable[mcp_types.CallToolResult]]


def build_bridge_server(tools: list[mcp_types.Tool], call_tool_fn: CallToolFn) -> Server:
    """Build the upstream stdio ``Server``. ``tools`` are advertised on
    ``tools/list``; each ``tools/call`` is delegated to ``call_tool_fn``.

    Factored so the proxy delegation can be unit-tested with a fake ``call_tool_fn``
    (no real transports).
    """
    server: Server = Server(_BRIDGE_NAME)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return tools

    # validate_input=False: the downstream server is the single schema authority.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        return await call_tool_fn(name, arguments)

    return server


async def _with_downstream(url: str, headers: dict[str, str], timeout: float, op):  # type: ignore[no-untyped-def]
    """Open a FRESH authed streamable-HTTP session, run ``op(session)``, close it.

    Bounded by ``timeout`` so a dead/idle-closed connection or a slow server can
    never hang forever. Fresh-per-op is safe: the mcp host is Stateless.
    """
    async with asyncio.timeout(timeout):
        async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _get_session_id):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                return await op(session)


def _make_caller(url: str, headers: dict[str, str], timeout: float) -> CallToolFn:
    """Production ``call_tool_fn``: fresh connection + timeout per call; any failure
    (timeout, transport, downstream error) is returned as an ``isError`` result so
    the CLI/agent sees a clean tool failure instead of a hang or a crashed stdio loop."""

    async def _call(name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        try:
            return await _with_downstream(url, headers, timeout, lambda s: s.call_tool(name, arguments))
        except Exception as exc:  # noqa: BLE001 — surface as a tool error, never hang/crash the loop
            log.warning('downstream MCP call %r failed: %s', name, exc)
            return mcp_types.CallToolResult(
                content=[mcp_types.TextContent(type='text', text=f'bridge: downstream MCP call {name!r} failed: {exc}')],
                isError=True,
            )

    return _call


async def _run() -> int:
    url = os.environ.get('LEARTECH_MCP_BRIDGE_URL', '').strip()
    if not url:
        log.error('LEARTECH_MCP_BRIDGE_URL unset — bridge cannot start')
        return 2
    token = os.environ.get('LEARTECH_MCP_BRIDGE_TOKEN', '').strip()
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    timeout = float(os.environ.get('LEARTECH_MCP_BRIDGE_TIMEOUT', _DEFAULT_CALL_TIMEOUT))

    # Startup: fetch the tool list with a fresh short-lived connection so the CLI
    # can discover tools. No session is held past this point — call_tool reconnects.
    list_result = await _with_downstream(url, headers, timeout, lambda s: s.list_tools())
    tools = list_result.tools
    log.info('bridge ready for %s — proxying %d tool(s) with per-call connections', url, len(tools))

    server = build_bridge_server(tools, _make_caller(url, headers, timeout))
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
        log.error('bridge failed: %s', exc)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
