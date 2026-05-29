"""Tests for D.5 — Job status reconciler.

Coverage matrix:

1. **Log parsing** — extract turns/cost from the trailing `--- turns=...`
   summary line, and the last GitHub PR URL from `gh pr create` stdout.
2. **Terminal-state classification** — Job conditions translate cleanly to
   'complete' / 'failed' / None.
3. **reconcile_once integration** — with mocked K8s client + DB stubs,
   skips already-terminal rows, patches non-terminal rows with parsed values.

Real K8s + DB are NOT exercised here. Live verification happens by firing
an initiative on AZ and watching the DB row transition queued -> complete.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gate.agent.job_reconciler import (
    _job_terminal_state,
    _parse_summary,
    reconcile_once,
)

# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def test_parse_summary_extracts_last_summary_line() -> None:
    """Agent emits one summary per iteration AND a final post-PR-resolution
    summary. We always use the final one — that's the authoritative entry
    that includes pr=N when a PR was opened."""
    log = (
        '→ Bash\n'
        '--- turns=2  in=15  out=10  cost=$0.001\n'
        'more work\n'
        '--- turns=10  in=15  out=2945  cost=$0.5230\n'
        '--- turns=10  in=0  out=0  cost=$0.5230  pr=237\n'
    )
    turns, cost, pr_number = _parse_summary(log)
    assert turns == 10
    assert cost == 0.5230
    assert pr_number == 237


def test_parse_summary_handles_missing_summary() -> None:
    """No summary line — e.g. crash before completion. Return all-None."""
    assert _parse_summary('some garbage\nno summary here\n') == (None, None, None)


def test_parse_summary_returns_none_pr_when_absent() -> None:
    """Final summary without `pr=N` — e.g. agent decided no changes needed.
    Don't fabricate a number, and don't go grepping URLs (PR #1 false-positive
    on prior run d93b17a6b82f surfaced this exact case)."""
    log = 'all done\n--- turns=2  in=0  out=0  cost=$0.0\n'
    turns, cost, pr_number = _parse_summary(log)
    assert turns == 2
    assert cost == 0.0
    assert pr_number is None


def test_parse_summary_ignores_url_references_in_prose() -> None:
    """A GitHub URL mentioned by the agent's prose (e.g. referencing a
    pre-existing PR for context) must NOT poison pr_number. Only the
    final `--- turns=...  pr=N` line is authoritative."""
    log = (
        'Existing PR for context: https://github.com/owner/repo/pull/100\n'
        '--- turns=3  in=10  out=20  cost=$0.05\n'
    )
    _, _, pr_number = _parse_summary(log)
    assert pr_number is None


# ---------------------------------------------------------------------------
# Terminal-state classification
# ---------------------------------------------------------------------------


def _make_job(name: str, conditions: list[tuple[str, str]]) -> SimpleNamespace:
    """Builds a K8s Job-like object with the given conditions list."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type=ctype, status=cstatus) for ctype, cstatus in conditions],
        ),
    )


def test_terminal_state_complete() -> None:
    job = _make_job('run-1', [('Complete', 'True')])
    assert _job_terminal_state(job) == 'complete'


def test_terminal_state_failed() -> None:
    job = _make_job('run-2', [('Failed', 'True')])
    assert _job_terminal_state(job) == 'failed'


def test_terminal_state_in_flight_returns_none() -> None:
    """Running Job has no terminal condition yet; reconciler must skip it."""
    job = _make_job('run-3', [('Available', 'True')])
    assert _job_terminal_state(job) is None


def test_terminal_state_ignores_false_conditions() -> None:
    """A condition with status=False is not terminal — defensive guard."""
    job = _make_job('run-4', [('Complete', 'False'), ('Failed', 'False')])
    assert _job_terminal_state(job) is None


# ---------------------------------------------------------------------------
# reconcile_once — integration with mocked K8s + DB stubs
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_k8s_ok():
    """Yields (batch, core) mocks for a successful single-pass reconcile."""
    batch = MagicMock()
    core = MagicMock()
    yield batch, core


@pytest.mark.asyncio
async def test_reconcile_once_skips_already_terminal_rows(_mock_k8s_ok: Any) -> None:  # type: ignore[valid-type]
    """A DB row already at status=complete must NOT be re-updated.
    Repeated polls would otherwise emit duplicate update events."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-already-done', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='complete'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = core

        count = await reconcile_once('jx-staging')

    assert count == 0
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_once_patches_non_terminal_row_with_parsed_fields(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """The happy path: terminal Job + queued DB row -> single update() call
    carrying status, finished_at, turns, cost, pr_number."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-needs-update', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name='run-needs-update-x9k2'))],
        ),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value=(
            'agent prose\n'
            'PR opened: https://github.com/mikelear/leartech-mortgages-gw/pull/513\n'
            '--- turns=12  in=20  out=3500  cost=$0.78\n'
            '--- turns=12  in=0  out=0  cost=$0.78  pr=513\n'
        ),
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='queued'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = core

        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.await_args.kwargs
    assert call_kwargs['status'] == 'complete'
    assert call_kwargs['turns'] == 12
    assert call_kwargs['cost_usd'] == 0.78
    assert call_kwargs['pr_number'] == 513
    assert 'finished_at' in call_kwargs


@pytest.mark.asyncio
async def test_reconcile_once_handles_log_fetch_failure_gracefully(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If pod logs can't be fetched (GC'd, log driver down), we STILL patch
    status + finished_at. turns/cost end up None — better than 'queued forever'."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-no-logs', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(side_effect=RuntimeError('log driver dead'))

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='queued'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = core

        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    kwargs = mock_update.await_args.kwargs
    assert kwargs['status'] == 'failed'
    assert kwargs['turns'] is None
    assert kwargs['cost_usd'] is None
