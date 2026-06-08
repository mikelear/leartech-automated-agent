"""Tests for the leartech-jx3-flow MCP server.

The rule logic itself lives in `test_jx3_flow_rules.py`. These tests cover:

- the GitHub-REST → PRSnapshot mapping (check-run state translation, label
  extraction, mergeable handling)
- the three @tool entry points returning the expected MCP shape
- the wait_for_merge polling loop short-circuits on terminal states
- the GH_TOKEN-missing surface
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from gate.mcp_servers import build_jx3_flow_server
from gate.mcp_servers.jx3_flow import (
    _check_state_for,
    _fetch_snapshot,
    _get_pr_jx3_stage,
    _list_required_actions,
    _wait_for_merge,
)

# Bind the @tool .handler coroutines once for brevity.
_Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]
_get_stage: _Handler = cast(_Handler, _get_pr_jx3_stage.handler)
_list_actions: _Handler = cast(_Handler, _list_required_actions.handler)
_wait_merge: _Handler = cast(_Handler, _wait_for_merge.handler)

# Test-only placeholder. Bandit/S106 flags string literals named like
# credentials; this constant makes the test intent explicit.
_TEST_TOKEN = 'fake-token-for-tests'  # noqa: S105 — clearly not a real secret


def _payload(result: dict[str, Any]) -> Any:
    """Extract the JSON payload from the MCP tool's wrapped response."""
    return json.loads(result['content'][0]['text'])


# ─── Fake GitHub server ───────────────────────────────────────────────────────


def _make_mock_transport(
    *,
    pr_payload: dict[str, Any],
    check_runs: list[dict[str, Any]],
) -> httpx.MockTransport:
    """Build an httpx MockTransport that serves one PR + its check-runs."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith('/check-runs'):
            return httpx.Response(200, json={'check_runs': check_runs})
        # Default: PR endpoint
        return httpx.Response(200, json=pr_payload)

    return httpx.MockTransport(handler)


def _pr(
    *,
    labels: tuple[str, ...] = (),
    merged: bool = False,
    head_sha: str = 'abc123',
    mergeable: bool | None = None,
) -> dict[str, Any]:
    return {
        'number': 1,
        'merged': merged,
        'mergeable': mergeable,
        'head': {'sha': head_sha},
        'labels': [{'name': name} for name in labels],
    }


def _check_run(name: str, *, status: str = 'completed', conclusion: str | None = 'success') -> dict[str, Any]:
    return {'name': name, 'status': status, 'conclusion': conclusion}


# ─── _check_state_for ──────────────────────────────────────────────────────────


def test_check_state_for_success() -> None:
    assert _check_state_for({'status': 'completed', 'conclusion': 'success'}) == 'success'


def test_check_state_for_failure_variants() -> None:
    for conclusion in ('failure', 'cancelled', 'timed_out', 'action_required'):
        assert _check_state_for({'status': 'completed', 'conclusion': conclusion}) == 'failure', conclusion


def test_check_state_for_pending() -> None:
    for status in ('queued', 'in_progress', 'waiting'):
        assert _check_state_for({'status': status, 'conclusion': None}) == 'pending', status


def test_check_state_for_neutral_defaults() -> None:
    assert _check_state_for({'status': 'completed', 'conclusion': 'neutral'}) == 'neutral'
    assert _check_state_for({'status': 'completed', 'conclusion': 'skipped'}) == 'neutral'


# ─── _fetch_snapshot ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_snapshot_assembles_pr_plus_checks() -> None:
    transport = _make_mock_transport(
        pr_payload=_pr(
            labels=('approved',),
            merged=False,
            head_sha='cafebabe',
            mergeable=True,
        ),
        check_runs=[
            _check_run('gcp/pr', conclusion='success'),
            _check_run('az/pr', status='in_progress', conclusion=None),
        ],
    )
    async with httpx.AsyncClient(transport=transport) as client:
        snap = await _fetch_snapshot(client, 'mikelear/foo', 1, token=_TEST_TOKEN)
    assert 'approved' in snap.labels
    assert snap.checks == {'gcp/pr': 'success', 'az/pr': 'pending'}
    assert snap.merged is False
    assert snap.head_sha == 'cafebabe'
    assert snap.mergeable is True


@pytest.mark.asyncio
async def test_fetch_snapshot_dedups_repeated_check_names() -> None:
    """When a check is re-run, GitHub returns multiple entries. The FIRST
    (freshest) should win, so the agent sees the current state."""
    transport = _make_mock_transport(
        pr_payload=_pr(head_sha='deadbeef'),
        check_runs=[
            # First entry is the most recent (the API returns newest first)
            _check_run('gcp/pr', status='in_progress', conclusion=None),
            _check_run('gcp/pr', conclusion='failure'),
        ],
    )
    async with httpx.AsyncClient(transport=transport) as client:
        snap = await _fetch_snapshot(client, 'mikelear/foo', 1, token=_TEST_TOKEN)
    assert snap.checks == {'gcp/pr': 'pending'}


@pytest.mark.asyncio
async def test_fetch_snapshot_qualifies_unprefixed_repo() -> None:
    """`foo` should resolve to `mikelear/foo` for GitHub URLs — same convention
    as the other MCP servers."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured['path'] = request.url.path
        if request.url.path.endswith('/check-runs'):
            return httpx.Response(200, json={'check_runs': []})
        return httpx.Response(200, json=_pr())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await _fetch_snapshot(client, 'foo', 1, token=_TEST_TOKEN)
    assert '/repos/mikelear/foo/' in captured['path']


# ─── _get_pr_jx3_stage tool ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pr_jx3_stage_returns_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(labels=('approved',), head_sha='aaa'),
        check_runs=[
            _check_run('gcp/pr', conclusion='success'),
            _check_run('az/pr', conclusion='success'),
        ],
    )

    # Inject our mock transport into the AsyncClient created inside the tool.
    real_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    with patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client):
        result = await _get_stage({'pr_repo': 'mikelear/foo', 'pr_number': 1})

    payload = _payload(result)
    assert payload['stage'] == 'pr_ready_to_merge'
    assert payload['blocking_predicate'] is None
    assert payload['labels'] == ['approved']
    assert payload['checks'] == {'gcp/pr': 'success', 'az/pr': 'success'}
    assert payload['merged'] is False
    assert payload['head_sha'] == 'aaa'


@pytest.mark.asyncio
async def test_get_pr_jx3_stage_held_blocking_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(labels=('approved', 'do-not-merge/hold')),
        check_runs=[
            _check_run('gcp/pr', conclusion='success'),
            _check_run('az/pr', conclusion='success'),
        ],
    )
    real_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    with patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client):
        result = await _get_stage({'pr_repo': 'mikelear/foo', 'pr_number': 1})
    payload = _payload(result)
    assert payload['stage'] == 'pr_held'
    assert 'do-not-merge/hold' in payload['blocking_predicate']


# ─── _list_required_actions tool ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_required_actions_held_returns_hold_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(labels=('approved', 'do-not-merge/hold')),
        check_runs=[
            _check_run('gcp/pr', conclusion='success'),
            _check_run('az/pr', conclusion='success'),
        ],
    )
    real_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    with patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client):
        result = await _list_actions({'pr_repo': 'mikelear/foo', 'pr_number': 1})
    payload = _payload(result)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]['command'] == '/hold cancel'


@pytest.mark.asyncio
async def test_list_required_actions_merged_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(merged=True),
        check_runs=[],
    )
    real_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    with patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client):
        result = await _list_actions({'pr_repo': 'mikelear/foo', 'pr_number': 1})
    assert _payload(result) == []


# ─── _wait_for_merge tool ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_merge_returns_immediately_on_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(labels=('approved',), merged=True),
        check_runs=[
            _check_run('gcp/pr', conclusion='success'),
            _check_run('az/pr', conclusion='success'),
        ],
    )
    real_async_client = httpx.AsyncClient
    sleep_calls: list[float] = []

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    with (
        patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client),
        patch('gate.mcp_servers.jx3_flow.asyncio.sleep', _fake_sleep),
    ):
        result = await _wait_merge({'pr_repo': 'foo', 'pr_number': 1, 'timeout_s': 600})

    payload = _payload(result)
    assert payload['final_stage'] == 'pr_merged_releasing'
    assert payload['merged'] is True
    # Should have short-circuited before sleeping a single time.
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_wait_for_merge_returns_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A required check failing terminates the wait — there's no point polling."""
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(),
        check_runs=[
            _check_run('gcp/pr', conclusion='failure'),
            _check_run('az/pr', conclusion='success'),
        ],
    )
    real_async_client = httpx.AsyncClient

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    async def _fake_sleep(seconds: float) -> None:
        pass

    with (
        patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client),
        patch('gate.mcp_servers.jx3_flow.asyncio.sleep', _fake_sleep),
    ):
        result = await _wait_merge({'pr_repo': 'foo', 'pr_number': 1, 'timeout_s': 600})
    payload = _payload(result)
    assert payload['final_stage'] == 'pr_checks_failing'
    assert payload['merged'] is False
    assert 'gcp/pr' in payload['blocking_predicate']


@pytest.mark.asyncio
async def test_wait_for_merge_times_out_with_last_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the wait runs to the deadline with no terminal state, return final_stage='timeout'
    but still include the last observed snapshot in the payload."""
    monkeypatch.setenv('GH_TOKEN', 'fake-token')
    transport = _make_mock_transport(
        pr_payload=_pr(),
        check_runs=[_check_run('gcp/pr', status='in_progress', conclusion=None)],
    )
    real_async_client = httpx.AsyncClient
    real_sleep = asyncio.sleep  # capture BEFORE the patch so _short_sleep doesn't recurse

    def _mock_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs['transport'] = transport
        return real_async_client(*args, **kwargs)

    async def _short_sleep(_seconds: float) -> None:
        # Tiny real sleep so wall-clock advances and the deadline-check exits.
        await real_sleep(0.05)

    # Drop the polling interval to ~0 so the loop exits within ~1s wall-clock.
    monkeypatch.setattr('gate.mcp_servers.jx3_flow._POLL_INTERVAL_S', 0.05)

    with (
        patch('gate.mcp_servers.jx3_flow.httpx.AsyncClient', _mock_client),
        patch('gate.mcp_servers.jx3_flow.asyncio.sleep', _short_sleep),
    ):
        result = await asyncio.wait_for(
            _wait_merge({'pr_repo': 'foo', 'pr_number': 1, 'timeout_s': 1}),
            timeout=5,
        )

    payload = _payload(result)
    assert payload['final_stage'] == 'timeout'
    assert payload['snapshot']['checks'] == {'gcp/pr': 'pending'}


# ─── GH_TOKEN missing ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_gh_token_raises_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('GH_TOKEN', raising=False)
    with pytest.raises(RuntimeError, match='GH_TOKEN'):
        await _get_stage({'pr_repo': 'foo', 'pr_number': 1})


# ─── server builder ──────────────────────────────────────────────────────────


def test_build_jx3_flow_server_constructs() -> None:
    """Smoke test — the SDK MCP wrapper builds without raising."""
    # Ensure no GH_TOKEN dependency at build time (it's only needed when tools are called).
    saved = os.environ.pop('GH_TOKEN', None)
    try:
        server = build_jx3_flow_server()
        assert server is not None
    finally:
        if saved is not None:
            os.environ['GH_TOKEN'] = saved
