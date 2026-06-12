"""V5 D2.2 + V5 D2.1 run-driver state-machine helpers.

This module exposes four surfaces:

- ``mark_first_turn(run_id)`` — async, set-once hook fired by the SDK
  loop in ``gate/agent/initiative.py`` on the **first iteration of the
  SDK message loop**, regardless of the iteration's message type. The
  earlier wire-up gated this on the first ``AssistantMessage`` only,
  which works for the common path (agent reply arrives before the turn
  closes) but lets the per-turn ``update_run_progress`` writeback race
  ahead in edge cases where the SDK happens to yield a different
  message type first — leaving ``started_executing_at`` NULL while
  ``turns`` / ``cost_usd`` advance. Hoisting the call above the
  message-type branching guarantees the timestamp lands before any
  per-turn snapshot can. Sets
  ``initiative_runs.started_executing_at`` to ``now()`` if the column
  is still NULL; subsequent calls are no-ops at the SQL level because
  the WHERE clause is gated on ``started_executing_at IS NULL``. This
  makes the hook safe to call repeatedly without coordination — the
  database is the source of truth, not the in-process flag.

- ``update_run_progress(run_id, turns, cost_usd, last_tool_call)`` —
  per-turn writeback (initiative ``agent-add-per-turn-writeback``).
  Fired after every SDK ``ResultMessage`` to keep
  ``initiative_runs.turns``, ``cost_usd``, and ``last_tool_call``
  current in real time so anyone watching a long-running run sees
  progress instead of NULL for the entire 30–90-minute window. Best
  effort: a DB hiccup is logged at WARN and swallowed so the writeback
  never blocks or crashes the SDK loop. The caller wraps the
  invocation in ``asyncio.create_task`` so even a slow round-trip can
  overlap with the next SDK turn.

- ``is_run_stale(record, threshold_seconds)`` — sync classifier used
  by the reconciler. The corrected staleness rule:

    turns == 0  AND  started_executing_at IS NULL  AND  age > T
        → STALE (agent never executed a first turn)

    turns == 0  AND  started_executing_at IS NOT NULL
        → NOT STALE (agent began executing, hasn't reached the
          first end-of-turn summary yet)

    turns > 0
        → NOT STALE (agent is making progress, regardless of age)

  The V3/V4 staleness probes currently read ``turns == 0`` in
  isolation and mis-classify in-flight runs as stuck. This helper
  replaces those checks at the consumer-init layer.

- ``detect_pod_stuck_image_pull(record, pod, ...)`` — V5 D2.1
  stale-progress detector. Returns a ``pod_stuck_<reason>`` string
  when a run's pod is wedged on an image-pull failure (one of
  ``ImagePullBackOff``, ``ErrImagePull``, ``InvalidImageName``,
  ``CreateContainerConfigError``) AND the agent has not yet executed
  its first turn (``started_executing_at IS NULL``) AND the row's
  age exceeds the configured threshold. Used by the reconciler to
  short-circuit the V4 95-minute stall: previously the K8s Job
  carried no terminal condition for these states, so the reconciler
  walked past it forever; now the detector flips the row to
  ``failed`` within ``STALE_PROGRESS_THRESHOLD_S + 1 poll`` of the
  pod entering the failure state.

Memory: ``feedback_sdk_toolresult_in_usermessage`` — within a turn the
agent's reply is an ``AssistantMessage`` and the tool's response is a
``UserMessage`` carrying a ``ToolResultBlock``. Per-turn detection is
done in ``gate/agent/initiative.py`` using the correct types for each
side. The first-turn hook intentionally avoids picking a single
message type as its detection signal — it fires on the very first
iteration of the SDK loop so a future SDK version that changes message
ordering doesn't silently regress ``started_executing_at``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update as sa_update

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_runs import update_run_progress as _db_update_run_progress
from app.db.models import InitiativeRunRow
from app.state import _records as _in_memory_records

logger = logging.getLogger(__name__)

# V5 D2.1 — finite, well-known set of K8s container-waiting reasons
# that indicate the pod will never make progress without operator
# action (registry auth, image tag fix, secret materialisation). These
# are NOT in ``frozenset``-form a configurable knob — they're the
# documented kubernetes pod-state values whose semantics are baked
# into the cluster. Adding a new reason here is a code change, not
# a values.yaml change.
IMAGE_PULL_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        'ImagePullBackOff',
        'ErrImagePull',
        'InvalidImageName',
        'CreateContainerConfigError',
    },
)

# Default threshold (seconds) for the V5 D2.1 stale-progress detector.
# Overridable via ``STALE_PROGRESS_THRESHOLD_S`` env (chart values.yaml
# plumbs it through deployment.yaml). 600s = 10min, picked so the V4
# 95-minute stall takes ~10min worst-case to terminate; the reconciler
# poll cadence (15s) is well under the threshold so a poll always
# happens between "threshold crossed" and "detector should fire".
DEFAULT_STALE_PROGRESS_THRESHOLD_S = 600

# Statuses considered terminal for the detector's "skip already-failed"
# guard. Duplicates the ``TERMINAL_STATUSES`` constant in
# ``job_reconciler.py`` deliberately — this module is a pure classifier
# and shouldn't import from the reconciler (the reconciler imports
# from this module, not the other way round).
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'},
)


def _utcnow() -> datetime:
    """Module-local clock — overrideable by tests via monkeypatch."""
    return datetime.now(UTC)


async def mark_first_turn(run_id: str) -> bool:
    """Idempotently record the wall-clock time of the agent's first turn.

    Set-once contract: the first invocation sets
    ``initiative_runs.started_executing_at = now()`` for the given
    ``run_id`` if the column is still NULL; every subsequent invocation
    is a no-op at the SQL layer (``WHERE started_executing_at IS NULL``).

    Returns True iff this call actually wrote the timestamp (useful for
    callers that want to log the transition); False if the column was
    already populated or the row does not exist.

    Concurrency: two coroutines racing on the same ``run_id`` will both
    issue the same UPDATE. The IS-NULL guard means whichever transaction
    commits first wins; the second updates 0 rows and returns False.
    This is the idempotency the test contract requires.

    Falls through cleanly when DB is not configured — the in-memory
    record (when present) is updated so unit tests without a DB still
    see the side-effect. Returns False in that mode if the record is
    missing, matching the DB-row-not-found behaviour.
    """
    now = _utcnow()

    # In-memory fast-path — always update the cache so subsequent
    # `app.state.get(run_id)` reads see the new value without a DB
    # round-trip. When the DB is enabled, the canonical truth lives in
    # the DB row; the in-memory cache is just a read-through. Idempotency
    # mirrors the SQL guard: only set the field when currently None.
    in_mem = _in_memory_records.get(run_id)
    wrote_in_memory = False
    if in_mem is not None and in_mem.started_executing_at is None:
        _in_memory_records[run_id] = in_mem.model_copy(
            update={'started_executing_at': now},
        )
        wrote_in_memory = True

    if not is_db_enabled():
        # No DB; the in-memory write (when present) is the only signal.
        # Return whether THIS call actually wrote — preserves the set-once
        # contract for DB-less tests.
        return wrote_in_memory

    async with db_session() as sess:
        result = await sess.execute(
            sa_update(InitiativeRunRow)
            .where(InitiativeRunRow.id == run_id)
            .where(InitiativeRunRow.started_executing_at.is_(None))
            .values(started_executing_at=now),
        )
        # ``result.rowcount`` is reliable on both asyncpg and aiosqlite for
        # simple UPDATE statements. 1 → this call set the column; 0 →
        # either the row is missing or the column was already populated
        # (idempotent no-op). The base ``Result`` type doesn't expose
        # ``rowcount`` in stubs (it's on the more specific
        # ``CursorResult``); the cast is safe for the UPDATE shape we
        # actually issue.
        rowcount = getattr(result, 'rowcount', 0) or 0
        wrote_db: bool = rowcount > 0

    if wrote_db and in_mem is not None and not wrote_in_memory:
        # Edge case: the in-memory record already had the timestamp from
        # a prior call in this process but the DB still showed NULL — keep
        # the in-memory copy authoritative since we just committed.
        _in_memory_records[run_id] = in_mem.model_copy(
            update={'started_executing_at': now},
        )

    return wrote_db or wrote_in_memory


async def update_run_progress(
    run_id: str | None,
    *,
    turns: int,
    cost_usd: float | None,
    last_tool_call: str | None,
) -> bool:
    """Per-turn writeback hook — fired after every SDK ``ResultMessage``.

    Contract (matches the initiative spec):

    - When fired: ONCE PER TURN, immediately after the SDK yields a
      ``ResultMessage`` (the natural end-of-turn boundary).
    - What it writes: ``turns`` (running int), ``cost_usd``
      (cumulative — the SDK's ``ResultMessage.total_cost_usd``, NOT
      the per-turn delta), and ``last_tool_call`` (NAME of the LAST
      tool the agent invoked during this turn, or ``None`` for a
      plain text turn).
    - Failure mode: best effort. A DB hiccup is logged at WARN and
      swallowed; the function never raises. The caller wraps the call
      in ``asyncio.create_task`` so even slow round-trips don't block
      the SDK loop.

    Returns ``True`` iff the writeback actually committed (DB row
    updated OR in-memory record patched). Returns ``False`` on every
    failure path — operators rely on the warning log line, not the
    return value, to diagnose persistent failures.

    DB-disabled mode: if ``LEARTECH_INITIATIVE_DB_DSN`` is unset (the
    laptop CLI path), the hook still patches the in-memory record (if
    present in ``app.state._records``) so DB-less runs observe
    progress through the same surface.

    A ``run_id`` of ``None`` is the explicit "no run row attached"
    signal — laptop runs without ``LEARTECH_RUN_ID`` env. The function
    returns ``False`` immediately in that case (nothing to write).
    """
    if not run_id:
        return False

    # In-memory fast-path — mirrors ``mark_first_turn``. Patching the
    # cache first means ``app.state.get(run_id)`` reads the new values
    # without a DB round-trip even before the SQL commit lands. We
    # patch all three fields atomically (one ``model_copy``).
    wrote_in_memory = False
    in_mem = _in_memory_records.get(run_id)
    if in_mem is not None:
        _in_memory_records[run_id] = in_mem.model_copy(
            update={
                'turns': turns,
                'cost_usd': cost_usd,
                'last_tool_call': last_tool_call,
            },
        )
        wrote_in_memory = True

    if not is_db_enabled():
        return wrote_in_memory

    # DB write — wrapped in a broad try/except per the initiative's
    # "DB errors MUST NOT crash the run" constraint. We log at WARN
    # (not ERROR) because a single failed writeback is recoverable —
    # the next turn will re-write the running totals. Persistent
    # failures show up as a sustained stream of WARNs, which the
    # operator can pick up via log aggregation.
    try:
        async with db_session() as sess:
            wrote_db: bool = await _db_update_run_progress(
                sess,
                id=run_id,
                turns=turns,
                cost_usd=cost_usd,
                last_tool_call=last_tool_call,
            )
    except Exception as exc:  # noqa: BLE001 — observability hook must not block the SDK loop
        logger.warning(
            'per-turn writeback failed for run_id=%s (turns=%s, cost_usd=%s, last_tool_call=%s): %s',
            run_id,
            turns,
            cost_usd,
            last_tool_call,
            exc,
        )
        return wrote_in_memory

    return wrote_db or wrote_in_memory


def is_run_stale(record: Any, *, threshold_seconds: int) -> bool:
    """Classify whether a run-row is genuinely stuck pre-execution.

    Stale iff ALL of:

    - ``record.turns`` is 0 (or None) — the agent has not emitted any
      turn-summary line yet
    - ``record.started_executing_at`` is None — the first-turn hook
      never fired
    - the row's age (``now() - record.started_at``) exceeds
      ``threshold_seconds``

    Returns False if any guard fails — in particular, a row whose
    ``started_executing_at`` is set is NEVER stale by this rule, even
    when its ``turns`` count is still 0 (the agent has begun a turn
    but not reached the first ``ResultMessage`` yet).

    ``record`` is duck-typed — any object exposing ``turns``,
    ``started_at`` and ``started_executing_at`` attributes works. This
    accommodates both the SQLAlchemy ``InitiativeRunRow`` and the
    pydantic ``InitiativeRecord`` without coupling.

    Note on ``turns is None``: the column starts NULL and is bumped to
    0+ by the reconciler after parsing the agent's log summary. NULL is
    semantically equivalent to "no turn yet observed", so we treat it
    the same as 0 here.
    """
    turns = getattr(record, 'turns', None)
    if turns is not None and turns > 0:
        return False

    started_executing_at = getattr(record, 'started_executing_at', None)
    if started_executing_at is not None:
        # Agent has begun executing — not stale regardless of turn count.
        return False

    started_at = getattr(record, 'started_at', None)
    if started_at is None:
        # Defensive: a record with no started_at can't be classified.
        return False

    # Normalise tz: SQLite (tests) may return naive datetimes; Postgres
    # always TZ-aware. Compare apples-to-apples in UTC.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    age_seconds: float = (_utcnow() - started_at).total_seconds()
    is_stale: bool = age_seconds > threshold_seconds
    return is_stale


def _get_stale_progress_threshold_s() -> int:
    """Read ``STALE_PROGRESS_THRESHOLD_S`` env var, falling back to default.

    A malformed env value (non-int) is logged + ignored — the default
    is preserved so a typo in chart values doesn't disable the
    watchdog altogether. The detector is the only consumer of this
    knob; the chart's ``deployment.yaml`` plumbs the env from
    ``values.yaml`` via ``.Values.staleProgressThresholdSeconds``.
    """
    raw = os.environ.get('STALE_PROGRESS_THRESHOLD_S')
    if not raw:
        return DEFAULT_STALE_PROGRESS_THRESHOLD_S
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            'STALE_PROGRESS_THRESHOLD_S=%r is not an int; using default %ds',
            raw,
            DEFAULT_STALE_PROGRESS_THRESHOLD_S,
        )
        return DEFAULT_STALE_PROGRESS_THRESHOLD_S


def _extract_waiting_reason(pod: Any) -> str | None:
    """Pull ``status.container_statuses[0].state.waiting.reason`` from a pod.

    Accepts kubernetes_asyncio pod-shape objects (where each level is a
    namespace attribute) and the equivalent ``SimpleNamespace`` test
    fixtures interchangeably. Returns ``None`` for any missing-link
    (pod has no status, no container_statuses, no waiting state, no
    reason field) so the caller can treat it uniformly as "no reason
    visible — not stuck on image pull".

    Walks ALL container statuses, not just the first — multi-container
    pods (e.g. the future migrations sidecar pattern) surface
    image-pull failures on whichever container failed, not always
    index 0. The first container with a populated waiting.reason wins.
    """
    if pod is None:
        return None
    status = getattr(pod, 'status', None)
    if status is None:
        return None
    container_statuses = getattr(status, 'container_statuses', None) or []
    for cs in container_statuses:
        state = getattr(cs, 'state', None)
        if state is None:
            continue
        waiting = getattr(state, 'waiting', None)
        if waiting is None:
            continue
        reason = getattr(waiting, 'reason', None)
        if reason:
            return str(reason)
    return None


def detect_pod_stuck_image_pull(
    *,
    record: Any,
    pod: Any | None,
    threshold_seconds: int | None = None,
    now: datetime | None = None,
) -> str | None:
    """V5 D2.1 — classify whether a run's pod is stuck on image-pull failure.

    Returns ``'pod_stuck_<reason>'`` (e.g. ``'pod_stuck_ImagePullBackOff'``)
    iff ALL of:

    1. ``record.status`` is NOT already in a terminal state (skip the
       double-fail case — see ``test_detector_does_not_double_fail``).
    2. ``record.started_executing_at`` is None — the agent has not yet
       reached its first SDK turn. This is the unambiguous "nothing
       happened yet" signal (V5 D2.2). A run whose first turn fired
       is by definition healthy regardless of pod state, so we leave
       the reconciler's normal terminal-state detection to handle it.
    3. ``(now - record.started_at) > threshold_seconds`` — protects
       against false positives during the normal image-pull window
       (a fresh pod can sit in ``ContainerCreating`` for ~30s before
       the image cache hit; the threshold default of 600s gives that
       a wide margin).
    4. The pod's container ``waiting.reason`` is in
       ``IMAGE_PULL_FAILURE_REASONS``. Benign reasons like
       ``ContainerCreating`` (pre-image-pull) and ``PodInitializing``
       (initContainers running) are deliberately NOT in the set —
       those genuinely indicate "still working" rather than "stuck".

    Returns ``None`` in every other case so the caller's loop body
    is a simple ``if reason := detect_pod_stuck_image_pull(...): ...``.

    Parameters
    ----------
    record : Any
        Duck-typed run record exposing ``status``, ``started_at``, and
        ``started_executing_at``. Accepts both the pydantic
        ``InitiativeRecord`` and the SQLAlchemy ``InitiativeRunRow``
        without coupling — the reconciler is the primary caller and
        passes whichever it has on hand.
    pod : Any | None
        Either a kubernetes_asyncio pod object (status.container_statuses
        nested namespaces) OR a dict matching the F5 ``k8s_mcp.get_pod_state``
        shape with a top-level ``'waiting_reason'`` key. The dict path
        lets future callers wire through the MCP layer without re-coding
        the kubernetes_asyncio attribute walk.
    threshold_seconds : int | None
        Optional override. When None, reads from env (defaults to 600s).
    now : datetime | None
        Optional clock injection for deterministic tests. When None,
        uses ``_utcnow()``.

    Notes
    -----
    Memory: ``feedback_orch_cant_see_pod_problems`` — the V4 stall
    happened because the orchestrator-equivalent loop had no signal
    when a pod was in ImagePullBackOff. ``started_executing_at IS
    NULL`` is the agreed-upon "agent hasn't run yet" signal that lets
    this watchdog distinguish "slow first turn" from "stuck pre-turn"
    without relying on the ambiguous ``turns == 0``.
    """
    if record is None:
        return None
    if getattr(record, 'status', None) in _TERMINAL_STATUSES:
        # Already finalised by another path — do not re-fail.
        return None
    if getattr(record, 'started_executing_at', None) is not None:
        # Agent has begun executing — pod's transient waiting reason
        # is irrelevant by definition (the container is running).
        return None

    started_at = getattr(record, 'started_at', None)
    if started_at is None:
        # Defensive: row with no started_at can't be aged. Don't fail it.
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)

    when = now if now is not None else _utcnow()
    threshold = threshold_seconds if threshold_seconds is not None else _get_stale_progress_threshold_s()
    age_seconds = (when - started_at).total_seconds()
    if age_seconds <= threshold:
        return None

    # Accept the F5 dict-shape AND kubernetes_asyncio pod-object shape
    # interchangeably. The dict shape is what `k8s_mcp.get_pod_state`
    # is documented to return; the pod-object shape is what the
    # reconciler already has from `core.list_namespaced_pod`. Same
    # downstream classification in both cases.
    reason: str | None
    if isinstance(pod, dict):
        raw = pod.get('waiting_reason')
        reason = str(raw) if raw else None
    else:
        reason = _extract_waiting_reason(pod)

    if reason and reason in IMAGE_PULL_FAILURE_REASONS:
        return f'pod_stuck_{reason}'
    return None


__all__ = [
    'DEFAULT_STALE_PROGRESS_THRESHOLD_S',
    'IMAGE_PULL_FAILURE_REASONS',
    'detect_pod_stuck_image_pull',
    'is_run_stale',
    'mark_first_turn',
    'update_run_progress',
]
