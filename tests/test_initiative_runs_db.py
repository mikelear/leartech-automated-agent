"""Tests for the DB-backed initiative run store — CRUD layer + state.py integration.

Uses in-memory SQLite (aiosqlite) so tests don't need Postgres. Production
uses Postgres via asyncpg — same SQLAlchemy 2.0 ORM, only the driver differs.

Covers:
- CRUD ops against in-memory SQLite
- mark_orphaned_runs correctly identifies runs not in the live set
- state.py functions write to DB when enabled, fall back to in-memory when not
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.state as state_module
from app import db as db_module
from app.db.initiative_runs import (
    create_run,
    get_run,
    list_runs,
    mark_orphaned_runs,
    update_run,
)
from app.db.models import Base
from app.state import InitiativeRecord, get, list_records, new_id, now, register, update

# ─── Helpers ────────────────────────────────────────────────────────────


def _started_at() -> datetime:
    return datetime.now(UTC)


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Per-test in-memory SQLite session with the full schema applied."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Enable the DB for state.py tests using an in-memory SQLite engine.

    Patches the env var so is_db_enabled() returns True, sets up the engine
    and schema, then tears down on exit. Also resets state._records so tests
    start with a clean in-memory dict.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    state_module._records.clear()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await db_module.dispose_engine()
    db_module._reset_for_tests()
    state_module._records.clear()


@pytest.fixture(autouse=False)
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure DSN is unset — forces in-memory fallback path."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()


# ─── CRUD layer tests (no state.py, no FastAPI) ──────────────────────────


async def test_create_and_get_roundtrip(db_session: AsyncSession) -> None:
    rec = await create_run(
        db_session,
        id='aabbccdd0001',
        initiative='test-run',
        status='queued',
        started_at=_started_at(),
        cluster='gcp',
        created_by='alice',
    )
    assert rec.id == 'aabbccdd0001'
    assert rec.initiative == 'test-run'
    assert rec.status == 'queued'
    assert rec.cluster == 'gcp'
    assert rec.created_by == 'alice'
    assert rec.finished_at is None

    fetched = await get_run(db_session, 'aabbccdd0001')
    assert fetched is not None
    assert fetched.id == 'aabbccdd0001'
    assert fetched.status == 'queued'


async def test_get_returns_none_when_not_found(db_session: AsyncSession) -> None:
    assert await get_run(db_session, 'does-not-exist') is None


async def test_list_returns_most_recent_first(db_session: AsyncSession) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    t2 = datetime(2026, 1, 3, tzinfo=UTC)
    for run_id, ts in (('run-a', t0), ('run-b', t2), ('run-c', t1)):
        await create_run(db_session, id=run_id, initiative='x', status='complete', started_at=ts)
    records = await list_runs(db_session)
    # Ordered by started_at DESC
    assert [r.id for r in records] == ['run-b', 'run-c', 'run-a']


async def test_list_filter_by_status(db_session: AsyncSession) -> None:
    await create_run(db_session, id='r1', initiative='x', status='complete', started_at=_started_at())
    await create_run(db_session, id='r2', initiative='x', status='failed', started_at=_started_at())
    await create_run(db_session, id='r3', initiative='x', status='complete', started_at=_started_at())
    records = await list_runs(db_session, status='complete')
    assert len(records) == 2
    assert all(r.status == 'complete' for r in records)


async def test_list_filter_by_initiative(db_session: AsyncSession) -> None:
    await create_run(db_session, id='r1', initiative='alpha', status='complete', started_at=_started_at())
    await create_run(db_session, id='r2', initiative='beta', status='complete', started_at=_started_at())
    records = await list_runs(db_session, initiative='alpha')
    assert len(records) == 1
    assert records[0].id == 'r1'


async def test_update_partial(db_session: AsyncSession) -> None:
    await create_run(db_session, id='upd1', initiative='x', status='queued', started_at=_started_at())
    updated = await update_run(db_session, id='upd1', status='running')
    assert updated is not None
    assert updated.status == 'running'
    # Other fields unchanged
    assert updated.finished_at is None
    assert updated.turns is None


async def test_update_all_fields(db_session: AsyncSession) -> None:
    ts = _started_at()
    await create_run(db_session, id='upd2', initiative='x', status='running', started_at=ts)
    updated = await update_run(
        db_session,
        id='upd2',
        status='complete',
        finished_at=ts,
        turns=7,
        cost_usd=0.42,
        pr_number=99,
        pr_repo='mikelear/leartech-auth-ui',
        error=None,
    )
    assert updated is not None
    assert updated.status == 'complete'
    assert updated.turns == 7
    assert updated.pr_number == 99
    assert updated.pr_repo == 'mikelear/leartech-auth-ui'


async def test_update_returns_none_when_not_found(db_session: AsyncSession) -> None:
    assert await update_run(db_session, id='ghost', status='running') is None


async def test_update_ignores_unknown_fields(db_session: AsyncSession) -> None:
    await create_run(db_session, id='unk1', initiative='x', status='queued', started_at=_started_at())
    # Should not raise; unknown fields are silently ignored.
    updated = await update_run(db_session, id='unk1', completely_made_up='value', status='running')
    assert updated is not None
    assert updated.status == 'running'


async def test_update_with_no_mutable_fields_returns_current(db_session: AsyncSession) -> None:
    await create_run(db_session, id='noop1', initiative='x', status='queued', started_at=_started_at())
    # Pass only unknown fields (not in _mutable). Should return the current row unchanged.
    result = await update_run(db_session, id='noop1', nonexistent_field='value')
    assert result is not None
    assert result.id == 'noop1'
    assert result.status == 'queued'


# ─── mark_orphaned_runs tests ────────────────────────────────────────────


async def test_mark_orphaned_runs_marks_non_live_in_flight(db_session: AsyncSession) -> None:
    ts = _started_at()
    await create_run(db_session, id='live-1', initiative='x', status='running', started_at=ts)
    await create_run(db_session, id='dead-1', initiative='x', status='running', started_at=ts)
    await create_run(db_session, id='dead-2', initiative='x', status='queued', started_at=ts)
    await create_run(db_session, id='done-1', initiative='x', status='complete', started_at=ts)

    count = await mark_orphaned_runs(db_session, live_ids={'live-1'})
    assert count == 2  # dead-1 and dead-2

    live = await get_run(db_session, 'live-1')
    assert live is not None and live.status == 'running'  # untouched

    dead1 = await get_run(db_session, 'dead-1')
    assert dead1 is not None and dead1.status == 'orphaned'

    dead2 = await get_run(db_session, 'dead-2')
    assert dead2 is not None and dead2.status == 'orphaned'

    done = await get_run(db_session, 'done-1')
    assert done is not None and done.status == 'complete'  # terminal — untouched


async def test_mark_orphaned_runs_empty_live_set_marks_all_in_flight(db_session: AsyncSession) -> None:
    ts = _started_at()
    await create_run(db_session, id='r1', initiative='x', status='running', started_at=ts)
    await create_run(db_session, id='r2', initiative='x', status='queued', started_at=ts)
    count = await mark_orphaned_runs(db_session, live_ids=set())
    assert count == 2


async def test_mark_orphaned_runs_returns_zero_when_nothing_in_flight(db_session: AsyncSession) -> None:
    ts = _started_at()
    await create_run(db_session, id='c1', initiative='x', status='complete', started_at=ts)
    await create_run(db_session, id='c2', initiative='x', status='failed', started_at=ts)
    count = await mark_orphaned_runs(db_session, live_ids=set())
    assert count == 0


async def test_mark_orphaned_runs_idempotent(db_session: AsyncSession) -> None:
    ts = _started_at()
    await create_run(db_session, id='idem1', initiative='x', status='running', started_at=ts)
    count1 = await mark_orphaned_runs(db_session, live_ids=set())
    count2 = await mark_orphaned_runs(db_session, live_ids=set())
    assert count1 == 1
    assert count2 == 0  # already orphaned — not in ('queued', 'running') any more


# ─── state.py DB-enabled path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_register_writes_to_db(db_enabled: None) -> None:
    run_id = new_id()
    rec = InitiativeRecord(id=run_id, initiative='db-write-test', status='queued', started_at=now())
    await register(rec)

    # Verify written to DB via direct CRUD read.
    async with db_module.session() as s:
        db_rec = await get_run(s, run_id)
    assert db_rec is not None
    assert db_rec.initiative == 'db-write-test'
    assert db_rec.status == 'queued'


@pytest.mark.asyncio
async def test_state_register_persists_pr_repo_to_db(db_enabled: None) -> None:
    """Regression: pr_repo must be persisted to the DB at register() time.

    The self_retrospect hook in ``app.routers.initiatives._run_self_retrospect``
    requires pr_repo + pr_number on the final record. Before the 2026-05-28
    fix, ``register()`` only passed cluster/created_by into ``create_run()``
    so the DB row started with ``pr_repo=NULL``. After a pod restart the
    in-memory record was gone and only the DB row survived — pr_repo
    arrived None on every run and the retrospect hook skipped them all
    (observed live on run ``44120e445abd``).

    This test guards the INSERT path: register() must carry pr_repo through
    to create_run() so the DB row has it from the start, before any
    completion update.
    """
    run_id = new_id()
    rec = InitiativeRecord(
        id=run_id,
        initiative='pr-repo-persist-test',
        status='queued',
        started_at=now(),
        pr_repo='mikelear/leartech-automated-agent',
    )
    await register(rec)

    # Clear in-memory cache so we PROVE the DB row carries pr_repo
    # (simulates a pod restart between INSERT and any subsequent read).
    state_module._records.clear()

    async with db_module.session() as s:
        db_rec = await get_run(s, run_id)
    assert db_rec is not None
    assert db_rec.pr_repo == 'mikelear/leartech-automated-agent', (
        'pr_repo must be persisted to the DB at INSERT time, not deferred to '
        'the completion update — otherwise a pod restart loses it.'
    )


@pytest.mark.asyncio
async def test_state_get_reads_from_db(db_enabled: None) -> None:
    run_id = new_id()
    rec = InitiativeRecord(id=run_id, initiative='db-read-test', status='queued', started_at=now())
    await register(rec)

    # Clear in-memory cache to prove we're reading from DB.
    state_module._records.clear()

    fetched = await get(run_id)
    assert fetched is not None
    assert fetched.id == run_id
    assert fetched.status == 'queued'


@pytest.mark.asyncio
async def test_state_update_propagates_to_db(db_enabled: None) -> None:
    run_id = new_id()
    rec = InitiativeRecord(id=run_id, initiative='db-update-test', status='queued', started_at=now())
    await register(rec)

    await update(run_id, status='complete', turns=5, cost_usd=0.12)

    # Verify via direct CRUD read (bypasses state._records).
    async with db_module.session() as s:
        db_rec = await get_run(s, run_id)
    assert db_rec is not None
    assert db_rec.status == 'complete'
    assert db_rec.turns == 5


@pytest.mark.asyncio
async def test_state_list_reads_from_db(db_enabled: None) -> None:
    for name in ('alpha', 'beta', 'gamma'):
        run_id = new_id()
        rec = InitiativeRecord(id=run_id, initiative=name, status='queued', started_at=now())
        await register(rec)

    records = await list_records()
    initiatives_seen = {r.initiative for r in records}
    assert {'alpha', 'beta', 'gamma'}.issubset(initiatives_seen)


# ─── state.py in-memory fallback path (no DB) ────────────────────────────


@pytest.mark.asyncio
async def test_state_fallback_register_and_get(no_db: None) -> None:
    run_id = new_id()
    rec = InitiativeRecord(id=run_id, initiative='mem-test', status='queued', started_at=now())
    await register(rec)

    fetched = await get(run_id)
    assert fetched is not None
    assert fetched.initiative == 'mem-test'


@pytest.mark.asyncio
async def test_state_fallback_update(no_db: None) -> None:
    run_id = new_id()
    rec = InitiativeRecord(id=run_id, initiative='mem-upd', status='queued', started_at=now())
    await register(rec)

    await update(run_id, status='complete', turns=3)
    fetched = await get(run_id)
    assert fetched is not None
    assert fetched.status == 'complete'
    assert fetched.turns == 3


# ─── reconcile_orphaned_runs ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_returns_zero_when_db_disabled(no_db: None) -> None:
    from app.state import reconcile_orphaned_runs

    count = await reconcile_orphaned_runs()
    assert count == 0


@pytest.mark.asyncio
async def test_reconcile_marks_stale_rows_on_startup(db_enabled: None) -> None:
    """Legacy runtime='asyncio' rows (created before Phase F) must be
    orphaned on startup — there's no backing K8s Job and the API pod
    that owned their asyncio.Task is gone."""
    from app.state import reconcile_orphaned_runs

    async with db_module.session() as s:
        ts = _started_at()
        # Use runtime='asyncio' so reconcile doesn't try to verify these
        # against K8s (no POD_NAMESPACE set in this test, the conservative
        # job-runtime branch would otherwise keep them live).
        await create_run(s, id='stale-1', initiative='x', status='running', started_at=ts, runtime='asyncio')
        await create_run(s, id='stale-2', initiative='x', status='queued', started_at=ts, runtime='asyncio')
        await create_run(s, id='done-1', initiative='x', status='complete', started_at=ts)

    count = await reconcile_orphaned_runs()
    assert count == 2

    async with db_module.session() as s:
        r1 = await get_run(s, 'stale-1')
        r2 = await get_run(s, 'stale-2')
        done = await get_run(s, 'done-1')

    assert r1 is not None and r1.status == 'orphaned'
    assert r2 is not None and r2.status == 'orphaned'
    assert done is not None and done.status == 'complete'
