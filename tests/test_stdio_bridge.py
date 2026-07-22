"""Unit tests for the authed stdio↔http MCP bridge (Phase 2).

Exercises the proxy delegation in ``build_bridge_server`` against a fake
downstream ``ClientSession`` — no real transports — so we prove:
  * ``tools/list`` returns the downstream tools verbatim, and
  * ``tools/call`` forwards (name, arguments) to the downstream session and
    returns its ``CallToolResult`` unchanged (content + isError preserved).
This is the behaviour that fixes open_pr: the bridge — not the header-dropping
Claude CLI — is the MCP client, so the authed call actually reaches the server.
"""

from __future__ import annotations

import os
import subprocess
import sys

import mcp.types as t

from gate.mcp_servers.stdio_bridge import build_bridge_server


class _FakeSession:
    """Minimal stand-in for mcp.ClientSession — records calls, returns a canned result."""

    def __init__(self, result: t.CallToolResult | None = None) -> None:
        self._result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object]) -> t.CallToolResult:
        self.calls.append((name, arguments))
        assert self._result is not None
        return self._result


def _tool(name: str) -> t.Tool:
    return t.Tool(name=name, description='x', inputSchema={'type': 'object'})


async def test_bridge_lists_downstream_tools_verbatim() -> None:
    tools = [_tool('open_pr'), _tool('get_pr_diff')]
    server = build_bridge_server(_FakeSession(), tools)  # type: ignore[arg-type]

    handler = server.request_handlers[t.ListToolsRequest]
    result = await handler(t.ListToolsRequest(method='tools/list'))

    assert [x.name for x in result.root.tools] == ['open_pr', 'get_pr_diff']


async def test_bridge_forwards_call_and_returns_result_verbatim() -> None:
    downstream = t.CallToolResult(
        content=[t.TextContent(type='text', text='{"targetPR": 7}')],
        isError=False,
    )
    fake = _FakeSession(downstream)
    server = build_bridge_server(fake, [_tool('open_pr')])  # type: ignore[arg-type]

    handler = server.request_handlers[t.CallToolRequest]
    req = t.CallToolRequest(
        method='tools/call',
        params=t.CallToolRequestParams(name='open_pr', arguments={'repo': 'x', 'branch': 'b'}),
    )
    result = await handler(req)

    # forwarded verbatim to the downstream session…
    assert fake.calls == [('open_pr', {'repo': 'x', 'branch': 'b'})]
    # …and the downstream result returned unchanged (content + isError).
    assert result.root.content[0].text == '{"targetPR": 7}'
    assert result.root.isError is False


async def test_bridge_preserves_downstream_error() -> None:
    """A downstream tool error (isError=True) must survive the hop — the agent
    sees a real tool failure, not a masked success (so its retry→FAIL hardening
    still fires)."""
    downstream = t.CallToolResult(
        content=[t.TextContent(type='text', text='boom')],
        isError=True,
    )
    server = build_bridge_server(_FakeSession(downstream), [_tool('open_pr')])  # type: ignore[arg-type]

    handler = server.request_handlers[t.CallToolRequest]
    req = t.CallToolRequest(
        method='tools/call',
        params=t.CallToolRequestParams(name='open_pr', arguments={}),
    )
    result = await handler(req)

    assert result.root.isError is True
    assert result.root.content[0].text == 'boom'


def test_bridge_module_is_runnable_and_requires_url() -> None:
    """Smoke: the bridge runs exactly as remote.py spawns it
    (`python -m gate.mcp_servers.stdio_bridge`) and, with no downstream URL,
    exits 2 rather than hanging or import-erroring. Proves the module entrypoint
    + asyncio/main wiring load (the part the unit tests can't reach)."""
    env = {k: v for k, v in os.environ.items() if k != 'LEARTECH_MCP_BRIDGE_URL'}
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, '-m', 'gate.mcp_servers.stdio_bridge'],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 2, proc.stderr.decode()[-500:]
    assert b'LEARTECH_MCP_BRIDGE_URL unset' in proc.stderr
