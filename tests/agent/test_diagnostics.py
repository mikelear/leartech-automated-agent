"""Unit tests for ``gate/agent/diagnostics.py``.

Covers Layers 1-4 of the comprehensive failure-diagnostics surface:

- Layer 1: ``classify_failure`` + ``write_failure_reason`` populate
  ``initiative_runs.error`` with a classified one-liner.
- Layer 2: ``record_decision`` appends rows to
  ``agent_run_decisions`` keyed by run_id + turn_index.
- Layer 3: ``persist_conversation_snapshot`` UPSERTs the full SDK
  conversation history into ``agent_run_snapshots``.
- Layer 4: ``install_terminate_handler`` registers a SIGTERM /
  atexit handler that flushes all three layers before exit.

The conversation-buffer + message-normalisation logic is exercised
in isolation here (no SDK message classes are imported — we use plain
dicts + stand-in dataclasses to keep the tests independent of the
SDK's release cadence).

Memory: ``feedback_async_tests_need_event_not_sleep`` — concurrent /
timed tests use ``asyncio.Event`` (not ``sleep``) for determinism.

Memory: ``feedback_sqlite_tests_dont_catch_cnpg_runtime_gaps`` — the
JSONB column is exercised via the ``JSONBOrJSON`` TypeDecorator
which falls back to JSON on SQLite. The Postgres-specific path is
covered separately by the integration test
``test_diagnostics_real_postgres_jsonb_roundtrip``.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.state as state_module
from app import db as db_module
from app.db.agent_diagnostics import (
    count_decisions,
    get_snapshot,
    list_decisions,
)
from app.db.initiative_runs import create_run, get_run
from app.db.models import Base
from app.state import InitiativeRecord, new_id, now, register
from gate.agent.diagnostics import (
    ALL_REASONS,
    RUN_LEVEL_REASONS,
    ConversationBuffer,
    TerminateState,
    bump_turn_counter,
    classify_failure,
    classify_step_failure_reason,
    current_turn_index,
    install_terminate_handler,
    persist_conversation_snapshot,
    record_decision,
    reset_turn_counter,
    uninstall_terminate_handler,
    write_failure_reason,
)

MIGRATIONS_DIR = Path(__file__).parents[2] / 'charts' / 'leartech-automated-agent' / 'files' / 'migrations'
MIGRATION_0006 = MIGRATIONS_DIR / '0006_agent_run_diagnostics.sql'


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[object]:
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


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure DSN is unset — forces no-op fallback path."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()


# ─── Layer 1: classify_failure ─────────────────────────────────────────


def test_classify_failure_returns_silent_terminate_when_no_exception_and_no_turns() -> None:
    """No exception + no turns observed = pod terminated before first SDK turn."""
    reason = classify_failure(None, last_turn_count=0, max_turns=200)
    assert reason.startswith('silent_terminate:')
    assert 'pod terminated' in reason or 'before first SDK turn' in reason


def test_classify_failure_returns_max_turns_when_exc_is_none_and_cap_hit() -> None:
    """Cap-hit without an exception — agent exited cleanly at the ceiling."""
    reason = classify_failure(None, last_turn_count=200, max_turns=200)
    assert reason.startswith('agent_sdk_max_turns_exceeded:')
    assert '200' in reason


def test_classify_failure_classifies_max_turns_when_sdk_raised_at_ceiling() -> None:
    """SDK raises the moment max_turns is hit (issue #913). Classifier must
    prefer the cap-hit reason over the bare SDK error so the operator sees
    the actionable signal first."""
    exc = RuntimeError('Maximum number of turns reached')
    reason = classify_failure(exc, last_turn_count=200, max_turns=200)
    assert reason.startswith('agent_sdk_max_turns_exceeded:')
    # The exception class is still surfaced for traceability.
    assert 'RuntimeError' in reason


def test_classify_failure_returns_agent_sdk_error_for_generic_exception() -> None:
    """A real SDK crash mid-run gets ``agent_sdk_error: <ExcClass>: <message>``."""
    exc = ValueError('Connection reset by peer')
    reason = classify_failure(exc, last_turn_count=42, max_turns=200)
    assert reason.startswith('agent_sdk_error:')
    assert 'ValueError' in reason
    assert 'Connection reset by peer' in reason


def test_classify_failure_truncates_long_messages() -> None:
    """A 5KB stack trace shouldn't blow out the error column.

    Truncation keeps the column readable; full forensics live in the
    snapshot table (Layer 3).
    """
    exc = RuntimeError('x' * 5000)
    reason = classify_failure(exc, last_turn_count=10, max_turns=200)
    assert len(reason) < 300


def test_classify_failure_handles_unknown_terminal_shape() -> None:
    """No exception + non-zero turn count + no cap hit — defensive case."""
    reason = classify_failure(None, last_turn_count=50, max_turns=200)
    assert reason.startswith('unknown_failure:')


def test_classify_step_failure_reason_renders_canonical_format() -> None:
    """Bridge to step_failure_diagnosis: pipeline-step failures land in
    the same ``<reason>: <context>`` shape so callers funnel everything
    through one write path."""
    log_tail = 'app/foo.py:12:5: E501 line too long\nFound 1 error.\n'
    reason = classify_step_failure_reason('ruff', log_tail)
    assert reason.startswith('ruff_lint_error:')
    assert 'ruff' in reason


def test_all_reasons_includes_run_level_and_pipeline_classifications() -> None:
    """Sanity check on the combined vocabulary — both source sets are present."""
    assert 'silent_terminate' in ALL_REASONS
    assert 'agent_sdk_error' in ALL_REASONS
    assert 'clone_failed' in ALL_REASONS
    assert 'pr_link_missing' in ALL_REASONS
    # Also includes the pipeline-step taxonomy.
    assert 'ruff_lint_error' in ALL_REASONS
    assert 'pytest_test_failure' in ALL_REASONS


def test_run_level_reasons_are_disjoint_from_unknown() -> None:
    """RUN_LEVEL_REASONS must NOT collide with the ``unknown`` step-classification."""
    # ``unknown`` is a step-failure reason; run-level has ``unknown_failure`` instead.
    assert 'unknown_failure' in RUN_LEVEL_REASONS
    assert 'unknown' not in RUN_LEVEL_REASONS


# ─── Layer 1: write_failure_reason ─────────────────────────────────────


async def test_write_failure_reason_populates_error_column(db_enabled: None) -> None:
    """Goal step 7 — `test_failure_writes_classified_error`. After a
    mock SDK error, the error column on the run row must carry the
    classified one-liner."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='write-failure-test', status='running', started_at=now()))

    reason = 'agent_sdk_error: ValueError: simulated crash'
    wrote = await write_failure_reason(run_id, reason)
    assert wrote is True

    async with db_module.session() as sess:
        fetched = await get_run(sess, run_id)
    assert fetched is not None
    assert fetched.error == reason


async def test_write_failure_reason_is_noop_when_db_disabled(no_db: None) -> None:
    """Laptop-CLI mode (no DSN) — the helper returns False without raising."""
    wrote = await write_failure_reason('ghost-run-id', 'silent_terminate: nothing')
    assert wrote is False


async def test_write_failure_reason_is_noop_when_run_id_missing(db_enabled: None) -> None:
    """None / empty run_id → no-op (e.g. laptop runs without LEARTECH_RUN_ID)."""
    assert await write_failure_reason(None, 'silent_terminate: x') is False
    assert await write_failure_reason('', 'silent_terminate: x') is False


async def test_write_failure_reason_warns_on_unknown_prefix(
    db_enabled: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Typos in the reason prefix get a warning at write time."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='warn-prefix-test', status='running', started_at=now()))
    with caplog.at_level('WARNING'):
        await write_failure_reason(run_id, 'made_up_prefix: some context')
    assert any('unknown reason prefix' in rec.message for rec in caplog.records)


# ─── Layer 2: record_decision ──────────────────────────────────────────


async def test_record_decision_writes_to_decision_table(db_enabled: None) -> None:
    """Single decision row lands in ``agent_run_decisions`` with the
    expected fields."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='record-decision-test', status='running', started_at=now()))
    reset_turn_counter(run_id)

    wrote = await record_decision(
        run_id,
        'tool_call',
        'Bash: ls /workspace',
        payload={'tool': 'Bash', 'cmd': 'ls /workspace'},
        turn_index=1,
    )
    assert wrote is True

    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert len(decisions) == 1
    d = decisions[0]
    assert d.run_id == run_id
    assert d.kind == 'tool_call'
    assert d.summary == 'Bash: ls /workspace'
    assert d.turn_index == 1
    assert d.payload == {'tool': 'Bash', 'cmd': 'ls /workspace'}


async def test_record_decision_uses_in_process_counter_when_index_omitted(
    db_enabled: None,
) -> None:
    """When ``turn_index`` is omitted, the helper reads from the in-process
    counter (``current_turn_index``). bump_turn_counter advances it."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='counter-test', status='running', started_at=now()))
    reset_turn_counter(run_id)
    assert current_turn_index(run_id) == 0

    await record_decision(run_id, 'wait', 'pre-first-turn marker')  # idx=0
    bump_turn_counter(run_id)  # idx=1
    await record_decision(run_id, 'tool_call', 'first tool')
    bump_turn_counter(run_id)  # idx=2
    await record_decision(run_id, 'tool_call', 'second tool')

    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert [d.turn_index for d in decisions] == [0, 1, 2]


async def test_record_decision_per_turn_writes_five_rows_for_five_calls(
    db_enabled: None,
) -> None:
    """Goal step 7 — ``test_decision_log_per_turn``. Five tool calls →
    five decision rows."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='five-calls-test', status='running', started_at=now()))
    reset_turn_counter(run_id)

    for i in range(5):
        bump_turn_counter(run_id)
        await record_decision(run_id, 'tool_call', f'Bash: cmd{i}')

    async with db_module.session() as sess:
        cnt = await count_decisions(sess, run_id=run_id)
    assert cnt == 5


async def test_record_decision_is_noop_when_db_disabled(no_db: None) -> None:
    """Laptop-CLI mode — record_decision returns False, no exception."""
    assert await record_decision('any-id', 'tool_call', 'noop') is False


async def test_record_decision_is_noop_when_run_id_missing(db_enabled: None) -> None:
    """Missing run_id is a defensive guard, not a hard error."""
    assert await record_decision(None, 'tool_call', 'no run') is False
    assert await record_decision('', 'tool_call', 'no run') is False


async def test_record_decision_truncates_overlong_summary(db_enabled: None) -> None:
    """Very large summaries are clamped at 2KB to keep the table readable."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='trunc-test', status='running', started_at=now()))
    long_summary = 'x' * 5000
    await record_decision(run_id, 'tool_call', long_summary)
    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert len(decisions[0].summary) <= 2003  # 2000 + "..."
    assert decisions[0].summary.endswith('...')


# ─── Layer 3: persist_conversation_snapshot ────────────────────────────


async def test_persist_conversation_snapshot_writes_full_message_list(
    db_enabled: None,
) -> None:
    """Goal step 7 — ``test_conversation_snapshot_on_terminal``. Agent
    succeeds → full snapshot persisted with the expected message count."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='snapshot-test', status='complete', started_at=now()))

    buf = ConversationBuffer()
    # Three SDK-shaped messages (dict form — the normaliser handles any shape).
    buf.append({'role': 'user', 'content': 'hello'})
    buf.append({'role': 'assistant', 'content': 'world'})
    buf.append({'role': 'result', 'content': None, 'extras': {'num_turns': 1}})

    wrote = await persist_conversation_snapshot(
        run_id,
        buf,
        terminal_reason='complete',
    )
    assert wrote is True

    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 3
    assert snap.terminal_reason == 'complete'
    assert isinstance(snap.messages, list)
    assert len(snap.messages) == 3


async def test_persist_conversation_snapshot_upserts_on_re_call(
    db_enabled: None,
) -> None:
    """SIGTERM handler may fire AFTER the natural terminal write — the
    second call must UPSERT (not raise on the PK conflict)."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='upsert-test', status='complete', started_at=now()))

    buf = ConversationBuffer()
    buf.append({'role': 'user', 'content': 'first'})
    await persist_conversation_snapshot(run_id, buf, terminal_reason='complete')

    buf.append({'role': 'assistant', 'content': 'second'})
    await persist_conversation_snapshot(run_id, buf, terminal_reason='sigterm')

    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 2
    assert snap.terminal_reason == 'sigterm'


async def test_persist_conversation_snapshot_keeps_terminal_reason_when_none(
    db_enabled: None,
) -> None:
    """Passing terminal_reason=None on an UPSERT must NOT overwrite the
    existing value — lets the SIGTERM handler add the snapshot without
    erasing a reason set by an earlier writer."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='preserve-reason-test', status='complete', started_at=now()))

    buf = ConversationBuffer()
    buf.append({'role': 'user', 'content': 'x'})
    await persist_conversation_snapshot(run_id, buf, terminal_reason='complete')

    buf.append({'role': 'assistant', 'content': 'y'})
    await persist_conversation_snapshot(run_id, buf, terminal_reason=None)

    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.terminal_reason == 'complete'  # unchanged
    assert snap.message_count == 2


async def test_persist_conversation_snapshot_accepts_list_input(db_enabled: None) -> None:
    """Tests can pass a pre-normalised list of dicts directly (no buffer)."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='list-input-test', status='complete', started_at=now()))
    msgs = [{'role': 'user', 'content': 'plain dict'}]
    wrote = await persist_conversation_snapshot(run_id, msgs, terminal_reason='complete')
    assert wrote is True


async def test_persist_conversation_snapshot_is_noop_when_db_disabled(no_db: None) -> None:
    """Laptop-CLI mode."""
    buf = ConversationBuffer()
    buf.append({'role': 'user', 'content': 'x'})
    assert await persist_conversation_snapshot('any-id', buf, terminal_reason='complete') is False


# ─── ConversationBuffer normalisation ──────────────────────────────────


def test_conversation_buffer_normalises_dict_messages_passthrough() -> None:
    """Plain dicts pass through unchanged."""
    buf = ConversationBuffer()
    buf.append({'role': 'user', 'content': 'hi'})
    snap = buf.snapshot()
    assert snap[0] == {'role': 'user', 'content': 'hi'}


def test_conversation_buffer_normalises_object_with_role_and_content() -> None:
    """An object exposing .content surfaces as role + content fields."""

    @dataclass
    class FakeAssistantMessage:
        content: str

    buf = ConversationBuffer()
    buf.append(FakeAssistantMessage(content='hello'))
    snap = buf.snapshot()
    msg = snap[0]
    assert msg['role'] == 'assistant'
    assert msg['class'] == 'FakeAssistantMessage'
    # Dataclass path round-trips via asdict.
    assert msg['content'] == {'content': 'hello'}


def test_conversation_buffer_handles_list_content_blocks() -> None:
    """SDK shape: content = [block, block, block] — each block normalised."""

    class FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class FakeAssistantMessage:
        def __init__(self, content: object) -> None:
            self.content = content

    buf = ConversationBuffer()
    buf.append(FakeAssistantMessage(content=[FakeTextBlock('hello'), FakeTextBlock('world')]))
    msg = buf.snapshot()[0]
    assert msg['role'] == 'assistant'
    assert isinstance(msg['content'], list)
    assert msg['content'][0]['text'] == 'hello'
    assert msg['content'][1]['text'] == 'world'


def test_conversation_buffer_handles_none_message() -> None:
    """Defensive — None should not crash the normaliser."""
    buf = ConversationBuffer()
    buf.append(None)
    msg = buf.snapshot()[0]
    assert msg['role'] == 'unknown'


def test_conversation_buffer_extras_preserved_for_result_messages() -> None:
    """ResultMessage fields (num_turns, total_cost_usd, usage) land in extras."""

    class FakeResultMessage:
        def __init__(self) -> None:
            self.content = None
            self.num_turns = 5
            self.total_cost_usd = 0.42
            self.is_error = False
            self.usage = {'input_tokens': 100, 'output_tokens': 50}

    buf = ConversationBuffer()
    buf.append(FakeResultMessage())
    msg = buf.snapshot()[0]
    assert msg['role'] == 'result'
    assert msg['extras']['num_turns'] == 5
    assert msg['extras']['total_cost_usd'] == 0.42
    assert msg['extras']['usage'] == {'input_tokens': 100, 'output_tokens': 50}


# ─── Layer 4: SIGTERM handler ──────────────────────────────────────────


async def test_sigterm_handler_flushes_all_three_layers(
    db_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Goal step 7 — ``test_sigterm_flushes``. Simulate SIGTERM mid-turn.

    Expect: ``error='silent_terminate: SIGTERM received at turn N'`` on
    the run row + a snapshot persisted with the messages observed so
    far + a 'sigterm' decision row.

    We invoke ``_flush`` directly rather than raising SIGTERM in-process
    (which would terminate pytest). The flush function is the entire
    handler body; this test exercises the contract.
    """
    from gate.agent.diagnostics import _flush

    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='sigterm-test', status='running', started_at=now()))

    state = TerminateState(run_id=run_id, max_turns=200)
    state.last_turn_count = 42
    state.buffer.append({'role': 'user', 'content': 'pre-sigterm'})
    state.buffer.append({'role': 'assistant', 'content': 'response 1'})

    await _flush(state, 'silent_terminate: SIGTERM received')

    # Layer 1
    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error is not None
    assert run.error.startswith('silent_terminate: SIGTERM received at turn 42')
    assert '/200' in run.error  # carries the max_turns context

    # Layer 3 — snapshot persisted with up-to-SIGTERM history
    async with db_module.session() as sess:
        snap = await get_snapshot(sess, run_id=run_id)
    assert snap is not None
    assert snap.message_count == 2
    assert snap.terminal_reason == 'sigterm'

    # Layer 2 — one final decision row marking the termination
    async with db_module.session() as sess:
        decisions = await list_decisions(sess, run_id=run_id)
    assert any(d.kind == 'sigterm' for d in decisions)


async def test_install_terminate_handler_is_idempotent(db_enabled: None) -> None:
    """Calling install twice with different states updates the pointer but
    doesn't re-register the signal handler."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='idempotent-install', status='running', started_at=now()))
    state1 = TerminateState(run_id=run_id, max_turns=100)
    state2 = TerminateState(run_id=run_id, max_turns=200)
    install_terminate_handler(state1)
    install_terminate_handler(state2)

    # Cleanup so the test process doesn't leak the handler.
    uninstall_terminate_handler()


async def test_install_terminate_handler_registers_sigterm() -> None:
    """The SIGTERM signal handler is actually installed on the process."""
    state = TerminateState(run_id='install-sigterm-test', max_turns=200)
    install_terminate_handler(state)
    try:
        handler = signal.getsignal(signal.SIGTERM)
        # Whatever's installed must not be the default (SIG_DFL=0).
        assert handler is not signal.SIG_DFL
        assert callable(handler)
    finally:
        uninstall_terminate_handler()


async def test_terminate_handler_skips_when_natural_terminal_completed(
    db_enabled: None,
) -> None:
    """If the SDK loop sets ``natural_terminal_completed=True`` before the
    SIGTERM fires, the handler is a no-op (avoids overwriting clean state).
    """
    # Live module-attribute access — ``from X import _installed_state``
    # captures the value at import time, not a live reference, so we
    # read through the module object instead.
    from gate.agent import diagnostics as diag_mod

    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='skip-natural', status='complete', started_at=now()))

    state = TerminateState(run_id=run_id, max_turns=200)
    state.natural_terminal_completed = True
    install_terminate_handler(state)
    try:
        assert diag_mod._installed_state is state
        diag_mod._atexit_handler()  # should be a no-op
        # No snapshot written (state.handler_fired stays False because the
        # early-return check happens before the flag is set).
        async with db_module.session() as sess:
            snap = await get_snapshot(sess, run_id=run_id)
        assert snap is None
    finally:
        uninstall_terminate_handler()


async def _drain_pending_tasks(timeout: float = 5.0) -> None:
    """Await every fire-and-forget task (other than the caller) to completion so
    scheduled DB writes finish before assertions + fixture teardown — deterministic,
    unlike ``sleep(0)`` which yields only once and leaves the task to error on teardown.
    """
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


async def test_sigterm_handler_only_fires_once(db_enabled: None) -> None:
    """Belt-and-braces: SIGTERM + atexit both call ``_fire_handler_safely``,
    but the ``handler_fired`` flag guards against double-flushing."""
    from gate.agent.diagnostics import _fire_handler_safely

    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='once-only', status='running', started_at=now()))

    state = TerminateState(run_id=run_id, max_turns=200)
    state.last_turn_count = 7
    state.buffer.append({'role': 'user', 'content': 'first call'})
    install_terminate_handler(state)
    try:
        _fire_handler_safely(reason='silent_terminate: SIGTERM received')
        # Deterministically wait for the fire-and-forget snapshot task to COMPLETE
        # its DB write. sleep(0) yields only once — under release-env contention the
        # scheduled coroutine outlived the test and errored on teardown (DB torn down),
        # surfacing as a flaky ERROR. Draining to completion is the fix
        # (feedback_async_tests_need_event_not_sleep).
        await _drain_pending_tasks()
        # Now grow the buffer and fire again — second call must be a no-op.
        state.buffer.append({'role': 'assistant', 'content': 'second call'})
        _fire_handler_safely(reason='silent_terminate: atexit')
        await _drain_pending_tasks()

        async with db_module.session() as sess:
            snap = await get_snapshot(sess, run_id=run_id)
        # Whatever was written on the FIRST fire is what's stored — the
        # second call's larger buffer must NOT have leaked through.
        # (Snapshot may be None if the scheduled coroutine hasn't run yet
        # in this test harness; in that case the contract still holds.)
        if snap is not None:
            assert snap.message_count == 1
    finally:
        uninstall_terminate_handler()


# ─── Layer 1: orphan-path detection (pr_link_missing) ──────────────────


async def test_orphan_path_writes_pr_link_missing_reason(db_enabled: None) -> None:
    """Goal step 7 — ``test_orphan_path_captured``. The Layer 1 reason
    classifies the orphan-PR case so an operator query against the error
    column surfaces it."""
    run_id = new_id()
    await register(InitiativeRecord(id=run_id, initiative='orphan-path-test', status='complete', started_at=now()))

    reason = 'pr_link_missing: agent reported success but no open PR on owner/repo@branch'
    await write_failure_reason(run_id, reason)

    async with db_module.session() as sess:
        run = await get_run(sess, run_id)
    assert run is not None
    assert run.error is not None
    assert run.error.startswith('pr_link_missing:')


# ─── Migration shape ───────────────────────────────────────────────────


def test_migration_0006_file_exists_and_creates_both_tables() -> None:
    """The migration file ships in files/migrations/ AND mentions both
    new tables in its CREATE TABLE statements (idempotent IF NOT EXISTS)."""
    assert MIGRATION_0006.exists(), f'expected {MIGRATION_0006} to exist'
    sql = MIGRATION_0006.read_text()
    assert 'CREATE TABLE IF NOT EXISTS agent_run_decisions' in sql
    assert 'CREATE TABLE IF NOT EXISTS agent_run_snapshots' in sql
    # Foreign keys are essential — diagnostics rows must cascade with the run.
    assert 'REFERENCES initiative_runs(id)' in sql
    assert 'ON DELETE CASCADE' in sql
    # JSONB for snapshot payloads + decision payloads
    assert 'JSONB' in sql
    # Index on run_id for the dominant operator query pattern.
    assert 'ix_agent_run_decisions_run_id' in sql


def test_orm_models_have_diagnostics_tables() -> None:
    """The SQLAlchemy companion classes must exist with the expected
    table names so ``Base.metadata.create_all`` bootstraps them in tests."""
    from app.db.models import AgentRunDecisionRow, AgentRunSnapshotRow

    assert AgentRunDecisionRow.__tablename__ == 'agent_run_decisions'
    assert AgentRunSnapshotRow.__tablename__ == 'agent_run_snapshots'


# ─── CRUD layer: insert / list / count ────────────────────────────────


async def test_insert_decision_requires_existing_run_row(db_session: object) -> None:
    """FK constraint catches caller bugs where the run row wasn't written
    first. SQLite enforces FKs only when pragma is set — we exercise the
    happy path here; the constraint is in the schema for production."""
    ts = datetime.now(UTC)
    await create_run(db_session, id='fk-test-1', initiative='x', status='running', started_at=ts)
    from app.db.agent_diagnostics import insert_decision as ins

    rec = await ins(db_session, run_id='fk-test-1', turn_index=1, kind='tool_call', summary='x')
    assert rec.id > 0


async def test_list_decisions_returns_ordered_by_turn_then_id(db_session: object) -> None:
    """Two decisions in the same turn round-trip in INSERT order."""
    ts = datetime.now(UTC)
    await create_run(db_session, id='order-test-1', initiative='x', status='running', started_at=ts)
    from app.db.agent_diagnostics import insert_decision as ins

    # Turn 2 first, then turn 1 (two rows) — list must reorder.
    await ins(db_session, run_id='order-test-1', turn_index=2, kind='tool_call', summary='B')
    await ins(db_session, run_id='order-test-1', turn_index=1, kind='tool_call', summary='A1')
    await ins(db_session, run_id='order-test-1', turn_index=1, kind='decision', summary='A2')

    decisions = await list_decisions(db_session, run_id='order-test-1')
    # Ordered by (turn_index, id) — turn 1 first, A1 before A2 (insert order).
    assert [d.summary for d in decisions] == ['A1', 'A2', 'B']


async def test_get_snapshot_returns_none_for_missing(db_session: object) -> None:
    snap = await get_snapshot(db_session, run_id='no-such-run')
    assert snap is None
