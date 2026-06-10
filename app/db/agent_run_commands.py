"""CRUD operations for the bidirectional command queue (Layer 1 — DB).

Implements the storage surface for initiative
``agent-add-command-queue-with-injection``. The SDK loop polls between
turns via :func:`list_unacked_commands`; the REST endpoint /
``leartech-agent ops`` CLI write commands via :func:`insert_command`;
both sides observe acks via :func:`ack_command` and reads.

Designed to mirror the shape of ``app/db/agent_diagnostics.py`` so
operators reading either module get a consistent feel — plain async
helpers operating on an injected ``AsyncSession``, frozen dataclass
record types, and zero agent-loop semantics.

Crash safety: writes here are best-effort observability + control. A
DB hiccup that prevents an ack write is recoverable on the next poll
(the row stays unacked, the agent reprocesses it on the next turn —
the command handlers themselves are written to be idempotent for this
case; see :mod:`gate.agent.commands`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AGENT_RUN_COMMAND_TYPES, AgentRunCommandRow


class UnknownCommandTypeError(ValueError):
    """Raised when a caller passes a ``command_type`` outside the vocabulary.

    Distinct from the DB-layer CHECK constraint failure so callers can
    catch the typo before issuing a transaction. The REST handler maps
    this to a 422 — same shape as the DB's check failure would surface
    as a 500, but with a friendlier error envelope.
    """


@dataclass(frozen=True)
class AgentRunCommandRecord:
    """Plain-data view of an ``agent_run_commands`` row.

    Frozen + JSON-serialisable so the REST handler can hand it
    straight to FastAPI's response model without extra plumbing.
    """

    id: int
    run_id: str
    command_type: str
    payload: Any | None
    submitted_at: datetime
    acked_at: datetime | None
    ack_message: str | None

    @classmethod
    def from_row(cls, row: AgentRunCommandRow) -> AgentRunCommandRecord:
        return cls(
            id=row.id,
            run_id=row.run_id,
            command_type=row.command_type,
            payload=row.payload,
            submitted_at=row.submitted_at,
            acked_at=row.acked_at,
            ack_message=row.ack_message,
        )


def _validate_command_type(command_type: str) -> None:
    """Raise :class:`UnknownCommandTypeError` for an out-of-vocabulary type.

    Mirrors the DB CHECK constraint at the application boundary so the
    REST endpoint surfaces a 422 instead of a 500 when an operator
    fat-fingers ``"cncel"``.
    """
    if command_type not in AGENT_RUN_COMMAND_TYPES:
        raise UnknownCommandTypeError(
            f'Unknown command_type {command_type!r}; expected one of {sorted(AGENT_RUN_COMMAND_TYPES)}'
        )


async def insert_command(
    session: AsyncSession,
    *,
    run_id: str,
    command_type: str,
    payload: Any | None = None,
) -> AgentRunCommandRecord:
    """Append one command row. Returns the persisted record.

    No-ops are NOT supported here — the caller is expected to have
    resolved the run_id via the existing ``initiative_runs`` lookup
    (the REST endpoint does this before insert). If the FK doesn't
    match a real run, SQLAlchemy raises ``IntegrityError`` which the
    handler translates to a 404.
    """
    _validate_command_type(command_type)
    row = AgentRunCommandRow(
        run_id=run_id,
        command_type=command_type,
        payload=payload,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return AgentRunCommandRecord.from_row(row)


async def list_unacked_commands(
    session: AsyncSession,
    *,
    run_id: str,
    limit: int = 100,
) -> list[AgentRunCommandRecord]:
    """Return all unacked commands for a run, ordered by submission time.

    The agent loop calls this every turn — typically returns 0 rows.
    The partial index ``ix_agent_run_commands_unacked`` makes that
    "no commands waiting" path sub-millisecond even at high command
    volumes.

    ``limit`` defaults to 100 so a runaway operator script can't queue
    a million commands and starve the loop. In practice a healthy run
    sees 0-3 pending commands.
    """
    stmt = (
        select(AgentRunCommandRow)
        .where(AgentRunCommandRow.run_id == run_id)
        .where(AgentRunCommandRow.acked_at.is_(None))
        .order_by(AgentRunCommandRow.submitted_at, AgentRunCommandRow.id)
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [AgentRunCommandRecord.from_row(row) for row in result.scalars()]


async def list_commands(
    session: AsyncSession,
    *,
    run_id: str,
    unacked_only: bool = False,
    limit: int = 200,
) -> list[AgentRunCommandRecord]:
    """List commands for a run — both acked and unacked by default.

    Operators reading the command history (via ``GET /initiatives/
    {run_id}/commands``) want to see what was sent, what was acked,
    and what stalled. ``unacked_only=True`` matches the agent-loop
    surface for operators who want to see only what's still pending.
    """
    stmt = select(AgentRunCommandRow).where(AgentRunCommandRow.run_id == run_id)
    if unacked_only:
        stmt = stmt.where(AgentRunCommandRow.acked_at.is_(None))
    stmt = stmt.order_by(AgentRunCommandRow.submitted_at, AgentRunCommandRow.id).limit(limit)
    result = await session.execute(stmt)
    return [AgentRunCommandRecord.from_row(row) for row in result.scalars()]


async def ack_command(
    session: AsyncSession,
    *,
    command_id: int,
    success: bool = True,
    message: str | None = None,
) -> bool:
    """Mark a command as processed. Returns True iff a row was updated.

    The ``success`` parameter is reflected in the ack_message prefix
    (``ok: ...`` vs ``err: ...``) so an operator's ``GET`` of the
    command history shows the outcome without needing a separate
    column. We deliberately don't add a status column — the textual
    convention keeps the schema lean and ack_message can carry any
    extra context (the cancel reason, the injected text, the
    resume-from-pause delay, etc.).

    Idempotent: re-acking an already-acked row is a no-op (the
    ``acked_at IS NULL`` guard means the UPDATE matches 0 rows). This
    matters because the SDK loop processes commands inside the same
    session and a transient retry must not double-ack.
    """
    prefix = 'ok: ' if success else 'err: '
    rendered = prefix + (message or '')
    if len(rendered) > 2000:
        rendered = rendered[:1997] + '...'

    stmt = (
        sa_update(AgentRunCommandRow)
        .where(AgentRunCommandRow.id == command_id)
        .where(AgentRunCommandRow.acked_at.is_(None))
        .values(acked_at=datetime.now(UTC), ack_message=rendered)
    )
    result = await session.execute(stmt)
    rowcount = getattr(result, 'rowcount', 0) or 0
    return rowcount > 0


async def get_command(
    session: AsyncSession,
    *,
    command_id: int,
) -> AgentRunCommandRecord | None:
    """Fetch one command by primary key, or None if missing.

    Used by tests to assert state transitions; not exposed via REST
    today — operators view commands through the list surface.
    """
    row = await session.get(AgentRunCommandRow, command_id)
    return AgentRunCommandRecord.from_row(row) if row is not None else None


__all__ = [
    'AgentRunCommandRecord',
    'UnknownCommandTypeError',
    'ack_command',
    'get_command',
    'insert_command',
    'list_commands',
    'list_unacked_commands',
]
