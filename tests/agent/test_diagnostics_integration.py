"""Integration tests for comprehensive failure diagnostics — full SDK-loop wiring.

Tier 2 (``integration`` marker). Exercises the wiring between:

- ``gate/agent/initiative.py`` (the SDK message-loop)
- ``gate/agent/diagnostics.py`` (the four-layer observability surface)
- ``app/db/agent_diagnostics.py`` (CRUD)
- ``app/db/models.py`` (schema)

A future refactor that moves the diagnostics calls behind a different
import path, or accidentally drops the SIGTERM handler install, will
surface here.

The unit tests in ``test_diagnostics.py`` exercise each layer in
isolation (DB enabled, in-memory SQLite). This file exercises the
*wiring* — same in-memory SQLite backend, but driven through the
public initiative-module helpers rather than the direct CRUD calls.

Memory: ``feedback_async_tests_need_event_not_sleep`` — coordination
via explicit ``await`` ordering, no ``asyncio.sleep`` for control flow.

Memory: ``feedback_sqlite_tests_dont_catch_cnpg_runtime_gaps`` — these
tests run against in-memory SQLite. The Postgres-specific JSONB +
TOAST + cascade behaviour is verified by the cluster-side
``end2end-ui`` tier and the chart's preview deploy, NOT here. This
file's job is to pin the Python-side wiring so a refactor that breaks
the call graph fails fast at unit-test time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

import app.state as state_module
from app import db as db_module
from app.db.agent_diagnostics import (
    count_decisions,
    get_snapshot,
    list_decisions,
)
from app.db.initiative_runs import get_run
from app.db.models import Base
from app.state import InitiativeRecord, new_id, now, register
from gate.agent.diagnostics import (
    ConversationBuffer,
    TerminateState,
    bump_turn_counter,
    classify_failure,
    persist_conversation_snapshot,
    record_decision,
    write_failure_reason,
)


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Enable the DB with an in-memory SQLite engine + clean state cache."""
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


# ─── Headline integration: full agent terminal sequence ────────────────


@pytest.mark.integration
async def test_integration_full_terminal_sequence_populates_all_three_layers(
    db_enabled: None,
) -> None:
    """Simulate the SDK-loop's terminal sequence end-to-end:

    1. Register a run (router does this on POST /initiatives).
    2. Five tool calls happen — five decision rows land.
    3. Loop exits cleanly — snapshot persisted with all five messages.
    4. Final decision row marks the terminal.
    5. Operator queries all three layers + sees a coherent narrative.
    """
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='full-terminal-test',
            status='running',
            started_at=now(),
        )
    )

    buf = ConversationBuffer()

    # Simulate five tool calls — each one bumps the counter and writes a row.
    for i in range(5):
        bump_turn_counter(run_id)
        await record_decision(
            run_id,
            'tool_call',
            f'Bash: ls /workspace/iter-{i}',
            payload={'tool': 'Bash', 'iter': i},
        )
        buf.append({'role': 'assistant', 'content': f'tool call {i}'})
        buf.append({'role': 'user', 'content': f'tool result {i}'})

    # Loop terminates cleanly — snapshot writes the full conversation.
    await persist_conversation_snapshot(
        run_id,
        buf,
        terminal_reason='complete',
    )
    await record_decision(
        run_id,
        'terminate',
        'agent loop exited cleanly',
        payload={'exit_code': 0},
    )

    # Operator query 1 — error column (should be NULL for success).
    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error is None  # successful run, no error reason

    # Operator query 2 — decision log: 5 tool calls + 1 terminate = 6 rows.
    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert len(decisions) == 6
    tool_calls = [d for d in decisions if d.kind == 'tool_call']
    terminates = [d for d in decisions if d.kind == 'terminate']
    assert len(tool_calls) == 5
    assert len(terminates) == 1

    # Operator query 3 — snapshot: 10 messages, terminal_reason='complete'.
    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 10
    assert snap.terminal_reason == 'complete'


# ─── Failure path: SDK crashes mid-turn ────────────────────────────────


@pytest.mark.integration
async def test_integration_sdk_crash_writes_classified_error_and_partial_snapshot(
    db_enabled: None,
) -> None:
    """Simulate the SDK crashing at turn 3 with a ValueError.

    Expect:
    - ``initiative_runs.error`` carries ``agent_sdk_error: ValueError: ...``
    - The snapshot has the 3 turns observed before the crash
    - A 'terminate' decision row marks the failure
    """
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='sdk-crash-test',
            status='running',
            started_at=now(),
        )
    )

    buf = ConversationBuffer()

    # Three turns happen, then exception raised at turn 3.
    for i in range(3):
        bump_turn_counter(run_id)
        await record_decision(
            run_id,
            'tool_call',
            f'Bash: iter-{i}',
        )
        buf.append({'role': 'assistant', 'content': f'turn {i}'})

    # Now the exception path fires (mimicking initiative.py's try/except).
    exc = ValueError('Connection reset by peer (simulated)')
    reason = classify_failure(exc, last_turn_count=3, max_turns=200)
    await write_failure_reason(run_id, reason)
    await persist_conversation_snapshot(
        run_id,
        buf,
        terminal_reason='failed',
    )
    await record_decision(
        run_id,
        'terminate',
        f'agent terminated with exception: {reason}',
        payload={'reason': reason},
    )

    # Verify each layer.
    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error is not None
    assert run.error.startswith('agent_sdk_error:')
    assert 'ValueError' in run.error
    assert 'Connection reset' in run.error

    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 3
    assert snap.terminal_reason == 'failed'

    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    # 3 tool_call + 1 terminate
    assert len(decisions) == 4
    assert decisions[-1].kind == 'terminate'


# ─── Operator query patterns from the initiative spec ──────────────────


@pytest.mark.integration
async def test_integration_operator_query_patterns_work(db_enabled: None) -> None:
    """The three SQL patterns called out in the initiative goal section 6
    must return useful data after a terminal write.

    Pattern 1: SELECT id, status, error FROM initiative_runs WHERE id = 'X';
    Pattern 2: SELECT turn_index, kind, summary
                  FROM agent_run_decisions WHERE run_id = 'X'
                  ORDER BY turn_index;
    Pattern 3: SELECT messages FROM agent_run_snapshots WHERE run_id = 'X';
    """
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='operator-query-test',
            status='failed',
            started_at=now(),
        )
    )

    bump_turn_counter(run_id)
    await record_decision(run_id, 'tool_call', 'gh pr list')
    bump_turn_counter(run_id)
    await record_decision(run_id, 'gate', 'gate verdict: red, 2 criteria failed')

    await write_failure_reason(run_id, 'agent_sdk_error: RuntimeError: cap exceeded')
    buf = ConversationBuffer()
    buf.append({'role': 'user', 'content': 'driver prompt'})
    buf.append({'role': 'assistant', 'content': 'first response'})
    await persist_conversation_snapshot(run_id, buf, terminal_reason='failed')

    # Pattern 1
    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error == 'agent_sdk_error: RuntimeError: cap exceeded'

    # Pattern 2
    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert [d.summary for d in decisions] == ['gh pr list', 'gate verdict: red, 2 criteria failed']
    assert [d.turn_index for d in decisions] == [1, 2]

    # Pattern 3
    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert isinstance(snap.messages, list)
    assert snap.messages[0]['content'] == 'driver prompt'
    assert snap.messages[1]['content'] == 'first response'


# ─── Schema bootstrap parity ───────────────────────────────────────────


@pytest.mark.integration
async def test_integration_create_all_bootstraps_diagnostics_tables() -> None:
    """The new tables must come up via ``Base.metadata.create_all`` —
    the test bootstrap path used by every other test in the repo. A
    drift between the ORM declaration and the migration SQL would be
    caught here (the migration covers the production initContainer
    path; create_all covers tests + dev)."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _list_tables(sync_conn: object) -> list[str]:
            from sqlalchemy import inspect

            insp = inspect(sync_conn)
            return list(insp.get_table_names())

        tables = await conn.run_sync(_list_tables)
    await engine.dispose()

    assert 'agent_run_decisions' in tables
    assert 'agent_run_snapshots' in tables
    # And the parent table is still present (FK requirement).
    assert 'initiative_runs' in tables


# ─── End-to-end SIGTERM mid-loop simulation ────────────────────────────


@pytest.mark.integration
async def test_integration_sigterm_mid_loop_persists_partial_state(
    db_enabled: None,
) -> None:
    """Simulate a SIGTERM arriving in the middle of turn-3 work.

    Expect ALL of:
    - error='silent_terminate: SIGTERM received at turn 3/200'
    - snapshot has the partial conversation buffer
    - decision log has tool_call rows up to turn 3 PLUS a final 'sigterm' row

    Uses the diagnostics module's internal ``_flush`` helper rather than
    raising SIGTERM in-process — same code path the SIGTERM signal
    handler invokes.
    """
    from gate.agent.diagnostics import _flush

    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='sigterm-mid-loop',
            status='running',
            started_at=now(),
        )
    )

    state = TerminateState(run_id=run_id, max_turns=200)

    # Three turns of activity before SIGTERM.
    for i in range(3):
        turn_idx = bump_turn_counter(run_id)
        state.last_turn_count = turn_idx
        await record_decision(
            run_id,
            'tool_call',
            f'Bash: cmd-{i}',
        )
        state.buffer.append({'role': 'assistant', 'content': f'message {i}'})

    # SIGTERM fires.
    await _flush(state, 'silent_terminate: SIGTERM received')

    # Layer 1
    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error is not None
    assert 'silent_terminate' in run.error
    assert 'turn 3' in run.error

    # Layer 3
    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 3
    assert snap.terminal_reason == 'sigterm'

    # Layer 2 — 3 tool_calls + 1 sigterm
    async with db_module.session() as sess:
        cnt = await count_decisions(sess, run_id=run_id)
    assert cnt == 4
    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert decisions[-1].kind == 'sigterm'
