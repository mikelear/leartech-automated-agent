"""Run-state store for active and completed initiatives.

Write-through-DB when `LEARTECH_INITIATIVE_DB_DSN` is configured; falls
back to in-memory dict when not. The in-memory dict is always maintained
as a fast-path cache — so interim `update()` calls from background tasks
never race against an incomplete DB INSERT (the dict is updated first,
then the DB write follows).

Reads prefer DB when enabled (persistence across pod restarts); fall back
to `_records` when not (dev / CI / preview without Postgres).

`_tasks` is always in-memory — asyncio.Task objects cannot be persisted.

v2: pod restart leaves DB rows in 'running'/'queued'. `reconcile_orphaned_runs()`
is called on FastAPI startup and marks those rows 'orphaned' so API consumers
can detect the gap.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_runs import (
    InitiativeRunRecord,
    create_run,
    get_run,
    list_runs,
    mark_orphaned_runs,
    update_run,
)


class InitiativeRecord(BaseModel):
    id: str
    initiative: str
    status: str = Field(description='queued | running | complete | failed | cancelled | orphaned | timed_out')
    started_at: datetime
    finished_at: datetime | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    turns: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    cluster: str | None = None
    created_by: str | None = None


_records: dict[str, InitiativeRecord] = {}
_tasks: dict[str, asyncio.Task[Any]] = {}


def new_id() -> str:
    """Short opaque ID — not security-sensitive, just unique within the process."""
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(UTC)


def _run_record_to_initiative_record(run: InitiativeRunRecord) -> InitiativeRecord:
    """Convert a DB-layer run record to the API-facing pydantic model."""
    return InitiativeRecord(
        id=run.id,
        initiative=run.initiative,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        pr_number=run.pr_number,
        pr_repo=run.pr_repo,
        turns=run.turns,
        cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
        error=run.error,
        cluster=run.cluster,
        created_by=run.created_by,
    )


async def register(record: InitiativeRecord, task: asyncio.Task[Any]) -> None:
    """Register a new initiative run — in-memory always, DB when configured.

    The in-memory write happens first (no await), so background tasks that
    call update() immediately after creation never race against an incomplete
    DB INSERT.
    """
    _records[record.id] = record
    _tasks[record.id] = task
    if is_db_enabled():
        async with db_session() as s:
            await create_run(
                s,
                id=record.id,
                initiative=record.initiative,
                status=record.status,
                started_at=record.started_at,
                cluster=record.cluster,
                created_by=record.created_by,
            )


async def get(initiative_id: str) -> InitiativeRecord | None:
    """Retrieve run state — from DB when configured, in-memory fallback otherwise."""
    if is_db_enabled():
        async with db_session() as s:
            run = await get_run(s, initiative_id)
            if run is None:
                return None
            return _run_record_to_initiative_record(run)
    return _records.get(initiative_id)


async def update(initiative_id: str, **fields: Any) -> None:
    """Partial update — updates in-memory dict first, then DB if configured.

    The in-memory update is synchronous (no await) so callers in a background
    task see the change immediately even while a concurrent DB write is in
    flight.
    """
    rec = _records.get(initiative_id)
    if rec is not None:
        _records[initiative_id] = rec.model_copy(update=fields)
    if is_db_enabled():
        async with db_session() as s:
            await update_run(s, id=initiative_id, **fields)


async def cancel(initiative_id: str) -> bool:
    """Request cancellation of a running task. Returns True if cancelled."""
    task = _tasks.get(initiative_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def list_records() -> list[InitiativeRecord]:
    """List all run records — from DB when configured, in-memory fallback otherwise."""
    if is_db_enabled():
        async with db_session() as s:
            runs = await list_runs(s)
            return [_run_record_to_initiative_record(r) for r in runs]
    return list(_records.values())


async def reconcile_orphaned_runs() -> int:
    """Mark in-flight DB runs as 'orphaned' if no live asyncio.Task exists.

    Called on FastAPI startup. A pod restart leaves DB rows in 'running' or
    'queued' state but with no Task in `_tasks`. This function rectifies
    the state so API consumers can detect the gap and act accordingly.

    Returns the count of rows marked orphaned (0 when DB is not configured).
    """
    if not is_db_enabled():
        return 0
    async with db_session() as s:
        return await mark_orphaned_runs(s, set(_tasks.keys()))
