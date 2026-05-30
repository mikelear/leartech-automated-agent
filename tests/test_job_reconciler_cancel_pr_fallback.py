"""Tests for D.5.1.5 — cancelled-row GH-side PR fallback.

The cancel endpoint (POST /initiatives/{id}/cancel) deletes the K8s Job
via Background propagation and writes `status='cancelled'` synchronously.
Both effects mean the reconciler's Job-iteration loop never sees these
runs — but the agent may have already opened a PR before SIGTERM, so the
DB row finalises with `pr_number=null` and operators lose the link.

D.5.1.5 fixes this by adding a second reconciler pass that walks DB rows
with `status='cancelled' AND pr_number IS NULL`, runs the D.5.1.1
GH-side fallback (`_lookup_pr_by_branch`), and patches `pr_number` when
GH returns a match. Status stays `cancelled` — the fallback only
enriches metadata.

Contract pinned here:

* cancelled + missing-pr + branch + pr_repo → pr_number patched
* status NEVER changes (stays cancelled even after enrichment)
* cancelled + already-has-pr → skipped (idempotent)
* cancelled + missing-branch → skipped (pre-D.5.1.2 NULL row case)
* cancelled + missing-pr_repo → skipped (no repo to query)
* DB not enabled → no-op
* GH lookup returns None → row left as-is (next pass retries)
* Other terminal statuses (failed/complete/orphaned/timed_out) NOT
  enriched by this pass (they have their own paths or are out of scope)

The D.5.1.5 pattern source is PR #74 (D.5.1.1 — GH-side fallback for
log-parse misses); this slice extends that fallback to a different
trigger (cancel-endpoint) rather than re-implementing the GH lookup.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gate.agent.job_reconciler import _enrich_cancelled_rows_missing_pr, reconcile_once


def _make_row(
    *,
    id: str,
    status: str = 'cancelled',
    pr_number: int | None = None,
    pr_repo: str | None = 'mikelear/example-svc',
    branch: str | None = 'agent/cancel-fixture',
) -> SimpleNamespace:
    """Build a DB-row-like object matching the `InitiativeRunRecord` shape
    that `list_runs` returns. Defaults to the canonical cancelled+missing-pr
    case; individual tests override the field(s) they want to exercise."""
    return SimpleNamespace(
        id=id,
        status=status,
        pr_number=pr_number,
        pr_repo=pr_repo,
        branch=branch,
    )


# ---------------------------------------------------------------------------
# _enrich_cancelled_rows_missing_pr — direct unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_patches_pr_number_for_cancelled_missing_pr_row() -> None:
    """Headline behaviour: cancelled row with pr_number=None and a valid
    branch+pr_repo → GH lookup returns 42 → row patched.

    Status NOT included in the update kwargs (only `pr_number=...`) — this is
    the contract that the fallback never changes terminal status, only
    enriches metadata."""
    row = _make_row(id='run-cancel-1')

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=42) as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 1
    mock_lookup.assert_called_once_with('mikelear/example-svc', 'agent/cancel-fixture', 'run-cancel-1')
    mock_update.assert_awaited_once_with('run-cancel-1', pr_number=42)
    # Critical assertion: `status` key is NOT in the update kwargs. The fallback
    # only enriches metadata; the row stays at status='cancelled' from the
    # earlier synchronous write in the cancel endpoint.
    assert 'status' not in mock_update.await_args.kwargs


@pytest.mark.asyncio
async def test_enrich_skips_cancelled_row_when_pr_already_set() -> None:
    """Idempotency: cancelled row that ALREADY has a pr_number must not be
    re-enriched. Repeated reconciler passes on the same row must not re-fire
    the GH `pr list` subprocess fork (cheap but wasteful) and must never
    overwrite a known-good pr_number with whatever GH returns now."""
    row = _make_row(id='run-cancel-already-set', pr_number=99)

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 0
    mock_lookup.assert_not_called()
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_skips_cancelled_row_when_branch_is_none() -> None:
    """Pre-D.5.1.2 NULL-branch contract: rows from before the
    `0004_branch_column.sql` migration carry `branch=None`. Without an
    authoritative branch the GH lookup would either fail or — worse — match
    against the wrong PR. Same guard as the Job-iteration path uses; pinned
    here so the cancelled fallback inherits the same safety."""
    row = _make_row(id='run-cancel-no-branch', branch=None)

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 0
    mock_lookup.assert_not_called()
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_skips_cancelled_row_when_pr_repo_is_none() -> None:
    """No qualified repo on the row → nothing to query. Cancelled before the
    loader resolved a repo (rare but possible if cancel raced an early-stage
    register). Skip silently."""
    row = _make_row(id='run-cancel-no-repo', pr_repo=None)

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 0
    mock_lookup.assert_not_called()
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_leaves_row_unpatched_when_gh_lookup_returns_none() -> None:
    """GH returned no matching PR (branch was never pushed before SIGTERM,
    or the PR was closed/merged out of state) → pr_number stays None on the
    row. Next reconcile pass will retry — that's deliberate: this row keeps
    matching the cancelled+missing-pr filter until GH agrees there's a PR."""
    row = _make_row(id='run-cancel-gh-empty')

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=None) as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 0
    mock_lookup.assert_called_once()
    mock_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_enrich_is_noop_when_db_disabled() -> None:
    """In-memory mode (tests / dev without Postgres) → the pass short-circuits.
    Returning 0 keeps the reconciler's `updates` counter accurate so the
    Job-iteration test cases continue to assert exact counts."""
    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=False),
        patch('gate.agent.job_reconciler.list_runs') as mock_list,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
    ):
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 0
    mock_list.assert_not_called()
    mock_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_handles_multiple_rows_independently() -> None:
    """Realistic scenario: the cancel-pr-fallback pass finds two rows. One
    has a branch GH knows about (gets patched), one doesn't yet (left alone).
    Pinned because the loop must continue past a None-lookup row, not bail."""
    row_a = _make_row(id='run-a', branch='agent/feature-a')
    row_b = _make_row(id='run-b', branch='agent/feature-b')

    def _lookup(repo: str, branch: str, run_id: str) -> int | None:
        # First row: GH knows the PR. Second row: GH returns nothing.
        return 111 if branch == 'agent/feature-a' else None

    with (
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[row_a, row_b])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', side_effect=_lookup) as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)
        patched = await _enrich_cancelled_rows_missing_pr()

    assert patched == 1
    assert mock_lookup.call_count == 2
    mock_update.assert_awaited_once_with('run-a', pr_number=111)


# ---------------------------------------------------------------------------
# reconcile_once integration — the second pass fires after the Job loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_once_runs_cancelled_pr_fallback_after_job_loop() -> None:
    """Integration: with NO terminal K8s Jobs in the namespace (empty list),
    the reconciler still walks DB cancelled-rows missing pr_number and patches
    them via the GH-side fallback.

    This is the real-world scenario: cancel deletes the Job via Background
    propagation (Job gone immediately from API); the reconciler's normal
    Job-iteration loop is empty; the D.5.1.5 second pass is the only signal
    path that recovers pr_number for the cancelled row."""
    cancelled_row = _make_row(id='36465f844cc0', branch='agent/orchestrator-dynamic-fix')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session') as mock_db_session,
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[cancelled_row])),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=4) as mock_lookup,
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        # Empty Job list — cancel deleted the Job, nothing to iterate.
        batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[]))
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = MagicMock()
        mock_db_session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_db_session.return_value.__aexit__ = AsyncMock(return_value=None)

        count = await reconcile_once('jx-staging')

    # The single update is from the cancelled-row enrichment, not the Job loop.
    assert count == 1
    mock_lookup.assert_called_once_with('mikelear/example-svc', 'agent/orchestrator-dynamic-fix', '36465f844cc0')
    mock_update.assert_awaited_once_with('36465f844cc0', pr_number=4)
    # The contract: status stays 'cancelled'. The update kwargs MUST NOT
    # carry a status field — only pr_number.
    update_kwargs: Any = mock_update.await_args.kwargs
    assert 'status' not in update_kwargs
    assert update_kwargs == {'pr_number': 4}
