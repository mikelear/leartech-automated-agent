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
    _build_job_crash_sticky_body,
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
    log = 'Existing PR for context: https://github.com/owner/repo/pull/100\n--- turns=3  in=10  out=20  cost=$0.05\n'
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
# The asyncio path's `_run_and_track` fires `_run_self_retrospect` after
# writing status='complete'. Job-mode runs reach terminal state through the
# reconciler (D.4 task=None branch never executes `_run_and_track`), so
# without an explicit hook here the post-success Issue is never filed —
# silent regression vs the asyncio path. These tests pin the contract:
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
    _run_self_retrospect with the run_id (Job-mode parity with _run_and_track)."""
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
                return_value=SimpleNamespace(status='running', pr_number=123, pr_repo='mikelear/example-svc'),
            ),
        ),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()),
        patch('gate.agent.job_reconciler._post_crash_sticky') as mock_post,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    assert count == 1
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs['pr_number'] == 123
