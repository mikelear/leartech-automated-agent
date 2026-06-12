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

import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gate.agent.job_reconciler import (
    _build_job_crash_sticky_body,
    _job_terminal_state,
    _lookup_pr_by_branch,
    _parse_summary,
    reconcile_once,
)

# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------


def test_parse_summary_sums_turns_keeps_last_cost_and_pr() -> None:
    """Multi-summary aggregation contract (post run a9699b453342 fix):

    * ``turns`` is the SUM of every ``--- turns=`` line (each SDK invocation's
      counter starts from zero, so the last line only reflects its segment).
    * ``cost_usd`` is the LAST value — Claude SDK ``total_cost_usd`` is
      process-cumulative, so the most recent summary carries the total.
    * ``pr_number`` is the LAST ``pr=N`` — the post-PR-resolution summary
      is canonical.
    """
    log = (
        '→ Bash\n'
        '--- turns=2  in=15  out=10  cost=$0.001\n'
        'more work\n'
        '--- turns=10  in=15  out=2945  cost=$0.5230\n'
        '--- turns=10  in=0  out=0  cost=$0.5230  pr=237\n'
    )
    turns, cost, pr_number = _parse_summary(log)
    assert turns == 22  # 2 + 10 + 10
    assert cost == 0.5230
    assert pr_number == 237


def test_parse_summary_regression_a9699b453342_pattern() -> None:
    """Regression fixture for run a9699b453342 (2026-06-12) — the bug that
    motivated the sum-semantics fix. The agent emitted a 91-turn main run
    summary followed by a 1-turn post-PR side-channel watcher summary. The
    pre-fix reconciler picked the LAST and persisted ``turns=1``, masking
    the real work as a near-zero run. Under sum semantics the value is 92
    (>= 90, the spec acceptance criterion) and the cost stays at the last
    process-cumulative value."""
    log = (
        'agent main loop output...\n'
        'lots of intermediate work elided by --tail=200\n'
        '--- turns=91  in=2500  out=15000  cost=$13.6456  pr=88\n'
        'post-PR side-channel watcher fires\n'
        '--- turns=1  in=200  out=300  cost=$13.7872  pr=88\n'
    )
    turns, cost, pr_number = _parse_summary(log)
    assert turns == 92
    assert turns >= 90  # spec acceptance: turns should match the main work
    assert cost == 13.7872  # last process-cumulative cost
    assert pr_number == 88


def test_parse_summary_cost_is_last_not_summed() -> None:
    """Multi-summary log with monotonically-increasing cost values: cost is
    cumulative across the whole process (the SDK reports ``total_cost_usd``
    as a process-level running total), so the LAST value IS the total. We
    must NOT sum cost — that would double-count the running total."""
    log = (
        '--- turns=3  in=0  out=0  cost=$1.00\n'
        '--- turns=2  in=0  out=0  cost=$1.50\n'
        '--- turns=1  in=0  out=0  cost=$2.00  pr=42\n'
    )
    turns, cost, _ = _parse_summary(log)
    assert turns == 6  # summed: 3 + 2 + 1
    assert cost == 2.00  # last, not 4.50 (would be the buggy sum)


def test_parse_summary_pr_with_only_one_summary_carrying_pr() -> None:
    """Multi-summary log where only ONE line carries ``pr=N``: that line's
    ``pr`` is the answer regardless of position. Common case for the
    a9699b453342 pattern — the side-channel watcher's summary may carry the
    same ``pr=`` the main run already resolved."""
    log = '--- turns=5  in=0  out=0  cost=$0.20  pr=99\n--- turns=1  in=0  out=0  cost=$0.25\n'
    _, _, pr_number = _parse_summary(log)
    assert pr_number == 99


def test_parse_summary_pr_last_wins_when_multiple_summaries_have_pr() -> None:
    """Multi-summary log where multiple lines carry ``pr=N`` (rare but
    possible — e.g. a follow-up segment re-resolves the same PR): the LAST
    one wins. The post-PR-resolution summary is canonical even when an
    earlier segment also published a ``pr=N``."""
    log = '--- turns=5  in=0  out=0  cost=$0.20  pr=99\n--- turns=1  in=0  out=0  cost=$0.25  pr=100\n'
    _, _, pr_number = _parse_summary(log)
    assert pr_number == 100


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
    log = 'Existing PR for context: https://github.com/owner/repo/pull/100\n--- turns=3  in=10  out=20  cost=$0.05\n'
    _, _, pr_number = _parse_summary(log)
    assert pr_number is None


# ---------------------------------------------------------------------------
# D.5.1.4 — early-emit `--- pr_open pr=N` marker
# ---------------------------------------------------------------------------
#
# Slice fix: the agent's final `--- turns=...  pr=N` summary line is emitted
# at the very end of `run_initiative`, AFTER all the gate waits. If the agent
# exits abnormally (wait_for_terminal blocks past pod SIGTERM, SDK exception
# mid-loop) the final summary never lands and pr=N is invisible to log parse —
# forcing every job-mode run onto the D.5.1.2 GH-fallback subprocess path.
#
# `run_initiative` now also emits a `--- pr_open pr=N repo=...` line the
# moment a PR URL appears in any tool result, giving log-parse a second
# emit point that fires *before* any blocking wait. Contract:
#
#   - marker only (no final summary)     → pr captured from marker
#   - marker AND summary with pr=N       → summary wins (canonical contract)
#   - neither marker nor summary pr=N    → pr is None; reconciler then falls
#                                          through to the D.5.1.2 GH fallback


def test_parse_summary_extracts_pr_from_pr_open_marker_when_summary_lacks_pr() -> None:
    """The headline win — log has the early marker but the final summary
    line carries no pr= (e.g. agent exited via exception before the post-loop
    emit). Reconciler still recovers the PR from log parse without paying for
    a `gh pr list` subprocess fork on the steady-state path."""
    log = (
        'opening PR...\n'
        '--- pr_open pr=42 repo=mikelear/example-svc\n'
        'iterating on lint...\n'
        '--- turns=8  in=100  out=200  cost=$0.40\n'
    )
    turns, cost, pr_number = _parse_summary(log)
    assert turns == 8
    assert cost == 0.40
    assert pr_number == 42


def test_parse_summary_summary_pr_wins_over_pr_open_marker() -> None:
    """Precedence guard: when both signals are present (the healthy-finish
    case) the final summary's pr is canonical. In practice both carry the
    same number, but pinning the order here prevents a future maintainer
    from accidentally flipping it and degrading to a marker-priority world
    that's harder to reason about."""
    log = '--- pr_open pr=42 repo=mikelear/example-svc\n--- turns=5  in=0  out=0  cost=$0.10  pr=42\n'
    _, _, pr_number = _parse_summary(log)
    assert pr_number == 42


def test_parse_summary_returns_none_when_neither_marker_nor_summary_pr() -> None:
    """No early marker AND the summary line lacks pr= (the pre-D.5.1.4 shape
    on an abnormal exit). _parse_summary returns pr=None; the reconciler
    then falls through to the D.5.1.2 ``_lookup_pr_by_branch`` GH fallback —
    proven by the `test_reconcile_once_uses_gh_fallback_when_log_pr_missing`
    integration test below."""
    log = '--- turns=3  in=0  out=0  cost=$0.05\n'
    _, _, pr_number = _parse_summary(log)
    assert pr_number is None


def test_parse_summary_pr_open_marker_only_no_summary_line_at_all() -> None:
    """Edge case: pod was SIGTERM-killed so fast that ONLY the pr_open marker
    landed in the log tail — no summary line at all. We still recover the
    PR; turns + cost remain None (legitimately unknown, since no summary
    was ever emitted). Better than dropping pr_number on the floor."""
    log = 'starting...\n--- pr_open pr=777 repo=mikelear/example-svc\n(pod killed here)\n'
    turns, cost, pr_number = _parse_summary(log)
    assert turns is None
    assert cost is None
    assert pr_number == 777


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
    # Single final-summary line is sufficient to exercise the reconciler's
    # wiring (the multi-summary aggregation semantics are covered by
    # dedicated ``_parse_summary`` tests above). Under sum semantics the
    # single-summary case is unchanged from the pre-fix behaviour — that's
    # the backwards-compat guarantee in the parser docstring.
    core.read_namespaced_pod_log = AsyncMock(
        return_value=(
            'agent prose\n'
            'PR opened: https://github.com/mikelear/leartech-mortgages-gw/pull/513\n'
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
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(return_value=SimpleNamespace(status='queued', pr_number=None, pr_repo=None)),
        ),
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


# ---------------------------------------------------------------------------
# D.5.2 — self_retrospect hook on Job-mode terminal+success
# ---------------------------------------------------------------------------
#
# Phase F: every run is Job-mode and reaches terminal state through the
# reconciler. The reconciler is the only signal path for completion, so
# without an explicit hook here the post-success Issue is never filed.
# These tests pin the contract:
#
#   - complete   -> retrospect fires (with run_id)
#   - failed     -> retrospect does NOT fire (only success runs)
#   - already-terminal row -> retrospect does NOT fire (idempotent skip)
#   - retrospect raises -> reconciler logs + continues; row update still
#     succeeds; next pass doesn't retry retrospect (idempotent)


def _patch_k8s_for_reconcile(
    batch: Any, core: Any, mock_config: Any, mock_api_client_cls: Any, mock_client_mod: Any
) -> None:
    """Common boilerplate to wire the K8s mocks for reconcile_once."""
    mock_config.load_incluster_config = MagicMock()
    mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    mock_client_mod.BatchV1Api.return_value = batch
    mock_client_mod.CoreV1Api.return_value = core


@pytest.mark.asyncio
async def test_reconcile_once_runs_self_retrospect_on_complete(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """terminal=='complete' on a non-terminal DB row must trigger
    _run_self_retrospect with the run_id (Phase F: reconciler is the only
    signal path)."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-success-1', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name='run-success-1-pod'))],
        ),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value='--- turns=3  in=10  out=20  cost=$0.05  pr=42\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='running'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()) as mock_retrospect,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    mock_retrospect.assert_awaited_once_with('run-success-1')


@pytest.mark.asyncio
async def test_reconcile_once_does_not_run_self_retrospect_on_failed(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """terminal=='failed' must NOT trigger retrospect — the asyncio path only
    fires it when exit_code==0, and Job-mode follows the same contract."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-failed-1', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='p'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=2  in=0  out=0  cost=$0.0\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(return_value=SimpleNamespace(status='running', pr_number=None, pr_repo=None)),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()) as mock_retrospect,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    assert mock_update.await_args.kwargs['status'] == 'failed'
    mock_retrospect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_once_already_terminal_row_does_not_run_retrospect(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """A row already at status='complete' is skipped before reaching the
    retrospect hook — idempotency guarantee. Repeated reconciler passes on
    the same terminal Job must not re-fire the LLM call."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-already-complete', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='complete'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()) as mock_retrospect,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 0
    mock_update.assert_not_called()
    mock_retrospect.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_once_swallows_self_retrospect_exception(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If _run_self_retrospect raises (LLM down, GH 5xx, etc.), the reconciler
    must log + continue: the row update has already succeeded and the next
    pass will see the row as terminal and skip retrospect entirely (no retry
    loop, no resurrected 'queued' state)."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-retrospect-blows', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='p'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value='--- turns=4  in=0  out=0  cost=$0.10  pr=99\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=SimpleNamespace(status='running'))),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch(
            'gate.agent.job_reconciler._run_self_retrospect',
            new=AsyncMock(side_effect=RuntimeError('llm provider down')),
        ) as mock_retrospect,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        # Must NOT raise — best-effort retrospect.
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    mock_retrospect.assert_awaited_once_with('run-retrospect-blows')


# ---------------------------------------------------------------------------
# D.crash-detection — crash sticky on hard-terminate (OOMKilled, Error, etc.)
# ---------------------------------------------------------------------------
#
# The preStop hook (D.7) only fires on graceful SIGTERM. For hard pod kills
# the reconciler is the only signal path that can surface the crash to the
# PR thread. Tests pin the contract:
#
#   - failed + OOMKilled + record has PR → sticky posted, body contains
#     "OOMKilled" + log tail
#   - failed + record.pr_number is None  → no sticky posted (only logged)
#   - failed + pod log unavailable       → partial sticky still posted
#   - complete (not failed)              → NO crash sticky path entered
#   - failed + gh post raises            → reconciler continues; row update
#                                          + retrospect path unaffected


def _make_failed_pod(name: str, exit_reason: str) -> SimpleNamespace:
    """Build a pod-like object whose containerStatuses[0] reports a terminal
    state with the given exit reason — matches the K8s shape that
    ``_fetch_pod_crash_info`` reads."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        terminated=SimpleNamespace(reason=exit_reason),
                    ),
                ),
            ],
        ),
    )


def test_build_job_crash_sticky_body_includes_marker_reason_and_log_tail() -> None:
    body = _build_job_crash_sticky_body(
        run_id='abc123',
        exit_reason='OOMKilled',
        log_tail='line a\nline b\nline c',
    )
    assert '<!-- leartech-agent-run -->' in body
    assert '## ⚠ Agent Job pod crashed' in body
    assert 'abc123' in body
    assert 'OOMKilled' in body
    assert 'line a' in body
    assert 'line c' in body


def test_build_job_crash_sticky_body_handles_empty_log_tail() -> None:
    """Pod GC'd or log driver flaked → sticky still rendered with a placeholder
    rather than dropping the comment entirely. The exit reason carries the
    primary signal anyway."""
    body = _build_job_crash_sticky_body(run_id='r1', exit_reason='Error', log_tail='')
    assert '(no log output captured)' in body
    assert 'Error' in body


@pytest.mark.asyncio
async def test_reconcile_once_posts_crash_sticky_on_oomkilled(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """OOMKilled + parsed pr=N + record.pr_repo set → crash sticky posted,
    body cites OOMKilled and contains the captured log tail."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-oom-1', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    failed_pod = _make_failed_pod('run-oom-1-pod', 'OOMKilled')
    core.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[failed_pod]))
    core.read_namespaced_pod_log = AsyncMock(
        return_value='compiling...\nMemoryError: out of memory\n--- turns=3  in=0  out=0  cost=$0.05  pr=99\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(status='running', pr_number=None, pr_repo='mikelear/example-svc'),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_post.assert_called_once()
    kwargs = mock_post.call_args.kwargs
    assert kwargs['qualified_repo'] == 'mikelear/example-svc'
    assert kwargs['pr_number'] == 99  # picked up from the parsed summary line
    body = kwargs['body']
    assert 'OOMKilled' in body
    assert 'MemoryError: out of memory' in body
    assert 'run-oom-1' in body


@pytest.mark.asyncio
async def test_reconcile_once_skips_crash_sticky_when_no_pr_resolved(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """Failed Job with neither parsed-pr nor record.pr_number → don't post a
    crash sticky (no PR to post to). Row update still happens so the catalog
    flips queued → failed; the crash is just silent on the GH side."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-no-pr', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_make_failed_pod('p', 'Error')]),
    )
    # Log carries no `pr=N` summary line — agent crashed before opening a PR.
    core.read_namespaced_pod_log = AsyncMock(return_value='boom\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(return_value=SimpleNamespace(status='running', pr_number=None, pr_repo=None)),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_once_posts_partial_crash_sticky_when_log_unavailable(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """Pod log endpoint flakes during crash-info fetch → sticky still posts
    with the placeholder log block. The row update is independent (uses the
    earlier 200-line tail) and already succeeded above."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-log-flake', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    failed_pod = _make_failed_pod('run-log-flake-pod', 'OOMKilled')

    # First call: succeeds (for the summary-parse path with tail=200).
    # Second call: raises (for the crash-info path with tail=50).
    pod_calls = AsyncMock(side_effect=[SimpleNamespace(items=[failed_pod]), SimpleNamespace(items=[failed_pod])])
    log_calls = AsyncMock(
        side_effect=['--- turns=3  in=0  out=0  cost=$0.05  pr=77\n', RuntimeError('logs unreadable')],
    )
    core.list_namespaced_pod = pod_calls
    core.read_namespaced_pod_log = log_calls

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(status='running', pr_number=None, pr_repo='mikelear/example-svc'),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_post.assert_called_once()
    body = mock_post.call_args.kwargs['body']
    # Partial sticky: exit reason captured (pod object came through), log tail empty.
    assert 'OOMKilled' in body
    assert '(no log output captured)' in body


@pytest.mark.asyncio
async def test_reconcile_once_does_not_post_crash_sticky_on_complete(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """The complete branch must NOT invoke the crash sticky path — even when a
    PR is resolved. Crash sticky is exclusively a failed-branch signal."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-complete-1', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='p'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value='--- turns=3  in=0  out=0  cost=$0.05  pr=42\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(status='running', pr_number=None, pr_repo='mikelear/example-svc'),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_post.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_once_swallows_crash_sticky_post_failure(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If `_post_crash_sticky` raises (gh down, network blip), the reconciler
    must log + continue. The row has already flipped to 'failed'; the next
    pass sees a terminal row and short-circuits — no resurrected 'running'
    state, no retry storm."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-gh-down', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_make_failed_pod('p', 'OOMKilled')]),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value='--- turns=3  in=0  out=0  cost=$0.05  pr=55\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(status='running', pr_number=None, pr_repo='mikelear/example-svc'),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch(
            'gate.agent.job_reconciler._post_crash_sticky',
            side_effect=RuntimeError('gh API 500'),
        ) as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        # Must not raise.
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_update.assert_awaited_once()
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_reconcile_once_uses_record_pr_number_when_log_lacks_pr(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If the parsed summary has no `pr=N` (e.g. agent crashed right after
    opening the PR but before the final summary), fall back to whatever's
    already on the DB row. Maximises sticky coverage."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-fallback-pr', [('Failed', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_make_failed_pod('p', 'Error')]),
    )
    # Summary line WITHOUT pr=N — pr_number from parsing is None.
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=2  in=0  out=0  cost=$0.0\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                # record.pr_number IS set — earlier write path captured it.
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=123,
                    pr_repo='mikelear/example-svc',
                    initiative='fallback-pr-test',
                    branch='agent/fallback-pr-test',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        # D.5.1.1 fallback now runs whenever the log lacks `pr=N`. Force it
        # to a no-op (returns None) here so the test still exercises the
        # crash-sticky path's fallback-to-record.pr_number behaviour, not
        # the GH-side branch lookup (covered by dedicated tests below).
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=None),
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs['pr_number'] == 123


# ---------------------------------------------------------------------------
# D.5.1.1 — GH-side PR fallback when log parse misses `pr=N`
# ---------------------------------------------------------------------------
#
# Today every job-mode run completes with `pr=None` even though a PR exists,
# because the agent's `wait_for_terminal` blocks past the point where the
# final `--- turns=...  pr=N` summary line would be emitted. The reconciler
# falls back to `gh pr list --head agent/<initiative>` to recover the
# pr_number from GitHub's side. Without this, D.5.2's self_retrospect path
# silently skips every job-mode run with "pr_repo/pr_number not set".
#
# Contract:
#   - log pr=N present                     → use that, skip GH lookup
#   - log pr=None + GH returns a PR        → pr_number set from GH
#   - log pr=None + GH returns empty array → pr_number stays None
#   - log pr=None + GH errors (timeout, non-zero exit) → graceful, pr_number stays None


@pytest.mark.asyncio
async def test_reconcile_once_uses_gh_fallback_when_log_pr_missing(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """Log lacks `pr=N` but record.pr_repo + branch are set → reconciler
    invokes `_lookup_pr_by_branch` with the DB-persisted branch (D.5.1.2),
    captures the returned PR number, and persists it via update(). This is
    the headline behaviour that unblocks D.5.2's self_retrospect path for
    job-mode runs."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-gh-fallback', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    # Summary line WITHOUT pr=N — wait_for_terminal swallowed the final emit.
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=5  in=10  out=20  cost=$0.30\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo='mikelear/example-svc',
                    initiative='my-cool-feature',
                    branch='agent/my-cool-feature',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=789) as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    # D.5.1.2 — the branch is read from the DB row (record.branch), not
    # re-derived from record.initiative. This is the contract that fixes
    # the doubled-prefix bug (`agent/agent-<name>`) that D.5.1.1 shipped.
    mock_lookup.assert_called_once_with('mikelear/example-svc', 'agent/my-cool-feature', 'run-gh-fallback')
    mock_update.assert_awaited_once()
    kwargs = mock_update.await_args.kwargs
    assert kwargs['pr_number'] == 789


@pytest.mark.asyncio
async def test_reconcile_once_uses_pr_open_marker_skips_gh_fallback(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """D.5.1.4 integration — log has the early ``--- pr_open pr=N`` marker but
    no final summary pr= (e.g. agent exited via SDK exception before the
    post-loop summary emit). Reconciler must capture pr from the marker via
    ``_parse_summary`` and skip the GH-side ``_lookup_pr_by_branch`` entirely.

    This is the headline win: on the abnormal-exit path that used to ALWAYS
    fork ``gh pr list``, log-parse now suffices, sparing the reconciler a
    subprocess call per failed-finish run."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-pr-open-marker', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    # Marker present, summary without pr= — the canonical D.5.1.4 case.
    core.read_namespaced_pod_log = AsyncMock(
        return_value=(
            '--- pr_open pr=987 repo=mikelear/example-svc\niterating...\n--- turns=4  in=10  out=20  cost=$0.15\n'
        ),
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo='mikelear/example-svc',
                    initiative='pr-open-test',
                    branch='agent/pr-open-test',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    # The marker captured pr=987 — GH fallback must NOT be invoked.
    mock_lookup.assert_not_called()
    assert mock_update.await_args.kwargs['pr_number'] == 987


@pytest.mark.asyncio
async def test_reconcile_once_skips_gh_fallback_when_log_has_pr(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If the log parse already produced a pr_number, we MUST NOT invoke the
    GH fallback — the log is the authoritative source. Skipping the subprocess
    call keeps reconciler cycles cheap on the healthy path (no per-pass `gh`
    fork+exec) and avoids any chance of overriding a parsed pr with a
    different GH answer."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-log-has-pr', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(
        return_value='--- turns=4  in=10  out=20  cost=$0.20  pr=321\n',
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo='mikelear/example-svc',
                    initiative='log-wins',
                    branch='agent/log-wins',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_lookup.assert_not_called()
    assert mock_update.await_args.kwargs['pr_number'] == 321


@pytest.mark.asyncio
async def test_reconcile_once_gh_fallback_returning_none_leaves_pr_none(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """Log lacks pr AND GH returns no matching PR (e.g. no open PR for the
    branch yet, or the branch was never pushed) → pr_number stays None. The
    update still writes status + turns + cost so the row is no longer stuck
    at 'running'; self_retrospect will then skip with its own pr-missing
    guard, exactly as today."""
    batch, core = _mock_k8s_ok
    job = _make_job('run-gh-empty', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=2  in=0  out=0  cost=$0.0\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo='mikelear/example-svc',
                    initiative='no-pr-yet',
                    branch='agent/no-pr-yet',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch', return_value=None) as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_lookup.assert_called_once()
    assert mock_update.await_args.kwargs['pr_number'] is None


@pytest.mark.asyncio
async def test_reconcile_once_skips_gh_fallback_when_record_branch_missing(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """D.5.1.2 — if record.branch is None (pre-migration row from before the
    0004_branch_column.sql migration ran), the reconciler must NOT attempt
    the GH-side branch fallback. The original D.5.1.1 code derived the branch
    from ``f'agent/{record.initiative}'`` which baked in the wrong convention
    (doubled `agent/` prefix on real initiative names) and always missed.

    The fix is to read the authoritative branch off the DB row — but old rows
    written before the column existed will carry NULL. For those we have to
    fall through to log-parse only; making up a branch name and hoping it
    matches is what got us into this bug.

    Net effect: log lacks `pr=N` AND record.branch is None → pr_number stays
    None, no subprocess invocation. Row update still completes (queued →
    terminal) so the catalog doesn't sit stuck.
    """
    batch, core = _mock_k8s_ok
    job = _make_job('run-no-branch', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    # Summary lacks `pr=N` — exactly the case where D.5.1.1 would fall back.
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=3  in=0  out=0  cost=$0.10\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo='mikelear/example-svc',
                    initiative='pre-migration-row',
                    branch=None,  # ← the pre-migration NULL case
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    # The headline assertion: no subprocess call attempted for NULL-branch rows.
    mock_lookup.assert_not_called()
    # Row STILL flips to terminal so the catalog isn't stuck — pr_number just
    # remains None, and self_retrospect's own guard skips it.
    mock_update.assert_awaited_once()
    kwargs = mock_update.await_args.kwargs
    assert kwargs['status'] == 'complete'
    assert kwargs['pr_number'] is None


@pytest.mark.asyncio
async def test_reconcile_once_skips_gh_fallback_when_pr_repo_missing(
    _mock_k8s_ok: Any,  # type: ignore[valid-type]
) -> None:
    """If record.pr_repo is None (initiative never resolved to a repo; e.g.
    the agent crashed before the loader ran), the GH fallback has no target
    to query and must be skipped entirely. No subprocess invocation, no
    AttributeError on missing initiative — just leave pr_number as None.

    The ``record.branch`` happens to be set here, but the ``pr_repo is None``
    short-circuit must dominate: no repo → no fallback regardless of branch.
    """
    batch, core = _mock_k8s_ok
    job = _make_job('run-no-repo', [('Complete', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='pod'))]),
    )
    core.read_namespaced_pod_log = AsyncMock(return_value='--- turns=1  in=0  out=0  cost=$0.0\n')

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch(
            'gate.agent.job_reconciler.get_record',
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status='running',
                    pr_number=None,
                    pr_repo=None,
                    initiative='no-repo-set',
                    branch='agent/no-repo-set',
                ),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        patch('gate.agent.job_reconciler._lookup_pr_by_branch') as mock_lookup,
        patch('gate.agent.job_reconciler._run_self_retrospect', new=AsyncMock()),
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_lookup.assert_not_called()


# ---------------------------------------------------------------------------
# _lookup_pr_by_branch — direct unit tests over the subprocess wrapper
# ---------------------------------------------------------------------------


def test_lookup_pr_by_branch_returns_number_from_gh_json() -> None:
    """Happy path: `gh pr list ... --json number` emits a single-row JSON array.
    The helper returns the integer number for the YAML-declared branch
    (D.5.1.2 — caller passes the persisted ``record.branch`` directly)."""
    fake_result = SimpleNamespace(returncode=0, stdout='[{"number": 456}]', stderr='')
    with patch('gate.agent.job_reconciler.subprocess.run', return_value=fake_result) as mock_run:
        pr = _lookup_pr_by_branch('mikelear/example-svc', 'agent/add-thing', 'run-xyz')
    assert pr == 456
    args = mock_run.call_args.args[0]
    # The branch passed in is used VERBATIM as `--head` — no string-templating
    # in the helper any more; that's why we persist `record.branch` in the
    # first place (D.5.1.2 motivation).
    assert '--head' in args
    assert args[args.index('--head') + 1] == 'agent/add-thing'
    assert '--state' in args
    assert args[args.index('--state') + 1] == 'open'


def test_lookup_pr_by_branch_returns_none_on_empty_array() -> None:
    """No matching open PR for the branch → returns None. Callers leave
    pr_number unset; self_retrospect's own pr-missing guard then applies."""
    fake_result = SimpleNamespace(returncode=0, stdout='[]', stderr='')
    with patch('gate.agent.job_reconciler.subprocess.run', return_value=fake_result):
        assert _lookup_pr_by_branch('mikelear/example-svc', 'agent/nope', 'run-xyz') is None


def test_lookup_pr_by_branch_returns_none_on_nonzero_exit() -> None:
    """`gh` returned non-zero (auth missing, network blip, rate-limit) →
    swallow + return None. The reconciler logs at DEBUG so the failure is
    observable but doesn't poison the row update."""
    fake_result = SimpleNamespace(returncode=1, stdout='', stderr='auth required')
    with patch('gate.agent.job_reconciler.subprocess.run', return_value=fake_result):
        assert _lookup_pr_by_branch('mikelear/example-svc', 'agent/whatever', 'run-xyz') is None


def test_lookup_pr_by_branch_returns_none_on_subprocess_exception() -> None:
    """Timeout, FileNotFoundError (gh missing), any other subprocess exception
    must be swallowed — the fallback is best-effort and must never bubble up
    to abort the reconciler pass."""
    with patch('gate.agent.job_reconciler.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='gh', timeout=10)):
        assert _lookup_pr_by_branch('mikelear/example-svc', 'agent/whatever', 'run-xyz') is None


def test_lookup_pr_by_branch_returns_none_on_invalid_json() -> None:
    """`gh` succeeded but stdout isn't valid JSON (corrupted pipe, partial
    write) → return None rather than raising."""
    fake_result = SimpleNamespace(returncode=0, stdout='not json {{{', stderr='')
    with patch('gate.agent.job_reconciler.subprocess.run', return_value=fake_result):
        assert _lookup_pr_by_branch('mikelear/example-svc', 'agent/whatever', 'run-xyz') is None
