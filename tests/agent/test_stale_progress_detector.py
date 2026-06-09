"""V5 D2.1 — unit tests for the stale-progress detector.

Pins the contract for ``gate.agent.run_driver.detect_pod_stuck_image_pull``:
when a run's row is older than ``STALE_PROGRESS_THRESHOLD_S`` AND the
agent has not executed its first turn (``started_executing_at IS
NULL``) AND the backing pod is waiting in one of the well-known
image-pull failure reasons, the detector must return
``pod_stuck_<reason>``. In every other case the detector must return
``None`` so the reconciler's normal loop body fires.

The V4 95-minute stall (Job ``8b837153bfda``) is the headline scenario
this watchdog short-circuits — the test names + parametrisation pin
each invariant separately so a future regression on any guard surfaces
as a specific failed test rather than a single noisy "detector broke".

Module is LLM-free, deterministic, fast. Mocks the kubernetes_asyncio
boundary at ``core.list_namespaced_pod`` via the pod-like
``SimpleNamespace`` fixtures.

Memory: ``feedback_async_tests_need_event_not_sleep`` — there's no
async waiting in this module (no race to coordinate), so plain
``pytest.mark.asyncio`` + ``await`` suffices; the memory's relevance
here is just "don't sleep — use the explicit ``now=`` injection".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from gate.agent.run_driver import (
    DEFAULT_STALE_PROGRESS_THRESHOLD_S,
    IMAGE_PULL_FAILURE_REASONS,
    detect_pod_stuck_image_pull,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_record(
    *,
    status: str = 'running',
    started_at: datetime | None = None,
    started_executing_at: datetime | None = None,
    age_seconds: int | None = None,
    now: datetime | None = None,
) -> SimpleNamespace:
    """Build a duck-typed run record matching the detector's contract.

    ``age_seconds`` is a shorthand: when set, ``started_at`` is derived
    as ``now - age_seconds`` so each test reads top-to-bottom without
    repeating the same ``datetime.now(UTC) - timedelta(...)`` boilerplate.
    """
    when = now if now is not None else datetime.now(UTC)
    if age_seconds is not None:
        started_at = when - timedelta(seconds=age_seconds)
    if started_at is None:
        started_at = when
    return SimpleNamespace(
        status=status,
        started_at=started_at,
        started_executing_at=started_executing_at,
    )


def _make_pod_waiting(reason: str | None) -> SimpleNamespace:
    """Build a pod-like object with the given ``waiting.reason``.

    ``None`` produces a pod whose container is NOT in a waiting state
    (used by the negative-case tests that exercise the "no reason
    visible — not stuck" path).
    """
    if reason is None:
        return SimpleNamespace(
            status=SimpleNamespace(
                container_statuses=[
                    SimpleNamespace(state=SimpleNamespace(waiting=None, terminated=None)),
                ],
            ),
        )
    return SimpleNamespace(
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        waiting=SimpleNamespace(reason=reason),
                        terminated=None,
                    ),
                ),
            ],
        ),
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path: stale + image-pull-failure reason → fires
# ---------------------------------------------------------------------------


def test_detector_marks_failed_when_image_pull_backoff_and_stale() -> None:
    """The headline scenario the V5 D2.1 watchdog short-circuits.

    ``started_executing_at IS None`` (agent never reached its first
    turn), row age 1200s > threshold 600s, pod waiting with reason
    'ImagePullBackOff' → detector returns 'pod_stuck_ImagePullBackOff'.
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=1200, started_executing_at=None, now=now)
    pod = _make_pod_waiting('ImagePullBackOff')

    reason = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert reason == 'pod_stuck_ImagePullBackOff'


# ---------------------------------------------------------------------------
# Test 2 — agent already executing → never fires
# ---------------------------------------------------------------------------


def test_detector_ignores_when_started_executing_at_is_set() -> None:
    """If the agent has reached its first turn, the pod's transient
    waiting state is irrelevant by definition — the container is
    Running, not Waiting. The detector must NOT fire even if the
    waiting.reason happens to be a failure reason (would be stale
    data anyway since the container has moved on).

    This is the key V5 D2.2 invariant: ``started_executing_at`` is
    the unambiguous "agent is alive" signal. Without it, a run whose
    first turn fired but whose pod object hasn't been updated would
    be false-failed.
    """
    now = datetime.now(UTC)
    record = _make_record(
        age_seconds=1200,
        started_executing_at=now - timedelta(seconds=300),  # ran ~5min ago
        now=now,
    )
    pod = _make_pod_waiting('ImagePullBackOff')

    reason = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert reason is None


# ---------------------------------------------------------------------------
# Test 3 — age below threshold → never fires
# ---------------------------------------------------------------------------


def test_detector_ignores_when_under_threshold() -> None:
    """The threshold (default 600s, knob via STALE_PROGRESS_THRESHOLD_S)
    protects against false-positives during the normal image-pull
    window. A pod that JUST started can legitimately sit in
    ``ContainerCreating`` for tens of seconds before the image cache
    hit — even an ImagePullBackOff that resolves quickly (registry
    transient) shouldn't trip the detector immediately.

    Concretely: age 100s, threshold 600s, pod stuck in
    ImagePullBackOff → still None (give it more time).
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=100, started_executing_at=None, now=now)
    pod = _make_pod_waiting('ImagePullBackOff')

    reason = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert reason is None


# ---------------------------------------------------------------------------
# Test 4 — parametrise across every documented image-pull failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'reason',
    sorted(IMAGE_PULL_FAILURE_REASONS),
    ids=lambda r: r,
)
def test_detector_handles_each_image_pull_failure_reason(reason: str) -> None:
    """Each of the four documented K8s container-waiting reasons that
    indicate "no progress without operator action" must trip the
    detector. The set is finite + well-known (see kubelet's
    ``pkg/kubelet/container/sync_result.go`` for the canonical
    enumeration); adding a new reason is a code change to
    ``IMAGE_PULL_FAILURE_REASONS``.
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=900, started_executing_at=None, now=now)
    pod = _make_pod_waiting(reason)

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert detected == f'pod_stuck_{reason}'


# ---------------------------------------------------------------------------
# Test 5 — benign waiting reason → never fires
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'reason',
    ['ContainerCreating', 'PodInitializing', 'CrashLoopBackOff', None],
    ids=['ContainerCreating', 'PodInitializing', 'CrashLoopBackOff', 'no-waiting-state'],
)
def test_detector_passes_through_when_waiting_reason_is_benign(reason: str | None) -> None:
    """Reasons outside ``IMAGE_PULL_FAILURE_REASONS`` mean either
    "still working" (ContainerCreating, PodInitializing) or "container
    is crashing, which is a different problem path" (CrashLoopBackOff
    — surfaces via the Job's terminal Failed condition, not this
    watchdog). ``None`` represents a pod whose container isn't in a
    waiting state at all (could be running, could be terminated).

    In every case the detector returns None so the reconciler's
    normal terminal-state loop handles things.
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=1200, started_executing_at=None, now=now)
    pod = _make_pod_waiting(reason)

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert detected is None


# ---------------------------------------------------------------------------
# Test 6 — already-terminal row is not re-failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'status',
    ['complete', 'failed', 'cancelled', 'orphaned', 'timed_out'],
)
def test_detector_does_not_double_fail(status: str) -> None:
    """Idempotency guard. If a row is already in a terminal state
    (e.g. another reconcile pass flipped it failed last poll, or the
    cancel endpoint set cancelled), the detector must NOT re-fail it
    even with a stuck pod still visible. The status check is the
    cheap first-line idempotency guard; without it the detector
    would emit a fresh `failed` write on every reconcile pass
    forever, polluting the DB and the crash-sticky path.
    """
    now = datetime.now(UTC)
    record = _make_record(
        status=status,
        age_seconds=1200,
        started_executing_at=None,
        now=now,
    )
    pod = _make_pod_waiting('ImagePullBackOff')

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert detected is None


# ---------------------------------------------------------------------------
# Test 7 — None pod (lookup failed) → never fires
# ---------------------------------------------------------------------------


def test_detector_returns_none_when_pod_lookup_failed() -> None:
    """When the reconciler's pod lookup raises or returns no items
    (transient K8s API blip, label-selector race), ``_fetch_pod_for_run``
    returns None. The detector must treat that as "can't see the pod —
    skip this pass" rather than fabricating a failure — better to wait
    one more reconcile cycle for the API to recover than to false-fail
    a healthy run on a flaky list call.
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=1200, started_executing_at=None, now=now)

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=None,
        threshold_seconds=600,
        now=now,
    )

    assert detected is None


# ---------------------------------------------------------------------------
# Test 8 — dict-shaped pod (F5 MCP response) is accepted
# ---------------------------------------------------------------------------


def test_detector_accepts_dict_shaped_pod_state_from_k8s_mcp() -> None:
    """The initiative pseudocode wires through ``k8s_mcp.get_pod_state``
    which returns a dict with a top-level ``waiting_reason`` key.
    Today the reconciler's caller hands the detector a
    kubernetes_asyncio pod object (the existing pre-MCP path); when
    F5/W1 ship the dict shape will start flowing through.

    The detector must accept both shapes without branching at the
    call site — same downstream classification path.
    """
    now = datetime.now(UTC)
    record = _make_record(age_seconds=1200, started_executing_at=None, now=now)
    pod_state_dict: dict[str, Any] = {
        'waiting_reason': 'ImagePullBackOff',
        'phase': 'Pending',
    }

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=pod_state_dict,
        threshold_seconds=600,
        now=now,
    )

    assert detected == 'pod_stuck_ImagePullBackOff'


# ---------------------------------------------------------------------------
# Test 9 — env var default + override
# ---------------------------------------------------------------------------


def test_detector_uses_env_threshold_when_not_passed_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``threshold_seconds`` is not passed, the detector reads
    ``STALE_PROGRESS_THRESHOLD_S`` from the env. Chart values plumb
    this through ``deployment.yaml`` so operators can tune the
    threshold (e.g. integration tests lower it to 60s) without a
    code change.
    """
    monkeypatch.setenv('STALE_PROGRESS_THRESHOLD_S', '60')
    now = datetime.now(UTC)

    # age 90s — under default 600s but OVER override 60s
    record = _make_record(age_seconds=90, started_executing_at=None, now=now)
    pod = _make_pod_waiting('ImagePullBackOff')

    detected = detect_pod_stuck_image_pull(record=record, pod=pod, now=now)
    assert detected == 'pod_stuck_ImagePullBackOff'


def test_detector_uses_default_threshold_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative env-fallback case. With no env set, the default 600s
    applies — a 90s-old row stays untouched even with a stuck pod.
    """
    monkeypatch.delenv('STALE_PROGRESS_THRESHOLD_S', raising=False)
    assert DEFAULT_STALE_PROGRESS_THRESHOLD_S == 600  # pins the constant
    now = datetime.now(UTC)

    record = _make_record(age_seconds=90, started_executing_at=None, now=now)
    pod = _make_pod_waiting('ImagePullBackOff')

    detected = detect_pod_stuck_image_pull(record=record, pod=pod, now=now)
    assert detected is None


def test_detector_ignores_malformed_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in chart values (``STALE_PROGRESS_THRESHOLD_S=ten``)
    must not silently disable the watchdog — fall back to the
    default so the operational invariant survives mis-configuration.
    """
    monkeypatch.setenv('STALE_PROGRESS_THRESHOLD_S', 'not-an-int')
    now = datetime.now(UTC)

    # age 1200s — over the default 600s, under any sane mis-typed value
    record = _make_record(age_seconds=1200, started_executing_at=None, now=now)
    pod = _make_pod_waiting('ImagePullBackOff')

    detected = detect_pod_stuck_image_pull(record=record, pod=pod, now=now)
    # Default kicks in (600s) and the row is over it → detector fires.
    assert detected == 'pod_stuck_ImagePullBackOff'


# ---------------------------------------------------------------------------
# Test 10 — pod with naive started_at is handled (SQLite vs CNPG TZ shim)
# ---------------------------------------------------------------------------


def test_detector_handles_naive_started_at_datetime() -> None:
    """SQLite (test DB) returns tz-naive datetimes; Postgres always
    tz-aware. The detector normalises to UTC so the age comparison
    is meaningful in both backends. Without this guard, a naive
    datetime would raise ``TypeError`` on the subtraction with the
    tz-aware ``now``, taking down the whole reconciler pass.

    Mirrors the same defensive pattern in ``is_run_stale`` and the
    ``app.state.reconcile_orphaned_runs`` cutoff logic.
    """
    now = datetime.now(UTC)
    naive_started_at = (now - timedelta(seconds=1200)).replace(tzinfo=None)
    record = SimpleNamespace(
        status='running',
        started_at=naive_started_at,
        started_executing_at=None,
    )
    pod = _make_pod_waiting('ImagePullBackOff')

    detected = detect_pod_stuck_image_pull(
        record=record,
        pod=pod,
        threshold_seconds=600,
        now=now,
    )

    assert detected == 'pod_stuck_ImagePullBackOff'
