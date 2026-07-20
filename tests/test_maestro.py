"""Unit tests for :mod:`gate.agent.maestro` — the best-effort ``run.pr_opened`` push.

Pinned contracts (all live in the module docstring):

  * Gated on ``LEARTECH_MAESTRO_URL`` being materially set (present + non-empty).
    Unset → silent no-op, zero network traffic.
  * Any failure — network, non-2xx, timeout, missing httpx — is caught + logged
    at WARN and swallowed. The emit MUST NEVER raise.
  * The wire format matches the initiative goal's ``{topic, run, tenant, repo,
    pr_number, head_branch}`` shape.

The tests use ``pytest.MonkeyPatch`` for env-var + httpx patching, matching the
style of ``tests/test_agentrun_status.py`` (the sibling best-effort push).
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import gate.agent.maestro as maestro

# ─── Gating: no-op when env unset ────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_when_url_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent ``LEARTECH_MAESTRO_URL`` → silent return, no httpx import needed."""
    monkeypatch.delenv('LEARTECH_MAESTRO_URL', raising=False)
    monkeypatch.delenv('LEARTECH_MAESTRO_TOKEN', raising=False)

    # If the emitter tried to POST it would either crash on the missing
    # httpx or attempt real DNS. Assert neither: bare await returns.
    await maestro.emit_run_pr_opened(
        run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
    )


@pytest.mark.asyncio
async def test_noop_when_url_env_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``""`` from an empty ConfigMap key must be treated like absent."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', '')
    await maestro.emit_run_pr_opened(
        run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
    )


@pytest.mark.asyncio
async def test_noop_when_url_env_whitespace_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only env value (mis-configured ConfigMap) → no-op."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', '   \n\t  ')
    await maestro.emit_run_pr_opened(
        run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
    )


# ─── Success path: payload shape + auth header ───────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = '') -> None:
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Minimal httpx.AsyncClient stand-in — captures the POST call args."""

    def __init__(self, response: _FakeResponse, captured: dict[str, Any], **kwargs: Any) -> None:
        self._response = response
        self._captured = captured
        self._captured['ctor_kwargs'] = kwargs

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        self._captured['url'] = url
        self._captured['json'] = json
        self._captured['headers'] = headers
        return self._response


@pytest.mark.asyncio
async def test_success_path_posts_expected_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')
    monkeypatch.delenv('LEARTECH_MAESTRO_TOKEN', raising=False)

    captured: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(_FakeResponse(status_code=202), captured, **kwargs)

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _make_client
    monkeypatch.setitem(__import__('sys').modules, 'httpx', fake_httpx)

    await maestro.emit_run_pr_opened(
        run='run-abc',
        tenant='acme',
        repo='mikelear/example-svc',
        pr_number=99,
        head_branch='agent/foo',
    )

    assert captured['url'] == 'https://maestro.example/publish'
    assert captured['json'] == {
        'topic': 'run.pr_opened',
        'run': 'run-abc',
        'tenant': 'acme',
        'repo': 'mikelear/example-svc',
        'pr_number': 99,
        'head_branch': 'agent/foo',
    }
    assert captured['headers']['Content-Type'] == 'application/json'
    # No token env → no Authorization header.
    assert 'Authorization' not in captured['headers']
    # AsyncClient constructed with a bounded timeout.
    assert captured['ctor_kwargs'].get('timeout') == 5.0


@pytest.mark.asyncio
async def test_success_path_injects_bearer_token_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')
    monkeypatch.setenv('LEARTECH_MAESTRO_TOKEN', 'ghs_test_token')

    captured: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(_FakeResponse(status_code=200), captured, **kwargs)

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _make_client
    monkeypatch.setitem(__import__('sys').modules, 'httpx', fake_httpx)

    await maestro.emit_run_pr_opened(
        run='run-1', tenant=None, repo='mikelear/example', pr_number=42, head_branch='agent/x'
    )

    assert captured['headers']['Authorization'] == 'Bearer ghs_test_token'


@pytest.mark.asyncio
async def test_success_path_accepts_null_tenant_and_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Laptop runs have no LEARTECH_RUN_ID/tenant — the payload includes JSON nulls."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')

    captured: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(_FakeResponse(status_code=200), captured, **kwargs)

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _make_client
    monkeypatch.setitem(__import__('sys').modules, 'httpx', fake_httpx)

    await maestro.emit_run_pr_opened(
        run=None, tenant=None, repo='mikelear/example', pr_number=42, head_branch='agent/x'
    )

    assert captured['json']['run'] is None
    assert captured['json']['tenant'] is None


# ─── Failure modes: emit MUST NEVER raise ────────────────────────────────


@pytest.mark.asyncio
async def test_non_2xx_response_is_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """5xx from Maestro → WARN log, return, no raise."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')

    captured: dict[str, Any] = {}

    def _make_client(**kwargs: Any) -> _FakeAsyncClient:
        return _FakeAsyncClient(_FakeResponse(status_code=503, text='service unavailable'), captured, **kwargs)

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _make_client
    monkeypatch.setitem(__import__('sys').modules, 'httpx', fake_httpx)

    with caplog.at_level(logging.WARNING, logger='gate.agent.maestro'):
        await maestro.emit_run_pr_opened(
            run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
        )

    warns = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any('503' in m and 'run.pr_opened' in m for m in warns), f'expected WARN about 503; got: {warns!r}'


@pytest.mark.asyncio
async def test_httpx_exception_is_swallowed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Any exception from httpx (connection refused, DNS, timeout) must be swallowed."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')

    class _ExplodingClient:
        def __init__(self, **_kwargs: Any) -> None: ...

        async def __aenter__(self) -> _ExplodingClient:
            raise ConnectionError('connection refused')

        async def __aexit__(self, *_a: Any) -> bool:
            return False

        post = AsyncMock()

    fake_httpx = MagicMock()
    fake_httpx.AsyncClient = _ExplodingClient
    monkeypatch.setitem(__import__('sys').modules, 'httpx', fake_httpx)

    with caplog.at_level(logging.WARNING, logger='gate.agent.maestro'):
        # Must return normally — never raise, even on connection failure.
        await maestro.emit_run_pr_opened(
            run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
        )

    warns = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any('run.pr_opened' in m and 'connection refused' in m for m in warns), (
        f'expected WARN citing the exception; got: {warns!r}'
    )


@pytest.mark.asyncio
async def test_missing_httpx_is_swallowed(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Even if httpx is not importable (defensive edge), the emit must not raise."""
    monkeypatch.setenv('LEARTECH_MAESTRO_URL', 'https://maestro.example/publish')

    # Set httpx to None in sys.modules — subsequent `import httpx` inside
    # the function will bind ``httpx = None`` which then explodes on
    # attribute access. That's the failure mode we want to prove is
    # swallowed.
    import sys as _sys

    monkeypatch.setitem(_sys.modules, 'httpx', None)

    with caplog.at_level(logging.WARNING, logger='gate.agent.maestro'):
        await maestro.emit_run_pr_opened(
            run='run-1', tenant='acme', repo='mikelear/example', pr_number=42, head_branch='agent/x'
        )

    warns = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any('run.pr_opened' in m for m in warns), f'expected a WARN describing the failure; got: {warns!r}'
