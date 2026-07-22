"""Authed stdio↔streamable-HTTP MCP bridge (Phase 2 of the ai-gateway migration).

WHY THIS EXISTS
---------------
The agent runs via ``claude-agent-sdk``, which spawns the Claude Code CLI as a
subprocess and hands it the MCP server configs. For a ``type: http`` MCP the CLI
is the http client — and it does NOT forward the static ``Authorization: Bearer``
header we set on ``McpHttpServerConfig``. Against our authed internal MCP host
that means: request → 401 → the CLI's OAuth-discovery fallback → ``/.well-known``
404 → ``open_pr`` (and every remote tool) unreachable. Proven root cause; see
memory ``project_mcp_discovery_source_of_truth``.

The fix is to stop delegating http-MCP auth to the opaque CLI and OWN the MCP
client ourselves — the portability direction too (tools via standard client-side
MCP, decoupled from the Anthropic runtime; see ``AI-GATEWAY-AND-PORTABILITY.md``).
This module is that client, packaged as a ``type: stdio`` MCP the CLI spawns:

    CLI  ──stdio (no auth needed, local subprocess)──▶  this bridge
                                                          │
                          authed streamable-HTTP (Bearer) ▼
                                            deployed /mcp/<server> (leartech-mcp-servers)

The CLI speaks plain MCP-over-stdio to us; we hold the authenticated
streamable-HTTP connection to the deployed server. No CLI http-auth, no OAuth
fallback, no 404. It is the standard ``mcp-remote`` pattern, in Python (the agent
image is Python — no need to add node), on the standard ``mcp`` library.

The bridge is transparent: it forwards ``tools/list`` and returns the downstream
``CallToolResult`` verbatim (preserving ``isError`` + ``structuredContent``), so
tool names (``mcp__leartech-pr-context__open_pr`` etc.) and behaviour are
identical to talking to the server directly.

CONFIG (env, injected by ``build_remote_mcp_servers`` in ``remote.py`` via the
``McpStdioServerConfig.env``):

  LEARTECH_MCP_BRIDGE_URL     full downstream URL, e.g.
                              http://leartech-mcp-servers.jx-staging.svc.cluster.local/mcp/pr_context
  LEARTECH_MCP_BRIDGE_TOKEN   aud=leartech-mcp bearer (minted once at agent
                              startup; same lifetime semantics as the old static
                              header). Optional — absent = unauthenticated (local
                              dev against an unauthed host).

One bridge process proxies ONE downstream server (so the CLI namespaces its
tools under that server's name); the agent wires one ``McpStdioServerConfig`` per
wanted remote MCP.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import mcp.types as mcp_types
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger('leartech.mcp_bridge')

_BRIDGE_NAME = 'leartech-mcp-bridge'


def build_bridge_server(session: ClientSession, downstream_tools: list[mcp_types.Tool]) -> Server:
    """Build the upstream stdio ``Server`` that proxies to ``session``.

    Factored out (takes the already-initialised downstream session + its tool
    list) so the proxy delegation can be unit-tested without real transports.
    ``tools/list`` returns the downstream tools verbatim; ``tools/call`` forwards
    to the downstream session and returns its ``CallToolResult`` unchanged, so
    errors (``isError``) and structured content survive the hop.
    """
    server: Server = Server(_BRIDGE_NAME)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:
        return downstream_tools

    # validate_input=False: the downstream server is the single schema authority,
    # so we don't re-validate here (double-validation only risks false rejects if
    # the two jsonschema versions disagree).
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, object]) -> mcp_types.CallToolResult:
        # Forward verbatim — the downstream CallToolResult carries content +
        # isError + structuredContent, all returned unchanged (transparent proxy).
        return await session.call_tool(name, arguments)

    return server


async def _run() -> int:
    url = os.environ.get('LEARTECH_MCP_BRIDGE_URL', '').strip()
    if not url:
        log.error('LEARTECH_MCP_BRIDGE_URL unset — bridge cannot start')
        return 2
    token = os.environ.get('LEARTECH_MCP_BRIDGE_TOKEN', '').strip()
    headers = {'Authorization': f'Bearer {token}'} if token else {}

    # Downstream: authed streamable-HTTP client → ClientSession. Kept open for
    # the whole process lifetime while we serve stdio upstream.
    async with streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            downstream_tools = (await session.list_tools()).tools
            log.info('bridge connected to %s — proxying %d tool(s)', url, len(downstream_tools))

            server = build_bridge_server(session, downstream_tools)
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
