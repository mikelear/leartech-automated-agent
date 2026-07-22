"""Unit tests for the authed stdio↔http MCP bridge (Phase 2).

Exercises the proxy delegation in ``build_bridge_server`` with a fake
``call_tool_fn`` (no real transports) — proving:
  * ``tools/list`` returns the advertised tools verbatim,
  * ``tools/call`` delegates to ``call_tool_fn`` and returns its ``CallToolResult``
    unchanged (content + isError preserved), and
  * the module runs exactly as spawned.
The fresh-per-call connection + timeout in ``_make_caller`` is what stops a
dead/idle downstream connection from hanging the agent (the 2026-07-22 open_pr
hang); the delegation contract below is what makes the proxy transparent.
"""

from __future__ import annotations

import os
import subprocess
import sys

import mcp.types as t
import pytest

from gate.mcp_servers.stdio_bridge import _flatten, _mint_token, build_bridge_server


def _tool(name: str) -> t.Tool:
    return t.Tool(name=name, description='x', inputSchema={'type': 'object'})


def _fake_caller(result: t.CallToolResult, sink: list[tuple[str, dict[str, object]]]):
    async def _call(name: str, arguments: dict[str, object]) -> t.CallToolResult:
        sink.append((name, arguments))
        return result

    return _call


async def test_bridge_lists_advertised_tools_verbatim() -> None:
    tools = [_tool('open_pr'), _tool('get_pr_diff')]
    server = build_bridge_server(tools, _fake_caller(t.CallToolResult(content=[]), []))

    handler = server.request_handlers[t.ListToolsRequest]
    result = await handler(t.ListToolsRequest(method='tools/list'))

    assert [x.name for x in result.root.tools] == ['open_pr', 'get_pr_diff']


async def test_bridge_forwards_call_and_returns_result_verbatim() -> None:
    downstream = t.CallToolResult(
        content=[t.TextContent(type='text', text='{"targetPR": 7}')],
        isError=False,
    )
    calls: list[tuple[str, dict[str, object]]] = []
    server = build_bridge_server([_tool('open_pr')], _fake_caller(downstream, calls))

    handler = server.request_handlers[t.CallToolRequest]
    req = t.CallToolRequest(
        method='tools/call',
        params=t.CallToolRequestParams(name='open_pr', arguments={'repo': 'x', 'branch': 'b'}),
    )
    result = await handler(req)

    assert calls == [('open_pr', {'repo': 'x', 'branch': 'b'})]  # delegated verbatim
    assert result.root.content[0].text == '{"targetPR": 7}'  # result returned unchanged
    assert result.root.isError is False


async def test_bridge_preserves_downstream_error() -> None:
    """A downstream error (isError=True) — including the bridge's own
    timeout/transport failure result — must survive the hop so the agent's
    retry→FAIL hardening fires instead of silently succeeding."""
    downstream = t.CallToolResult(content=[t.TextContent(type='text', text='boom')], isError=True)
    server = build_bridge_server([_tool('open_pr')], _fake_caller(downstream, []))

    handler = server.request_handlers[t.CallToolRequest]
    req = t.CallToolRequest(method='tools/call', params=t.CallToolRequestParams(name='open_pr', arguments={}))
    result = await handler(req)

    assert result.root.isError is True
    assert result.root.content[0].text == 'boom'


def test_mint_token_none_when_auth_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No auth creds → mint returns None (bridge then surfaces a clean tool error
    rather than sending a bogus/empty bearer)."""
    for k in ('LEARTECH_AUTH_TOKEN_URL', 'LEARTECH_AUTH_CLIENT_ID', 'LEARTECH_AUTH_CLIENT_SECRET'):
        monkeypatch.delenv(k, raising=False)
    assert _mint_token() is None


def test_flatten_unwraps_exception_group() -> None:
    """The opaque 'unhandled errors in a TaskGroup' must be flattened to the real
    inner error so failures are diagnosable (the open_pr expired-token error was
    hidden inside an ExceptionGroup)."""
    eg = ExceptionGroup('unhandled errors in a TaskGroup', [ValueError('token is expired')])
    out = _flatten(eg)
    assert 'token is expired' in out
    assert 'ValueError' in out
    assert _flatten(RuntimeError('boom')) == 'RuntimeError: boom'


def test_bridge_module_is_runnable_and_requires_url() -> None:
    """Smoke: the bridge runs exactly as remote.py spawns it
    (`python -m gate.mcp_servers.stdio_bridge`) and, with no downstream URL,
    exits 2 rather than hanging or import-erroring."""
    env = {k: v for k, v in os.environ.items() if k != 'LEARTECH_MCP_BRIDGE_URL'}
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, '-m', 'gate.mcp_servers.stdio_bridge'],
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 2, proc.stderr.decode()[-500:]
    assert b'LEARTECH_MCP_BRIDGE_URL unset' in proc.stderr
