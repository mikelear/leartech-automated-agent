"""V5 D2.2 run-driver state-machine helpers.

This module exposes two surfaces:

- ``mark_first_turn(run_id)`` — async, set-once hook fired by the SDK
  loop in ``gate/agent/initiative.py`` the very first time the agent
  emits a turn (the first ``UserMessage``-with-``ToolResultBlock``).
  Sets ``initiative_runs.started_executing_at`` to ``now()`` if the
  column is still NULL; subsequent calls are no-ops at the SQL level
  because the WHERE clause is gated on
  ``started_executing_at IS NULL``. This makes the hook safe to call
  repeatedly without coordination — the database is the source of
  truth, not the in-process flag.

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

Memory: ``feedback_sdk_toolresult_in_usermessage`` — the first
meaningful SDK message is a ``UserMessage`` carrying a
``ToolResultBlock``, NOT an ``AssistantMessage``. Callers in
``gate/agent/initiative.py`` invoke this hook from the same
detection point used for turn counting; no parallel scanner.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update as sa_update

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.models import InitiativeRunRow
from app.state import _records as _in_memory_records

logger = logging.getLogger(__name__)


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


__all__ = ['is_run_stale', 'mark_first_turn']
