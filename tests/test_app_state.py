"""Unit tests for app.state — in-memory record management.

Tests the state primitives directly (no HTTP layer). Phase F removed
the asyncio.Task tracking (and the cancel() helper that targeted it);
every run is a K8s Job now, so the state module only owns the
in-memory record dict + write-through-DB plumbing.

State functions are now async (write-through-DB path). In these tests
no DSN is set, so all operations use the in-memory fallback — same
behaviour as before, just needs `await`.
"""

from __future__ import annotations

import pytest

from app.state import (
    InitiativeRecord,
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


@pytest.mark.asyncio
async def test_get_unknown_id_returns_none() -> None:
    assert await get('does-not-exist-xxx') is None


@pytest.mark.asyncio
async def test_register_and_get_round_trip() -> None:
    initiative_id = new_id()
    record = InitiativeRecord(
        id=initiative_id,
        initiative='test-initiative',
        status='queued',
        started_at=now(),
    )
    await register(record)
    retrieved = await get(initiative_id)
    assert retrieved is not None
    assert retrieved.id == initiative_id
    assert retrieved.status == 'queued'


@pytest.mark.asyncio
async def test_update_replaces_fields() -> None:
    initiative_id = new_id()
    await register(
        InitiativeRecord(id=initiative_id, initiative='i', status='queued', started_at=now()),
    )
    await update(initiative_id, status='running')
    retrieved = await get(initiative_id)
    assert retrieved is not None
    assert retrieved.status == 'running'
    await update(initiative_id, status='complete', turns=42, cost_usd=1.23)
    retrieved = await get(initiative_id)
    assert retrieved is not None
    assert retrieved.status == 'complete'
    assert retrieved.turns == 42
    assert retrieved.cost_usd == pytest.approx(1.23)


@pytest.mark.asyncio
async def test_update_unknown_id_is_noop() -> None:
    # Should not raise; just silently does nothing.
    await update('unknown-id', status='something')


@pytest.mark.asyncio
async def test_list_records_includes_registered_records() -> None:
    snapshot_count_before = len(await list_records())
    record = InitiativeRecord(
        id=new_id(),
        initiative='listcheck',
        status='queued',
        started_at=now(),
    )
    await register(record)
    snapshot_count_after = len(await list_records())
    assert snapshot_count_after > snapshot_count_before
