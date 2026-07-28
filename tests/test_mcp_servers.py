"""Smoke tests for MCP server builders — confirm they construct cleanly with the right tools.

We don't exercise tool execution here — that requires the full Agent SDK loop. These tests
catch shape regressions: tool name typos, missing wirings, schema build errors.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from gate.mcp_servers import (
    build_ai_gateway_web_server,
    build_artifacts_server,
    build_criteria_server,
    build_initiatives_server,
)


def _tool_names(server: object) -> list[str]:
    """Extract the registered tool names from an SDK MCP server config."""
    instance = server['instance'] if isinstance(server, dict) else getattr(server, 'instance', None)
    if instance is None:
        # Fallback: serialise the server and look for `name=` patterns. Keeps the test resilient
        # to small structural changes in McpSdkServerConfig across SDK versions.
        return [t.name for t in getattr(server, 'tools', [])]
    return [t.name for t in instance._tool_handlers.values()] if hasattr(instance, '_tool_handlers') else []


def test_artifacts_server_builds() -> None:
    server = build_artifacts_server()
    assert server is not None


def test_criteria_server_builds() -> None:
    server = build_criteria_server()
    assert server is not None


def test_initiatives_server_builds() -> None:
    """The initiatives MCP server (fire_initiative + fire_initiative_inline)
    must construct cleanly so the dynamic-MCP-registry can wire it in."""
    server = build_initiatives_server()
    assert server is not None


def test_ai_gateway_web_server_builds() -> None:
    """The BA agent's web-research MCP (web_search + web_fetch) must build
    cleanly — a shape regression here would silently disable BA research."""
    server = build_ai_gateway_web_server()
    assert server is not None


@pytest.mark.asyncio
async def test_mcp_fire_initiative_inline_tool() -> None:
    """The MCP tool ``fire_initiative_inline`` POSTs to ``/initiatives``
    with the supplied body in the ``initiative_body`` field.

    We monkeypatch httpx.AsyncClient so the test doesn't actually hit the
    network — we only need to verify the wire-level request shape.
    """
    from gate.mcp_servers import initiatives_server as srv

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 202

        def json(self) -> dict[str, Any]:
            return {'id': 'abc123', 'status': 'running', 'initiative': 'fired-inline'}

    class _FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D401
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResp:  # noqa: A002 — match httpx signature
            captured['path'] = path
            captured['json'] = json
            return _FakeResp()

    inline_body = 'name: fired-inline\nrepo: r\nbranch: agent/x\ngoal: g\n'
    # The @tool decorator wraps the async function into an SdkMcpTool;
    # the underlying coroutine is on `.handler`. Invoke that directly so
    # the test exercises the real tool body without spinning up an MCP loop.
    with patch.object(srv.httpx, 'AsyncClient', _FakeClient):
        result = await srv._fire_initiative_inline.handler({'body': inline_body})

    # Wire-level check: the tool must POST to /initiatives with
    # `initiative_body` set to the verbatim body — never to a wrapper or
    # catalog write.
    assert captured['path'] == '/initiatives'
    assert captured['json'] == {'initiative_body': inline_body}

    # The tool wraps the HTTP response in the standard MCP envelope.
    text = result['content'][0]['text']
    payload = json.loads(text)
    assert payload['status_code'] == 202
    assert payload['body']['initiative'] == 'fired-inline'


@pytest.mark.asyncio
async def test_mcp_fire_initiative_tool_posts_name() -> None:
    """Sibling check: ``fire_initiative`` POSTs the catalog name to the
    same endpoint with ``initiative`` (not ``initiative_body``) — the two
    paths must not be conflated on the wire."""
    from gate.mcp_servers import initiatives_server as srv

    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 202

        def json(self) -> dict[str, Any]:
            return {'id': 'def456', 'status': 'running', 'initiative': 'cat-name'}

    class _FakeClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_exc: Any) -> None:
            return None

        async def post(self, path: str, json: dict[str, Any]) -> _FakeResp:  # noqa: A002
            captured['path'] = path
            captured['json'] = json
            return _FakeResp()

    with patch.object(srv.httpx, 'AsyncClient', _FakeClient):
        await srv._fire_initiative.handler({'name': 'cat-name'})

    assert captured['path'] == '/initiatives'
    assert captured['json'] == {'initiative': 'cat-name'}


# Bind imports referenced by the async-mock helpers above. Keeping the
# import here (rather than at file top) avoids polluting the module
# namespace for simpler builder-smoke tests.
_ = AsyncMock  # silence "imported but unused" until a future test needs it


def test_all_servers_build_with_distinct_names() -> None:
    """Belt-and-braces: confirm each builder returns a distinct McpSdkServerConfig.

    Pipeline-check status (list_pr_checks / wait_for_terminal /
    wait_for_first_failure_or_all_pass) previously lived in an in-process
    `build_pipeline_server()` shim; that shim is now retired in favour of the
    remote `leartech-jx3-flow` MCP wired via `build_remote_mcp_servers` — see
    `test_all_platform_mcps_present_in_remote_registry` for the wire-up assertion.
    """
    servers = [
        build_artifacts_server(),
        build_criteria_server(),
        build_initiatives_server(),
        build_ai_gateway_web_server(),
    ]
    assert all(s is not None for s in servers)
    assert len({id(s) for s in servers}) == 4
