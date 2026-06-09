"""V5 D2 — state-machine tests for the agent's run-driver + reconciler.

Encodes the invariants the V4 stall broke. The canonical V4 incident:
Job ``8b837153bfda`` was deleted externally, but the DB row stayed at
``running`` for 95 minutes because ``reconcile_once`` only walks K8s
Jobs (which were gone) and never noticed the dangling row. The fix is
to also sweep DB rows in ``running`` and flip ones whose backing Job
has vanished to ``orphaned``.

This module is LLM-free, deterministic, fast. It mocks
``kubernetes_asyncio.client.BatchV1Api`` + ``CoreV1Api`` and the DB
session at the boundary, mirroring the conventions in
``tests/test_job_reconciler.py``.

Note: ``kubernetes_asyncio.config.load_incluster_config`` is SYNCHRONOUS
in the upstream library — use ``MagicMock`` for it, never ``AsyncMock``.
See the ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.

Cross-task synchronisation in async tests uses ``asyncio.Event`` rather
than ``asyncio.sleep`` — sleeps are flaky on CI runners under load.
See the ``feedback_async_tests_need_event_not_sleep`` memory.

## Coverage matrix

| Test                                                       | Status   | Follow-up                                                  |
|------------------------------------------------------------|----------|------------------------------------------------------------|
| job_deleted_externally → orphaned                          | PASS     | V5 D2 (this PR — small fix landed in job_reconciler)       |
| pod stuck ImagePullBackOff > threshold → failed            | PASS     | V5 D2.1 landed — detect_pod_stuck_image_pull in run_driver |
| started_executing_at starts NULL                           | PASS     | V5 D2.2 landed — column + pydantic default                 |
| started_executing_at set on first turn (immutable)         | PASS     | V5 D2.2 landed — mark_first_turn hook in run_driver        |
| reconciler staleness uses started_executing_at not turns=0 | PASS     | V5 D2.2 landed — is_run_stale in run_driver                |
| status transitions only legal (created → running → done)   | XFAIL    | V5 D2.3 transition validation in app.state.update          |

XFAIL tests with ``strict=True`` flip to a hard FAIL the moment the
implementation lands — they're the regression-prevention contract.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gate.agent.job_reconciler import reconcile_once

# ---------------------------------------------------------------------------
# Boilerplate — mirrors tests/test_job_reconciler.py helpers
# ---------------------------------------------------------------------------


def _make_job(name: str, conditions: list[tuple[str, str]]) -> SimpleNamespace:
    """Build a K8s Job-like object with the given conditions list.

    Same shape as ``tests/test_job_reconciler.py::_make_job``; duplicated
    here so this module is self-contained and reads top-to-bottom without
    cross-file helper hunting.
    """
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type=ctype, status=cstatus) for ctype, cstatus in conditions],
        ),
    )


def _make_pod_in_image_pull_backoff(
    name: str,
    reason: str = 'ImagePullBackOff',
    waiting_seconds: int = 900,
) -> SimpleNamespace:
    """Pod-like object whose containerStatus reports a waiting state with the
    given reason. The K8s API surfaces this on the ``waiting`` (not
    ``terminated``) sub-state when the container has never started.

    ``waiting_seconds`` controls how long the pod has been in this state —
    the V5 D2.1 watchdog should only flip past a threshold (600s in the
    YAML spec) to avoid false-positives on transient registry blips.
    """
    started_time = datetime.now(UTC) - timedelta(seconds=waiting_seconds)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, creation_timestamp=started_time),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        waiting=SimpleNamespace(reason=reason),
                        terminated=None,
                    ),
                ),
            ],
            start_time=started_time,
        ),
    )


def _patch_k8s_for_reconcile(
    batch: Any,
    core: Any,
    mock_config: Any,
    mock_api_client_cls: Any,
    mock_client_mod: Any,
) -> None:
    """Wire the kubernetes_asyncio mocks. Duplicated from
    ``tests/test_job_reconciler.py`` for the same reason ``_make_job`` is."""
    mock_config.load_incluster_config = MagicMock()
    mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    mock_client_mod.BatchV1Api.return_value = batch
    mock_client_mod.CoreV1Api.return_value = core


# ---------------------------------------------------------------------------
# Test 1 — V4 stall fix: Job deleted externally must flip row to 'orphaned'
# ---------------------------------------------------------------------------
#
# This is the headline V5 D2 invariant. The V4 incident: Job 8b837153bfda
# was deleted (kubectl, TTL race, or operator cleanup) and the DB row
# stayed in `running` for 95 minutes because the reconciler only walks
# K8s Jobs. With no Job to iterate, the row is invisible.
#
# Fix landed in this PR (small, ~15 LOC) — a parallel sweep over DB rows
# in `running` state flips any whose `job_name` is not in the current
# K8s set to `orphaned` with `error='job_deleted_externally'`.


@pytest.mark.asyncio
async def test_job_deleted_externally_flips_to_orphaned() -> None:
    """V4 stall fix. With NO K8s Job for the row's job_name visible in this
    namespace, the reconciler must flip the row from `running` → `orphaned`
    with a tagged `error` and a non-null `finished_at`.

    Mocks:
      - ``BatchV1Api.list_namespaced_job`` → empty (Job vanished)
      - ``is_db_enabled`` → True (orphan sweep only runs with DB)
      - ``list_runs(status='running')`` → single ghost row
      - ``update`` → AsyncMock (assert kwargs on the orphan call)

    Assertions:
      - status flipped to 'orphaned'
      - error mentions 'job_deleted_externally'
      - finished_at is non-null (and is a tz-aware datetime)
    """
    batch = MagicMock()
    core = MagicMock()
    # The V4 condition: no Jobs in the namespace at all.
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[]))

    ghost_row = SimpleNamespace(
        id='8b837153bfda',
        initiative='v4-stalled-init',
        status='running',
        started_at=datetime.now(UTC) - timedelta(minutes=95),
        finished_at=None,
        pr_number=None,
        pr_repo='mikelear/example-svc',
        turns=0,
        cost_usd=Decimal('0.00'),
        error=None,
        cluster='az',
        created_by=None,
        runtime='job',
        job_name='8b837153bfda',
        branch='agent/v4-stalled',
        updated_at=datetime.now(UTC),
    )

    # No-op async context manager for db_session() — the orphan sweep enters
    # the session but the inner `list_runs` is patched separately to return
    # the ghost row.
    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session', return_value=mock_session_cm),
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[ghost_row])),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        count = await reconcile_once('jx-staging')

    # At least one update fired (cancel-enrich path may also no-op).
    assert count >= 1
    # The orphan update is identifiable by its run_id + status kwarg.
    orphan_calls = [
        call
        for call in mock_update.await_args_list
        if call.args and call.args[0] == '8b837153bfda' and call.kwargs.get('status') == 'orphaned'
    ]
    assert len(orphan_calls) == 1, f'Expected exactly one orphan flip; got {mock_update.await_args_list!r}'

    kwargs = orphan_calls[0].kwargs
    assert kwargs['status'] == 'orphaned'
    assert 'job_deleted_externally' in (kwargs.get('error') or '')
    assert kwargs.get('finished_at') is not None
    assert isinstance(kwargs['finished_at'], datetime)
    assert kwargs['finished_at'].tzinfo is not None


@pytest.mark.asyncio
async def test_running_row_with_live_job_is_not_orphaned() -> None:
    """Negative case for the V4 fix. If the row's job_name IS still present
    in the K8s set, do NOT orphan it — the agent is in-flight, the row's
    'running' state is correct.

    Without this guard, the orphan sweep would false-orphan every
    healthy in-flight run on every reconcile pass.
    """
    batch = MagicMock()
    core = MagicMock()
    # Job IS still in K8s — but it's still in-flight (no terminal condition).
    live_job = _make_job('healthy-run-1', [('Available', 'True')])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[live_job]))

    running_row = SimpleNamespace(
        id='healthy-run-1',
        initiative='healthy',
        status='running',
        started_at=datetime.now(UTC) - timedelta(minutes=2),
        finished_at=None,
        pr_number=None,
        pr_repo='mikelear/example-svc',
        turns=0,
        cost_usd=Decimal('0.00'),
        error=None,
        cluster='az',
        created_by=None,
        runtime='job',
        job_name='healthy-run-1',
        branch='agent/healthy',
        updated_at=datetime.now(UTC),
    )

    mock_session_cm = MagicMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    mock_session_cm.__aexit__ = AsyncMock(return_value=None)

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=True),
        patch('gate.agent.job_reconciler.db_session', return_value=mock_session_cm),
        patch('gate.agent.job_reconciler.list_runs', new=AsyncMock(return_value=[running_row])),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        await reconcile_once('jx-staging')

    # The headline assertion: row was NOT touched by an orphan update.
    orphan_calls = [call for call in mock_update.await_args_list if call.kwargs.get('status') == 'orphaned']
    assert orphan_calls == [], f'Healthy in-flight row was incorrectly orphaned: {orphan_calls!r}'


# ---------------------------------------------------------------------------
# Test 2 — Pod stuck in ImagePullBackOff > 600s must flip to 'failed'
# ---------------------------------------------------------------------------
#
# V5 D2.1 (LANDED). The watchdog lives in
# ``gate.agent.run_driver.detect_pod_stuck_image_pull`` and is wired
# into ``job_reconciler.reconcile_once`` via the new
# ``_detect_and_patch_stuck_pod`` pass. The Job has NO terminal
# condition (restartPolicy=Never + backoffLimit=0 doesn't progress
# the Job to Failed on an image-pull error), so the watchdog looks at
# the pod's waiting.reason + age independently of the Job's status.
#
# This was XFAIL until V5 D2.1 landed; now an integration assertion
# that the wiring still routes through reconcile_once correctly. The
# detector's per-case behaviour is covered exhaustively by
# ``tests/agent/test_stale_progress_detector.py`` — this test pins the
# end-to-end "reconcile_once must invoke the watchdog and patch the
# row when fired" contract.


@pytest.mark.asyncio
async def test_pod_stuck_image_pull_marks_failed() -> None:
    """Pod waiting in ImagePullBackOff for >600s → row flipped to failed
    with `error` capturing the K8s reason.

    The Job has NO terminal condition (it never will — restartPolicy=Never
    + backoffLimit=0 doesn't progress the Job to Failed for an image-pull
    error). The watchdog needs to look at the pod's waiting reason and
    age, independent of the Job's own status.
    """
    batch = MagicMock()
    core = MagicMock()

    # Job alive in K8s, no terminal condition.
    job = _make_job('image-pull-stuck-1', [])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))

    # Pod stuck in ImagePullBackOff for 900s (well past the 600s threshold).
    stuck_pod = _make_pod_in_image_pull_backoff(
        'image-pull-stuck-1-pod', reason='ImagePullBackOff', waiting_seconds=900
    )
    core.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[stuck_pod]))

    # Record must carry started_at (over threshold) + started_executing_at=None.
    # The detector reads BOTH — the V5 D2.2 column is the unambiguous "agent
    # has not executed" signal; without it a future regression could re-fail
    # in-flight runs whose first turn already fired.
    record = SimpleNamespace(
        id='image-pull-stuck-1',
        status='running',
        pr_number=None,
        pr_repo='mikelear/example-svc',
        initiative='img-pull',
        branch='agent/img-pull',
        started_at=datetime.now(UTC) - timedelta(seconds=1200),
        started_executing_at=None,
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=record)),
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=False),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        await reconcile_once('jx-staging')

    failed_calls = [call for call in mock_update.await_args_list if call.kwargs.get('status') == 'failed']
    assert len(failed_calls) == 1, f'Expected one image-pull-watchdog flip; got {mock_update.await_args_list!r}'
    error_msg = failed_calls[0].kwargs.get('error') or ''
    assert 'pod_stuck_ImagePullBackOff' in error_msg


@pytest.mark.asyncio
async def test_pod_stuck_image_pull_skipped_when_under_threshold() -> None:
    """Negative case for the V5 D2.1 wiring. A young Job (started 30s
    ago) with the SAME ImagePullBackOff pod must NOT be flipped — the
    threshold protects against false-positives during the normal
    image-pull window (image cache warm-up + initial container start
    can legitimately take tens of seconds).

    Pairs with ``test_pod_stuck_image_pull_marks_failed`` to prove the
    age guard is wired through reconcile_once, not just available on
    the detector in isolation.
    """
    batch = MagicMock()
    core = MagicMock()

    job = _make_job('image-pull-young-1', [])
    batch.list_namespaced_job = AsyncMock(return_value=SimpleNamespace(items=[job]))

    # Pod stuck in ImagePullBackOff, but only for 30s (well under threshold).
    young_pod = _make_pod_in_image_pull_backoff('image-pull-young-1-pod', reason='ImagePullBackOff', waiting_seconds=30)
    core.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[young_pod]))

    record = SimpleNamespace(
        id='image-pull-young-1',
        status='running',
        pr_number=None,
        pr_repo='mikelear/example-svc',
        initiative='img-pull-young',
        branch='agent/img-pull-young',
        started_at=datetime.now(UTC) - timedelta(seconds=30),  # young
        started_executing_at=None,
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.get_record', new=AsyncMock(return_value=record)),
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=False),
        patch('gate.agent.job_reconciler.update', new=AsyncMock()) as mock_update,
    ):
        _patch_k8s_for_reconcile(batch, core, mock_config, mock_api_client_cls, mock_client_mod)
        await reconcile_once('jx-staging')

    failed_calls = [call for call in mock_update.await_args_list if call.kwargs.get('status') == 'failed']
    assert failed_calls == [], f'Young Job was false-failed by the watchdog: {mock_update.await_args_list!r}'


# ---------------------------------------------------------------------------
# Test 3 — started_executing_at lifecycle: starts NULL on row creation
# ---------------------------------------------------------------------------
#
# XFAIL — V5 D2.2 will add the column + first-turn hook. The lifecycle
# the V4 stall demonstrated we need is:
#
#   created  (queued)              → started_executing_at IS NULL
#   spawned  (job spawned, queued) → started_executing_at IS NULL
#   first turn fires               → started_executing_at = now() (immutable)
#   subsequent turns               → started_executing_at unchanged
#
# The reconciler then uses `started_executing_at` (not `turns == 0`) to
# decide if a row is stale — a row with `turns == 0` but
# `started_executing_at IS NOT NULL` is in-flight (agent has begun but
# hasn't completed a turn yet); a row with `turns == 0` AND
# `started_executing_at IS NULL` AND age > threshold is genuinely stuck
# in pre-execution (image pulling, scheduling delay) and is the
# orphan-eligible shape.


def test_started_executing_at_starts_null() -> None:
    """V5 D2.2 (LANDED). Newly-created `InitiativeRecord` must have
    `started_executing_at IS NULL`.

    The field's semantic: "wall-clock time of the first turn the agent
    actually executed". For a freshly-registered row (the row exists, the
    Job hasn't started yet), this MUST be NULL — confusing
    `started_executing_at` with `started_at` (which is row-creation time)
    is the bug the V4 reconciler had.
    """
    from app.state import InitiativeRecord

    record = InitiativeRecord(
        id='abc123def456',
        initiative='test',
        status='queued',
        started_at=datetime.now(UTC),
    )

    assert record.started_executing_at is None


@pytest.mark.asyncio
async def test_started_executing_at_set_on_first_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V5 D2.2 (LANDED). Calling the first-turn hook MUST set
    ``started_executing_at`` to a non-null tz-aware datetime; subsequent
    calls MUST NOT overwrite it.

    Idempotency is the contract: the hook may be called repeatedly (e.g.
    SDK turn callback fires every iteration) but only the first call
    has effect. This prevents the V4-style bug where ``started_at`` got
    bumped on every turn, defeating the staleness check entirely.

    Run-state plumbing matches the deeper coverage in
    ``tests/agent/test_run_driver_first_turn.py``: register the run
    first (in-memory cache via the DB-less code path), then call the
    hook against that real run_id.
    """
    import app.state as state_module
    from app import db as db_module
    from app.state import InitiativeRecord, register
    from app.state import get as get_record
    from gate.agent.run_driver import mark_first_turn

    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()
    state_module._records.clear()

    run_id = 'first-turn-run-1'
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='first-turn-test',
            status='running',
            started_at=datetime.now(UTC),
        )
    )

    # First call: sets the field.
    wrote_first = await mark_first_turn(run_id)
    assert wrote_first is True

    record_1 = await get_record(run_id)
    assert record_1 is not None
    first_value = record_1.started_executing_at
    assert first_value is not None

    # Second call: must NOT overwrite.
    wrote_second = await mark_first_turn(run_id)
    assert wrote_second is False

    record_2 = await get_record(run_id)
    assert record_2 is not None
    assert record_2.started_executing_at == first_value


def test_reconciler_uses_started_executing_at_not_turns_zero() -> None:
    """Staleness classification:

    - turns == 0 AND started_executing_at IS NOT NULL  → NOT stale
      (agent has begun executing, just hasn't reached the first
      end-of-turn summary yet)
    - turns == 0 AND started_executing_at IS NULL AND age > threshold
                                                      → STALE
      (row created, Job spawned, but the agent process never started
      a turn — image-pull, scheduling delay, or worse)

    The V4 stall would have been correctly classified as stale by this
    logic, because `started_executing_at` would have been NULL while
    the Job sat dead.
    """
    from gate.agent.run_driver import is_run_stale  # type: ignore[import-not-found]

    now = datetime.now(UTC)
    threshold_seconds = 600

    # Case 1: agent began executing — NOT stale even with turns=0
    record_running = SimpleNamespace(
        turns=0,
        started_at=now - timedelta(seconds=1200),
        started_executing_at=now - timedelta(seconds=300),
    )
    assert is_run_stale(record_running, threshold_seconds=threshold_seconds) is False

    # Case 2: agent never started a turn AND row is old — STALE
    record_stale = SimpleNamespace(
        turns=0,
        started_at=now - timedelta(seconds=1200),
        started_executing_at=None,
    )
    assert is_run_stale(record_stale, threshold_seconds=threshold_seconds) is True

    # Case 3: agent never started AND row is young — NOT stale (no panic)
    record_young = SimpleNamespace(
        turns=0,
        started_at=now - timedelta(seconds=30),
        started_executing_at=None,
    )
    assert is_run_stale(record_young, threshold_seconds=threshold_seconds) is False


# ---------------------------------------------------------------------------
# Test 4 — status transitions: only the legal graph is allowed
# ---------------------------------------------------------------------------
#
# XFAIL — V5 D2.3 will add transition validation to `app.state.update`.
# Today, `update()` accepts any status string. The V4 stall demonstrated
# the cost of this: a misordered call sequence (or a bug in a callsite)
# can flip a `complete` row back to `running`, or skip the `running`
# step entirely. Encoding the legal graph as a guard in `update()`
# makes the surface uniform.
#
# Legal transitions:
#   created (queued)  → running
#   running           → complete | failed | orphaned | cancelled | timed_out
#   (terminal states) → (no transitions)


@pytest.mark.xfail(
    strict=True,
    reason='V5 D2.3 — status transition validation not yet added to '
    '`app.state.update`. The follow-up init adds a guard that raises '
    '(or no-ops with a logged warning) on illegal transitions. This '
    'test pins the legal-graph contract.',
)
@pytest.mark.asyncio
async def test_status_transitions_only_legal() -> None:
    """Each illegal transition must raise (or be a no-op); only the
    canonical graph succeeds.

    Parametrised inline rather than via ``@pytest.mark.parametrize`` because
    the test asserts behaviour over a SEQUENCE of update() calls — we want
    a single registered run to walk the legal path then attempt an illegal
    one and verify the row state is preserved.
    """
    from app.state import (
        InitiativeRecord,
        register,
        update,
    )
    from app.state import (
        get as get_record,
    )

    record = InitiativeRecord(
        id='transition-test-1',
        initiative='transition-test',
        status='queued',
        started_at=datetime.now(UTC),
    )
    await register(record)

    # Legal: queued/created → running
    await update('transition-test-1', status='running')
    r = await get_record('transition-test-1')
    assert r is not None and r.status == 'running'

    # Legal: running → complete
    await update('transition-test-1', status='complete')
    r = await get_record('transition-test-1')
    assert r is not None and r.status == 'complete'

    # ILLEGAL: complete → running (terminal states are sticky).
    # Either raises or no-ops with a logged warning; in both cases the
    # row's observable status MUST remain 'complete'.
    try:
        await update('transition-test-1', status='running')
    except ValueError:
        pass  # explicit reject is one acceptable contract
    r = await get_record('transition-test-1')
    assert r is not None and r.status == 'complete', (
        f'Terminal status leaked: row went complete → {r.status if r else "None"}'
    )

    # ILLEGAL: complete → queued (backward jump)
    try:
        await update('transition-test-1', status='queued')
    except ValueError:
        pass
    r = await get_record('transition-test-1')
    assert r is not None and r.status == 'complete'
