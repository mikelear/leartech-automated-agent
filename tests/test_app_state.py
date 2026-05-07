"""Unit tests for app.state — in-memory record + asyncio.Task tracking.

Tests the state primitives directly (no HTTP layer). Concurrency tests
use real asyncio.Tasks against a no-op coroutine.
"""

from __future__ import annotations

import asyncio

import pytest

from app.state import (
    InitiativeRecord,
    cancel,
    get,
    list_records,
    new_id,
    now,
    register,
    update,
)


def test_new_id_is_unique_and_short() -> None:
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100, 'expected unique IDs'
    assert all(len(i) == 12 for i in ids), 'expected 12-char IDs'


def test_now_returns_timezone_aware_utc() -> None:
    ts = now()
    assert ts.tzinfo is not None, 'expected timezone-aware datetime'


def test_get_unknown_id_returns_none() -> None:
    assert get('does-not-exist-xxx') is None


@pytest.mark.asyncio
async def test_register_and_get_round_trip() -> None:
    initiative_id = new_id()

    async def noop() -> int:
        return 0

    task = asyncio.create_task(noop())
    record = InitiativeRecord(
        id=initiative_id,
        initiative='test-initiative',
        status='queued',
        started_at=now(),
    )
    register(record, task)
    retrieved = get(initiative_id)
    assert retrieved is not None
    assert retrieved.id == initiative_id
    assert retrieved.status == 'queued'
    await task  # let it complete


@pytest.mark.asyncio
async def test_update_replaces_fields() -> None:
    initiative_id = new_id()

    async def noop() -> int:
        return 0

    task = asyncio.create_task(noop())
    register(
        InitiativeRecord(id=initiative_id, initiative='i', status='queued', started_at=now()),
        task,
    )
    update(initiative_id, status='running')
    retrieved = get(initiative_id)
    assert retrieved is not None
    assert retrieved.status == 'running'
    update(initiative_id, status='complete', turns=42, cost_usd=1.23)
    retrieved = get(initiative_id)
    assert retrieved is not None
    assert retrieved.status == 'complete'
    assert retrieved.turns == 42
    assert retrieved.cost_usd == pytest.approx(1.23)
    await task


def test_update_unknown_id_is_noop() -> None:
    # Should not raise; just silently does nothing.
    update('unknown-id', status='something')


@pytest.mark.asyncio
async def test_cancel_running_task_returns_true() -> None:
    initiative_id = new_id()

    async def slow() -> int:
        await asyncio.sleep(60)
        return 0

    task = asyncio.create_task(slow())
    register(
        InitiativeRecord(id=initiative_id, initiative='i', status='running', started_at=now()),
        task,
    )
    cancelled = cancel(initiative_id)
    assert cancelled is True
    with pytest.raises(asyncio.CancelledError):
        await task


def test_cancel_unknown_id_returns_false() -> None:
    assert cancel('unknown-id-xxx') is False


@pytest.mark.asyncio
async def test_cancel_already_done_task_returns_false() -> None:
    initiative_id = new_id()

    async def quick() -> int:
        return 0

    task = asyncio.create_task(quick())
    await task  # Let it complete.
    register(
        InitiativeRecord(id=initiative_id, initiative='i', status='complete', started_at=now()),
        task,
    )
    assert cancel(initiative_id) is False


def test_list_records_includes_registered_records() -> None:
    snapshot_count_before = len(list_records())

    async def noop() -> int:
        return 0

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()
    task = loop.create_task(noop())
    record = InitiativeRecord(
        id=new_id(), initiative='listcheck', status='queued', started_at=now(),
    )
    register(record, task)
    snapshot_count_after = len(list_records())
    assert snapshot_count_after > snapshot_count_before
    loop.run_until_complete(task)
    loop.close()
