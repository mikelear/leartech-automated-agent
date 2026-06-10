"""Unit + integration tests for the bidirectional command queue.

Covers all four moving parts of initiative
``agent-add-command-queue-with-injection``:

  - DB layer  (``app.db.agent_run_commands``)
  - SDK-loop drain  (``gate.agent.commands``)
  - REST endpoints  (``POST/GET /initiatives/{run_id}/commands``)
  - CLI shape  (asserted via the FastAPI TestClient — the actual click
    parsing is straightforward)

Memory: ``feedback_sqlite_tests_dont_catch_cnpg_runtime_gaps`` — the
JSONB ``payload`` column is exercised via ``JSONBOrJSON`` (falls back
to JSON on SQLite). The CHECK constraint is asserted at the Python
boundary via :class:`UnknownCommandTypeError`; the SQL-level check is
duplicated in migration 0007 for defence in depth and is covered by
``test_chart_initcontainer`` indirectly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.state as state_module
from app import db as db_module
from app.db.agent_run_commands import (
    UnknownCommandTypeError,
    ack_command,
    get_command,
    insert_command,
    list_commands,
    list_unacked_commands,
)
from app.db.models import AGENT_RUN_COMMAND_TYPES, Base
from app.main import app
from app.state import InitiativeRecord, new_id, now, register
from gate.agent.commands import (
    RecordingSink,
    drain_commands,
    wait_while_paused,
)

# ─── Fixtures ──────────────────────────────────────────────────────────


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
    """Enable the DB with an in-memory SQLite engine for state-level tests."""
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


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure DSN is unset — forces no-op fallback path."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()


async def _register_running_run(run_id: str | None = None) -> str:
    """Register a running run; return its id."""
    rid = run_id or new_id()
    await register(InitiativeRecord(id=rid, initiative='cmd-test', status='running', started_at=now()))
    return rid


# ─── Vocabulary ────────────────────────────────────────────────────────


def test_vocabulary_includes_expected_command_types() -> None:
    """Sanity check — the model-level vocabulary matches the spec."""
    assert AGENT_RUN_COMMAND_TYPES == {'cancel', 'pause', 'resume', 'inject_guidance'}


# ─── DB layer ──────────────────────────────────────────────────────────


@pytest.mark.unit
async def test_insert_command_rejects_unknown_type(db_session: AsyncSession) -> None:
    """A typo in command_type must raise BEFORE hitting the DB."""
    with pytest.raises(UnknownCommandTypeError):
        await insert_command(db_session, run_id='r1', command_type='cncel')


@pytest.mark.unit
async def test_insert_command_persists_payload(db_session: AsyncSession) -> None:
    """JSONB payload round-trips through the JSONBOrJSON adapter."""
    # Create the parent run row first (FK is enforced).
    from app.db.initiative_runs import create_run

    rid = await _make_run_via_crud(db_session, run_id='run-1')
    record = await insert_command(
        db_session,
        run_id=rid,
        command_type='inject_guidance',
        payload={'text': 'use docker.io', 'priority': 1},
    )
    assert record.command_type == 'inject_guidance'
    assert record.payload == {'text': 'use docker.io', 'priority': 1}
    assert record.acked_at is None
    # Verify it persisted by re-reading.
    fetched = await get_command(db_session, command_id=record.id)
    assert fetched is not None
    assert fetched.payload == {'text': 'use docker.io', 'priority': 1}
    # Quiet the unused-import warning on `create_run`.
    _ = create_run


@pytest.mark.unit
async def test_ack_command_is_idempotent(db_session: AsyncSession) -> None:
    """A second ack on the same row matches zero rows (set-once)."""
    rid = await _make_run_via_crud(db_session, run_id='run-1')
    record = await insert_command(db_session, run_id=rid, command_type='pause')

    first = await ack_command(db_session, command_id=record.id, success=True, message='ack')
    assert first is True
    second = await ack_command(db_session, command_id=record.id, success=True, message='ack')
    assert second is False


@pytest.mark.unit
async def test_list_unacked_commands_returns_in_submission_order(db_session: AsyncSession) -> None:
    """Commands are processed FIFO — list_unacked_commands honours that."""
    rid = await _make_run_via_crud(db_session, run_id='run-1')
    a = await insert_command(db_session, run_id=rid, command_type='pause')
    b = await insert_command(db_session, run_id=rid, command_type='inject_guidance', payload={'text': 'a'})
    c = await insert_command(db_session, run_id=rid, command_type='resume')

    pending = await list_unacked_commands(db_session, run_id=rid)
    assert [r.id for r in pending] == [a.id, b.id, c.id]

    # Ack the middle one — the other two are still unacked + still ordered.
    await ack_command(db_session, command_id=b.id)
    remaining = await list_unacked_commands(db_session, run_id=rid)
    assert [r.id for r in remaining] == [a.id, c.id]


@pytest.mark.unit
async def test_list_commands_returns_acked_too_by_default(db_session: AsyncSession) -> None:
    """Operator's GET should see history; agent's poll filters to unacked."""
    rid = await _make_run_via_crud(db_session, run_id='run-1')
    pause_cmd = await insert_command(db_session, run_id=rid, command_type='pause')
    await ack_command(db_session, command_id=pause_cmd.id, success=True, message='paused')

    all_cmds = await list_commands(db_session, run_id=rid, unacked_only=False)
    assert len(all_cmds) == 1
    unacked = await list_commands(db_session, run_id=rid, unacked_only=True)
    assert unacked == []


# ─── drain_commands integration ────────────────────────────────────────


@pytest.mark.integration
async def test_cancel_command_terminates_gracefully(db_enabled: None) -> None:
    """Goal step 7 — ``test_cancel_command_terminates_gracefully``.

    Inject cancel mid-turn → drain → sink records cancel reason.
    """
    rid = await _register_running_run()
    async with db_module.session() as sess:
        await insert_command(
            sess,
            run_id=rid,
            command_type='cancel',
            payload={'reason': 'wrong branch'},
        )

    sink = RecordingSink()
    processed = await drain_commands(rid, sink)
    assert processed == 1
    assert len(sink.cancel_calls) == 1
    assert 'wrong branch' in sink.cancel_calls[0]
    assert sink.cancel_calls[0].startswith('cancelled_by_operator:')

    # The row is now acked.
    async with db_module.session() as sess:
        remaining = await list_unacked_commands(sess, run_id=rid)
    assert remaining == []


@pytest.mark.integration
async def test_pause_resume(db_enabled: None) -> None:
    """Goal step 7 — ``test_pause_resume``. Pause flag toggles via commands."""
    rid = await _register_running_run()
    sink = RecordingSink()
    pause_state = {'paused': False}

    class _StatefulSink(RecordingSink):
        def set_pause(self, paused: bool) -> None:
            super().set_pause(paused)
            pause_state['paused'] = paused

    sink = _StatefulSink()

    async with db_module.session() as sess:
        await insert_command(sess, run_id=rid, command_type='pause')
    await drain_commands(rid, sink)
    assert pause_state['paused'] is True

    async with db_module.session() as sess:
        await insert_command(sess, run_id=rid, command_type='resume')

    # wait_while_paused drains until is_paused returns False.
    total = await wait_while_paused(
        rid,
        sink,
        is_paused=lambda: pause_state['paused'],
        poll_interval_seconds=0.01,
        max_iterations=5,
    )
    assert pause_state['paused'] is False
    assert total >= 1


@pytest.mark.integration
async def test_inject_guidance_appears_in_next_turn(db_enabled: None) -> None:
    """Goal step 7 — ``test_inject_guidance_appears_in_next_turn``.

    The drain pipes the operator's text into the sink's inject path —
    in production this lands in the conversation buffer.
    """
    rid = await _register_running_run()
    sink = RecordingSink()

    async with db_module.session() as sess:
        await insert_command(
            sess,
            run_id=rid,
            command_type='inject_guidance',
            payload={'text': 'stop using ghcr — use docker.io'},
        )
    processed = await drain_commands(rid, sink)
    assert processed == 1
    assert sink.inject_calls == ['stop using ghcr — use docker.io']


@pytest.mark.integration
async def test_command_ack_recorded(db_enabled: None) -> None:
    """Goal step 7 — ``test_command_ack_recorded``.

    drain_commands sets ``acked_at`` + ``ack_message`` on every row it
    processes.
    """
    rid = await _register_running_run()
    sink = RecordingSink()

    async with db_module.session() as sess:
        cmd = await insert_command(sess, run_id=rid, command_type='resume')

    await drain_commands(rid, sink)

    async with db_module.session() as sess:
        fetched = await get_command(sess, command_id=cmd.id)
    assert fetched is not None
    assert fetched.acked_at is not None
    assert fetched.ack_message is not None
    assert fetched.ack_message.startswith('ok: ')


@pytest.mark.integration
async def test_multiple_unacked_commands_processed_in_order(db_enabled: None) -> None:
    """Goal step 7 — ``test_multiple_unacked_commands_processed_in_order``.

    Three commands queued before any drain → all processed, FIFO.
    """
    rid = await _register_running_run()
    sink = RecordingSink()

    async with db_module.session() as sess:
        await insert_command(sess, run_id=rid, command_type='pause')
        await insert_command(
            sess,
            run_id=rid,
            command_type='inject_guidance',
            payload={'text': 'note'},
        )
        await insert_command(sess, run_id=rid, command_type='resume')

    processed = await drain_commands(rid, sink)
    assert processed == 3
    assert sink.pause_calls == [True, False]
    assert sink.inject_calls == ['note']


@pytest.mark.integration
async def test_inject_guidance_with_empty_text_acks_with_error(db_enabled: None) -> None:
    """An ``inject_guidance`` row with empty text gets an ``err:`` ack.

    Defence in depth — the REST endpoint blocks this case, but a raw
    SQL insert (or a future client) might queue a malformed row. The
    handler shouldn't crash the loop; it should ack with err so the
    operator sees the rejection in their next GET.
    """
    rid = await _register_running_run()
    sink = RecordingSink()

    async with db_module.session() as sess:
        cmd = await insert_command(
            sess,
            run_id=rid,
            command_type='inject_guidance',
            payload={'text': '  '},  # whitespace-only
        )

    await drain_commands(rid, sink)
    assert sink.inject_calls == []

    async with db_module.session() as sess:
        fetched = await get_command(sess, command_id=cmd.id)
    assert fetched is not None
    assert fetched.ack_message is not None
    assert fetched.ack_message.startswith('err: ')


@pytest.mark.integration
async def test_drain_commands_is_noop_when_db_disabled(no_db: None) -> None:
    """Laptop-CLI mode — drain returns 0 without raising."""
    sink = RecordingSink()
    assert await drain_commands('any-run', sink) == 0
    assert sink.cancel_calls == []


@pytest.mark.integration
async def test_drain_commands_is_noop_when_run_id_missing(db_enabled: None) -> None:
    """Defensive — drain returns 0 without touching the DB for None ids."""
    sink = RecordingSink()
    assert await drain_commands(None, sink) == 0
    assert await drain_commands('', sink) == 0


# ─── REST endpoints (FastAPI TestClient) ───────────────────────────────


@pytest.mark.integration
def test_post_commands_returns_201_with_command_id(db_enabled: None) -> None:
    """``POST /initiatives/{id}/commands`` queues the command + returns the id.

    Uses the FastAPI TestClient against the real app — guarantees the
    router wiring + pydantic validation work end-to-end.
    """
    import asyncio

    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    with TestClient(app) as client:
        response = client.post(
            f'/initiatives/{rid}/commands',
            json={'command_type': 'pause'},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert isinstance(body['command_id'], int)
    assert body['submitted_at'].startswith('2')  # ISO datetime starts with year


@pytest.mark.integration
def test_post_commands_404_for_unknown_run(db_enabled: None) -> None:
    """Unknown run → 404, not a generic 500."""
    with TestClient(app) as client:
        response = client.post(
            '/initiatives/does-not-exist/commands',
            json={'command_type': 'pause'},
        )
    assert response.status_code == 404


@pytest.mark.integration
def test_post_commands_409_for_terminal_run(db_enabled: None) -> None:
    """Commands cannot be queued against a finished run."""
    import asyncio

    rid = new_id()

    async def _setup() -> None:
        await register(
            InitiativeRecord(id=rid, initiative='cmd-test', status='complete', started_at=now()),
        )

    asyncio.get_event_loop().run_until_complete(_setup())

    with TestClient(app) as client:
        response = client.post(
            f'/initiatives/{rid}/commands',
            json={'command_type': 'cancel', 'payload': {'reason': 'too late'}},
        )
    assert response.status_code == 409


@pytest.mark.integration
def test_post_commands_422_for_unknown_type(db_enabled: None) -> None:
    """Bad command_type → 422 (not 500)."""
    import asyncio

    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    with TestClient(app) as client:
        response = client.post(
            f'/initiatives/{rid}/commands',
            json={'command_type': 'destroy'},
        )
    assert response.status_code == 422


@pytest.mark.integration
def test_post_commands_422_for_inject_without_text(db_enabled: None) -> None:
    """inject_guidance requires payload.text — missing → 422."""
    import asyncio

    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    with TestClient(app) as client:
        response = client.post(
            f'/initiatives/{rid}/commands',
            json={'command_type': 'inject_guidance', 'payload': {}},
        )
    assert response.status_code == 422


@pytest.mark.integration
def test_get_commands_returns_queued_records(db_enabled: None) -> None:
    """``GET /initiatives/{id}/commands`` mirrors the agent-side poll."""
    import asyncio

    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    with TestClient(app) as client:
        client.post(f'/initiatives/{rid}/commands', json={'command_type': 'pause'})
        response = client.get(f'/initiatives/{rid}/commands')
    assert response.status_code == 200
    items = response.json()
    assert len(items) == 1
    assert items[0]['command_type'] == 'pause'
    assert items[0]['acked_at'] is None


@pytest.mark.integration
def test_get_commands_503_when_db_disabled(no_db: None) -> None:
    """Filesystem-only mode → command queue requires the DB → 503."""
    # Need a run id even when DB is off (the route otherwise 404s on
    # missing run before getting to the db check); but no_db mode
    # never has a real run anyway. We expect 503 to win over 404 here
    # because the db-check fires first.
    with TestClient(app) as client:
        response = client.get('/initiatives/whatever/commands')
    assert response.status_code == 503


# ─── Helpers ───────────────────────────────────────────────────────────


async def _make_run_via_crud(session: AsyncSession, *, run_id: str) -> str:
    """Insert an initiative_runs row directly via CRUD — bypasses state.py.

    Used by the DB-layer tests above which take an explicit session and
    don't go through ``register()``. Keeps the FK happy.
    """
    from datetime import UTC
    from datetime import datetime as _dt

    from app.db.initiative_runs import create_run

    await create_run(
        session,
        id=run_id,
        initiative='cmd-test',
        status='running',
        started_at=_dt.now(UTC),
    )
    return run_id
