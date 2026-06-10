"""CRUD operations for Layer 2 (decision log) + Layer 3 (snapshot).

Pairs with ``gate/agent/diagnostics.py`` which is the call-site for the
SDK loop. This module is the thin async DB layer — keep it free of
agent-loop semantics so it stays unit-testable against an in-memory
SQLite engine.

The decision-log surface is INSERT-only — there is no update path.
Operators correct misclassifications by reading + interpreting, not by
editing history. The snapshot surface is UPSERT: the natural terminal
write may fire first or the SIGTERM handler may, depending on whether
the SDK loop completed cleanly. Both cases land at the same row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AgentRunDecisionRow, AgentRunSnapshotRow


@dataclass(frozen=True)
class AgentRunDecisionRecord:
    """Plain-data view of an ``agent_run_decisions`` row."""

    id: int
    run_id: str
    turn_index: int
    kind: str
    summary: str
    payload: Any | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: AgentRunDecisionRow) -> AgentRunDecisionRecord:
        return cls(
            id=row.id,
            run_id=row.run_id,
            turn_index=row.turn_index,
            kind=row.kind,
            summary=row.summary,
            payload=row.payload,
            created_at=row.created_at,
        )


@dataclass(frozen=True)
class AgentRunSnapshotRecord:
    """Plain-data view of an ``agent_run_snapshots`` row."""

    run_id: str
    messages: Any
    message_count: int
    terminal_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: AgentRunSnapshotRow) -> AgentRunSnapshotRecord:
        return cls(
            run_id=row.run_id,
            messages=row.messages,
            message_count=row.message_count,
            terminal_reason=row.terminal_reason,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# ─── Layer 2: decisions ────────────────────────────────────────────────


async def insert_decision(
    session: AsyncSession,
    *,
    run_id: str,
    turn_index: int,
    kind: str,
    summary: str,
    payload: Any | None = None,
) -> AgentRunDecisionRecord:
    """Append one decision row. Returns the persisted record.

    No partial-update path is provided — decision rows are immutable
    once written. Operators reinterpret the history; they don't edit
    it.

    Raises ``IntegrityError`` if ``run_id`` does not reference a real
    ``initiative_runs`` row (FK constraint). The diagnostics caller is
    expected to write the run row first; this is enforced at the DB
    layer to catch wiring mistakes early.
    """
    row = AgentRunDecisionRow(
        run_id=run_id,
        turn_index=turn_index,
        kind=kind,
        summary=summary,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return AgentRunDecisionRecord.from_row(row)


async def list_decisions(
    session: AsyncSession,
    *,
    run_id: str,
    limit: int = 1000,
) -> list[AgentRunDecisionRecord]:
    """Return decisions for a run ordered by ``(turn_index, id)``.

    Stable secondary order via ``id`` matters: two decisions in the
    same turn (e.g. a ``tool_call`` followed by a ``decision``
    classifying its result) must come back in INSERT order so the
    operator's reconstruction reads coherently.
    """
    stmt = (
        select(AgentRunDecisionRow)
        .where(AgentRunDecisionRow.run_id == run_id)
        .order_by(AgentRunDecisionRow.turn_index, AgentRunDecisionRow.id)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [AgentRunDecisionRecord.from_row(row) for row in result.scalars()]


async def count_decisions(session: AsyncSession, *, run_id: str) -> int:
    """Return the number of decision rows for a run (used by tests)."""
    stmt = select(AgentRunDecisionRow).where(AgentRunDecisionRow.run_id == run_id)
    result = await session.execute(stmt)
    return len(result.scalars().all())


# ─── Layer 3: snapshots (UPSERT) ────────────────────────────────────────


async def upsert_snapshot(
    session: AsyncSession,
    *,
    run_id: str,
    messages: list[dict[str, Any]],
    terminal_reason: str | None,
) -> AgentRunSnapshotRecord:
    """Insert or replace the snapshot row for ``run_id``.

    Idempotent: the SIGTERM handler may fire AFTER the natural terminal
    write, OR the natural terminal write may fail leaving the SIGTERM
    handler as the only writer. Both paths land at the same row.

    Implementation uses a portable "fetch + update OR insert" so the
    same code runs against SQLite (tests) and Postgres (production).
    The Postgres-native ``ON CONFLICT DO UPDATE`` would be marginally
    faster but unsupported on SQLite; the perf gap is negligible
    relative to the JSON serialisation cost.

    ``messages`` is serialised AS-IS into the JSONB column — callers
    must shape-normalise SDK message objects into JSON-safe dicts
    upstream (``gate.agent.diagnostics`` owns that transform).
    """
    count = len(messages)
    existing = await session.get(AgentRunSnapshotRow, run_id)
    if existing is None:
        row = AgentRunSnapshotRow(
            run_id=run_id,
            messages=messages,
            message_count=count,
            terminal_reason=terminal_reason,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return AgentRunSnapshotRecord.from_row(row)

    existing.messages = messages
    existing.message_count = count
    # Only overwrite terminal_reason when caller supplies a value —
    # lets the SIGTERM handler "annotate" an existing snapshot without
    # accidentally erasing a reason set by an earlier writer.
    if terminal_reason is not None:
        existing.terminal_reason = terminal_reason
    await session.flush()
    await session.refresh(existing)
    return AgentRunSnapshotRecord.from_row(existing)


async def get_snapshot(
    session: AsyncSession,
    *,
    run_id: str,
) -> AgentRunSnapshotRecord | None:
    """Return the snapshot for a run or None if no terminal write fired."""
    row = await session.get(AgentRunSnapshotRow, run_id)
    return AgentRunSnapshotRecord.from_row(row) if row is not None else None


async def list_snapshots(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[AgentRunSnapshotRecord]:
    """Recent snapshots — operator dashboard helper."""
    stmt = select(AgentRunSnapshotRow).order_by(desc(AgentRunSnapshotRow.created_at)).limit(limit)
    result = await session.execute(stmt)
    return [AgentRunSnapshotRecord.from_row(row) for row in result.scalars()]


__all__ = [
    'AgentRunDecisionRecord',
    'AgentRunSnapshotRecord',
    'count_decisions',
    'get_snapshot',
    'insert_decision',
    'list_decisions',
    'list_snapshots',
    'upsert_snapshot',
]
