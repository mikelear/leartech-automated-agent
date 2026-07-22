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


# Live `/mcps` payload shape (host = source of truth): `mounts` carries the
# authoritative {name, path}; `servers` (name list) kept for backward-compat.
def _mounts(*names: str) -> list[dict[str, str]]:
    return [{'name': n, 'path': f'/mcp/{n}'} for n in names]


_ALL_ADVERTISED = {
    'servers': ['pr_context', 'tekton', 'jx3_flow', 'k8s', 'agent_api'],
    'mounts': _mounts('pr_context', 'tekton', 'jx3_flow', 'k8s', 'agent_api'),
}


def _mock_token(monkeypatch: pytest.MonkeyPatch, token: str = 'tok-xyz') -> None:
    """Mock the client_credentials token POST."""
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(200, {'access_token': token}))


def _mock_discovery(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], status: int = 200) -> None:
    """Mock the GET /mcps discovery call."""
    monkeypatch.setattr(remote.httpx, 'get', lambda *a, **k: _FakeResp(status, payload))


def test_no_mcp_url_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LEARTECH_MCP_URL → no remote MCPs, no crash (splat of {} is a no-op).

    Explicit for every registered entry (pr-context, tekton, jx3-flow) so a
    future addition to REMOTE_MCPS can't silently skip its degrade path.
    """
    _set_env(monkeypatch, {})
    servers = remote.build_remote_mcp_servers()
    assert servers == {}
    for name in ('leartech-pr-context', 'leartech-tekton', 'leartech-jx3-flow'):
        assert name not in servers, f'{name} must be absent when unconfigured'


def test_mcp_url_but_no_creds_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """URL set but no auth creds → token mint returns None → {} (degrade).

    Explicit for every registered entry (pr-context, tekton, jx3-flow) so
    the token-mint failure path uniformly drops every remote MCP, not just
    the first one hit.
    """
    _set_env(monkeypatch, {'LEARTECH_MCP_URL': _AUTH_ENV['LEARTECH_MCP_URL']})
    servers = remote.build_remote_mcp_servers()
    assert servers == {}
    for name in ('leartech-pr-context', 'leartech-tekton', 'leartech-jx3-flow'):
        assert name not in servers, f'{name} must be absent when auth mint fails'


def test_jx3_flow_absent_when_token_mint_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit regression: a 200 without an access_token drops jx3-flow (and every
    other remote entry) — no partial registration where jx3-flow leaks in without
    a Bearer header. Sibling coverage exists for the sibling entries via the
    happy-path test; this pins the failure path specifically for the newly-ported
    jx3-flow entry (retirement of the in-process pipeline_server shim)."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(200, {}))
    servers = remote.build_remote_mcp_servers()
    assert servers == {}
    assert 'leartech-jx3-flow' not in servers


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


def test_mint_token_200_without_access_token_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 whose body lacks access_token (or is non-str) → None (the `not
    isinstance(token, str)` guard), not a bogus 'None' Bearer header."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    monkeypatch.setattr(remote.httpx, 'post', lambda *a, **k: _FakeResp(200, {}))
    assert remote.mint_mcp_token() is None


def test_mint_token_transport_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, dict(_AUTH_ENV))

    def _boom(*a: Any, **k: Any) -> _FakeResp:
        raise httpx.ConnectError('refused')

    monkeypatch.setattr(remote.httpx, 'post', _boom)
    assert remote.mint_mcp_token() is None


def test_fully_configured_wires_from_discovery_with_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: every wanted MCP the host advertises on /mcps is wired at
    /mcp/<server-name> (the host's ground-truth name) with the Bearer header.
    The path is DISCOVERED, not hardcoded — this is the fix for the open_pr 404
    (agent hitting a guessed path the host didn't mount)."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    _mock_token(monkeypatch)
    _mock_discovery(monkeypatch, _ALL_ADVERTISED)
    servers = remote.build_remote_mcp_servers()
    assert set(servers) == {'leartech-pr-context', 'leartech-tekton', 'leartech-jx3-flow'}
    base = 'http://leartech-mcp-servers.jx-staging.svc.cluster.local'
    # Phase 2: each server is wired as a stdio bridge. The discovered downstream
    # URL (base + host-advertised path) + bearer are passed to the bridge via env;
    # the bridge (not the CLI) makes the authed streamable-HTTP call.
    assert servers['leartech-pr-context']['env']['LEARTECH_MCP_BRIDGE_URL'] == f'{base}/mcp/pr_context'
    assert servers['leartech-tekton']['env']['LEARTECH_MCP_BRIDGE_URL'] == f'{base}/mcp/tekton'
    assert servers['leartech-jx3-flow']['env']['LEARTECH_MCP_BRIDGE_URL'] == f'{base}/mcp/jx3_flow'
    for cfg in servers.values():
        assert cfg['type'] == 'stdio'
        assert cfg['args'] == ['-m', 'gate.mcp_servers.stdio_bridge']
        # The bridge mints a FRESH token per call (tokens are ~300s), so it gets
        # the auth CONFIG, NOT a static token that would expire mid-run.
        assert 'LEARTECH_MCP_BRIDGE_TOKEN' not in cfg['env']
        assert cfg['env']['LEARTECH_AUTH_TOKEN_URL'] == _AUTH_ENV['LEARTECH_AUTH_TOKEN_URL']
        assert cfg['env']['LEARTECH_AUTH_CLIENT_ID'] == _AUTH_ENV['LEARTECH_AUTH_CLIENT_ID']
        assert cfg['env']['LEARTECH_AUTH_CLIENT_SECRET'] == _AUTH_ENV['LEARTECH_AUTH_CLIENT_SECRET']


def test_wanted_mcp_absent_from_mcps_is_skipped_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the host does NOT advertise a wanted server, it is SKIPPED (loudly),
    never wired at a guessed path. This is the core drift-proofing: the agent
    only wires what the host actually mounts, so it can't 404 on a stale path."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    _mock_token(monkeypatch)
    # Host mounts pr_context + tekton but NOT jx3_flow.
    _mock_discovery(monkeypatch, {'mounts': _mounts('pr_context', 'tekton')})
    servers = remote.build_remote_mcp_servers()
    assert set(servers) == {'leartech-pr-context', 'leartech-tekton'}
    assert 'leartech-jx3-flow' not in servers, 'absent server must be skipped, not guessed'


def test_uses_host_advertised_path_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent wires the host's advertised path VERBATIM — even a non-conventional
    path — proving it does NOT construct /mcp/<name> itself (the drift fix)."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    _mock_token(monkeypatch)
    _mock_discovery(monkeypatch, {'mounts': [{'name': 'pr_context', 'path': '/custom/route/pr'}]})
    servers = remote.build_remote_mcp_servers()
    assert servers['leartech-pr-context']['env']['LEARTECH_MCP_BRIDGE_URL'] == (
        'http://leartech-mcp-servers.jx-staging.svc.cluster.local/custom/route/pr'
    )


def test_discovery_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """If /mcps itself fails (host unreachable / non-200), wire NO remote MCPs
    rather than blind-guess against an unverified host — the agent halts cleanly
    at open_pr instead of racking up 404s."""
    _set_env(monkeypatch, dict(_AUTH_ENV))
    _mock_token(monkeypatch)
    _mock_discovery(monkeypatch, {}, status=503)
    assert remote.build_remote_mcp_servers() == {}


def test_trailing_slash_on_base_does_not_double(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {**_AUTH_ENV, 'LEARTECH_MCP_URL': _AUTH_ENV['LEARTECH_MCP_URL'] + '/'})
    _mock_token(monkeypatch, token='t')
    _mock_discovery(monkeypatch, _ALL_ADVERTISED)
    servers = remote.build_remote_mcp_servers()
    dl_url = servers['leartech-pr-context']['env']['LEARTECH_MCP_BRIDGE_URL']
    assert dl_url.endswith('.local/mcp/pr_context')
    assert '//mcp/pr_context' not in dl_url


def test_discover_mounts_parses_name_path_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """discover_mounts returns {server-name: host-path} from the /mcps mounts."""
    _mock_discovery(monkeypatch, _ALL_ADVERTISED)
    got = remote.discover_mounts('http://host', 'tok')
    assert got == {
        'pr_context': '/mcp/pr_context',
        'tekton': '/mcp/tekton',
        'jx3_flow': '/mcp/jx3_flow',
        'k8s': '/mcp/k8s',
        'agent_api': '/mcp/agent_api',
    }


def test_discover_mounts_transitional_servers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Older host without `mounts` → derive /mcp/<name> from `servers` (transitional)."""
    _mock_discovery(monkeypatch, {'servers': ['pr_context', 'tekton']})
    got = remote.discover_mounts('http://host', 'tok')
    assert got == {'pr_context': '/mcp/pr_context', 'tekton': '/mcp/tekton'}


def test_discover_mounts_bad_payload_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_discovery(monkeypatch, {'unexpected': []})
    assert remote.discover_mounts('http://host', 'tok') is None
