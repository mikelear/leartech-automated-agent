"""In-memory state store for running initiatives.

v1.5 limitation: lost on process restart. Acceptable for now — v2 will move
to Redis or postgres when persistence + multi-replica matter. The state
store is intentionally minimal: id → status + timing + exit metadata.

Concurrency: multiple initiatives can run in parallel as separate
asyncio.Tasks. They share the Anthropic API key and rate limit; if two
target the same repo's checkout, file-system contention is the consumer's
problem to avoid (don't queue two against the same branch).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class InitiativeRecord(BaseModel):
    id: str
    initiative: str
    status: str = Field(description='queued | running | complete | failed | cancelled')
    started_at: datetime
    finished_at: datetime | None = None
    pr_number: int | None = None
    turns: int | None = None
    cost_usd: float | None = None
    error: str | None = None


_records: dict[str, InitiativeRecord] = {}
_tasks: dict[str, asyncio.Task[Any]] = {}


def new_id() -> str:
    """Short opaque ID — not security-sensitive, just unique within the process."""
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(UTC)


def register(record: InitiativeRecord, task: asyncio.Task[Any]) -> None:
    _records[record.id] = record
    _tasks[record.id] = task


def get(initiative_id: str) -> InitiativeRecord | None:
    return _records.get(initiative_id)


def update(initiative_id: str, **fields: Any) -> None:
    record = _records.get(initiative_id)
    if record is None:
        return
    _records[initiative_id] = record.model_copy(update=fields)


def cancel(initiative_id: str) -> bool:
    task = _tasks.get(initiative_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def list_records() -> list[InitiativeRecord]:
    return list(_records.values())
