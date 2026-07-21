"""Unit tests for gate.mcp_servers.remote — the agent's authed remote-MCP wiring.

Pins the two things that matter: (1) graceful degradation to ``{}`` when the
auth env is absent (laptop / preview / not-yet-wired) so the agent never
crashes at startup, and (2) correct authed Streamable-HTTP config (audience,
Bearer header, URL composition) when fully configured — the exact shape the
SDK needs to reach leartech-mcp-servers' open_pr.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gate.mcp_servers import remote

_AUTH_ENV = {
    'LEARTECH_MCP_URL': 'http://leartech-mcp-servers.jx-staging.svc.cluster.local',
    'LEARTECH_AUTH_TOKEN_URL': 'https://hydra.example/oauth2/token',
    'LEARTECH_AUTH_CLIENT_ID': 'controller-internal-services',
    'LEARTECH_AUTH_CLIENT_SECRET': 'sekret',
    'LEARTECH_AUTH_SCOPE': 'leartechapi.internal_services',
}


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for key in (*_AUTH_ENV, 'LEARTECH_MCP_URL'):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)


class _FakeResp:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_no_mcp_url_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LEARTECH_MCP_URL → no remote MCPs, no crash (splat of {} is a no-op)."""
    _set_env(monkeypatch, {})
    assert remote.build_remote_mcp_servers() == {}


def test_mcp_url_but_no_creds_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL set but no auth creds → token mint returns None → {} (degrade)."""
    _set_env(monkeypatch, {'LEARTECH_MCP_URL': _AUTH_ENV['LEARTECH_MCP_URL']})
    assert remote.build_remote_mcp_servers() == {}


def test_mint_token_missing_creds_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {'LEARTECH_AUTH_TOKEN_URL': 'https://hydra.example/oauth2/token'})
    assert remote.mint_mcp_token() is None


def test_mint_token_posts_audience_and_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The grant must bind audience=leartech-mcp (RFC 8707) so the internal MCP accepts it."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    captured: dict[str, Any] = {}

    def _fake_post(url: str, *, data: dict[str, str], timeout: float) -> _FakeResp:
        captured['url'] = url
        captured['data'] = data
        return _FakeResp(200, {'access_token': 'tok-abc'})

    monkeypatch.setattr(remote.httpx, 'post', _fake_post)
    token = remote.mint_mcp_token()
    assert token == 'tok-abc'
    assert captured['url'] == _AUTH_ENV['LEARTECH_AUTH_TOKEN_URL']
    assert captured['data']['grant_type'] == 'client_credentials'
    assert captured['data']['audience'] == remote.MCP_AUDIENCE == 'leartech-mcp'
    assert captured['data']['client_id'] == 'controller-internal-services'
    assert captured['data']['scope'] == 'leartechapi.internal_services'


def test_mint_token_non_200_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, dict(_AUTH_ENV))
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(401, {}))
    assert remote.mint_mcp_token() is None


def test_mint_token_transport_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, dict(_AUTH_ENV))

    def _boom(*a: Any, **k: Any) -> _FakeResp:
        raise httpx.ConnectError('refused')

    monkeypatch.setattr(remote.httpx, 'post', _boom)
    assert remote.mint_mcp_token() is None


def test_fully_configured_wires_pr_context_with_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: authed http MCP config for pr-context, no double slash, Bearer header."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(200, {'access_token': 'tok-xyz'}))
    servers = remote.build_remote_mcp_servers()
    assert set(servers) == {'leartech-pr-context'}
    cfg = servers['leartech-pr-context']
    assert cfg['type'] == 'http'
    assert cfg['url'] == 'http://leartech-mcp-servers.jx-staging.svc.cluster.local/mcp/pr_context'
    assert cfg['headers']['Authorization'] == 'Bearer tok-xyz'


def test_trailing_slash_on_base_does_not_double(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {**_AUTH_ENV, 'LEARTECH_MCP_URL': _AUTH_ENV['LEARTECH_MCP_URL'] + '/'})
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(200, {'access_token': 't'}))
    servers = remote.build_remote_mcp_servers()
    assert servers['leartech-pr-context']['url'].endswith('.local/mcp/pr_context')
    assert '//mcp/pr_context' not in servers['leartech-pr-context']['url']
