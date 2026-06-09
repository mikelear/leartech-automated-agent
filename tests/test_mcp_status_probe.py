"""Unit tests for :mod:`gate.introspection.mcp_status`.

The active-probe wraps the static ``reachable_status`` with type-aware
liveness checks. We cover each McpType branch here so a refactor that
changes one probe path doesn't silently re-route through 'down'.
"""

from __future__ import annotations

import httpx
import pytest

from gate.agent.mcp_catalog import McpServer
from gate.introspection.mcp_status import probe_mcp


def test_sdk_mcp_ready_when_builder_importable() -> None:
    mcp = McpServer(
        type='sdk',
        description='reachable sdk',
        builder='gate.mcp_servers.pipeline_server:build_pipeline_server',
    )
    assert probe_mcp(mcp) == 'ready'


def test_sdk_mcp_down_when_builder_missing() -> None:
    mcp = McpServer(
        type='sdk',
        description='broken sdk',
        builder='gate.mcp_servers.never_was:never_was',
    )
    assert probe_mcp(mcp) == 'down'


def test_stdio_mcp_ready_when_command_on_path() -> None:
    # `sh` is on PATH in every CI image we ship + on every dev laptop.
    mcp = McpServer(type='stdio', description='sh probe', command='sh')
    assert probe_mcp(mcp) == 'ready'


def test_stdio_mcp_down_when_command_missing() -> None:
    mcp = McpServer(type='stdio', description='ghost', command='this-binary-does-not-exist-xyz')
    assert probe_mcp(mcp) == 'down'


def test_http_mcp_ready_on_2xx_healthz(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(self: object, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(status_code=200, content=b'{}')

    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    mcp = McpServer(type='http_sse', description='ok server', url='https://mcp.example.com')
    assert probe_mcp(mcp) == 'ready'


def test_http_mcp_down_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(self: object, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(status_code=500, content=b'boom')

    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    mcp = McpServer(type='http_sse', description='broken server', url='https://mcp.example.com')
    assert probe_mcp(mcp) == 'down'


def test_http_mcp_down_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(self: object, url: str, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError('refused')

    monkeypatch.setattr(httpx.Client, 'get', fake_get)
    mcp = McpServer(type='http_sse', description='unreachable', url='https://mcp.example.com')
    assert probe_mcp(mcp) == 'down'


def test_static_not_built_passthrough() -> None:
    """If the catalog declares status='not_built', probe_mcp respects it
    without attempting a live probe."""
    mcp = McpServer(
        type='stdio',
        description='not yet',
        command='sh',
        status='not_built',
    )
    assert probe_mcp(mcp) == 'not_built'
