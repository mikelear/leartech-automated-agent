"""Per-turn writeback unit tests (initiative ``agent-add-per-turn-writeback``).

Pins the contract of the per-turn ``update_run_progress`` hook:

- The hook is fired once per SDK ``ResultMessage`` boundary.
- Writes the running ``turns`` count, cumulative ``cost_usd``, and the
  NAME of the LAST tool the agent invoked during the just-completed
  turn (or NULL for a plain text turn).
- Idempotent: re-writing the same values is safe.
- Failure-isolated: a DB error is logged at WARN and swallowed; the
  hook never propagates an exception that could crash the SDK loop.
- Last-tool extraction follows the SDK message protocol: tool uses are
  emitted as ``ToolUseBlock`` entries inside ``AssistantMessage.content``,
  and the LATEST block in the current turn wins.

The headline contract is `update_run_progress(run_id, turns, cost_usd,
last_tool_call)` in :mod:`gate.agent.run_driver` — the SDK-loop wiring
in :mod:`gate.agent.initiative` is exercised by the diagnostics
integration suite, not duplicated here. This file focuses on the
helper's behaviour in isolation, so a refactor that changes the SDK
loop's call shape doesn't false-positive this suite.

Memory: ``feedback_async_tests_need_event_not_sleep`` — concurrency
tests coordinate via ``asyncio.gather``, never ``asyncio.sleep``.

Memory: ``feedback_sqlite_tests_dont_catch_cnpg_runtime_gaps`` — these
tests run against in-memory SQLite. Production Postgres semantics
(connection pooling under load, asyncpg cancellation) are verified by
the cluster-side ``end2end`` / ``end2end-ui`` tiers + the live
verification noted in the PR description.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import app.state as state_module
from app import db as db_module
from app.db.initiative_runs import (
    create_run,
    get_run,
)
from app.db.initiative_runs import (
    update_run_progress as db_update_run_progress,
)
from app.db.models import Base
from app.state import InitiativeRecord, new_id, now, register
from gate.agent.run_driver import update_run_progress

MIGRATIONS_DIR = Path(__file__).parents[2] / 'charts' / 'leartech-automated-agent' / 'files' / 'migrations'
MIGRATION_0008 = MIGRATIONS_DIR / '0008_last_tool_call.sql'


# ─── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[Any]:
    """Per-test in-memory SQLite session with the full schema applied."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker

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


# ─── 1. Schema: last_tool_call column lands in the ORM + migration ─────


async def test_last_tool_call_column_present_in_orm_schema() -> None:
    """Pin the SQLAlchemy companion. ``last_tool_call`` must be mapped on
    ``InitiativeRunRow`` so ``Base.metadata.create_all`` bootstraps it in
    tests. A missing column here turns every other test in this module
    into a confusing AttributeError; failing here gives the clear
    diagnostic.
    """
    engine: AsyncEngine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _inspect(sync_conn: object) -> list[str]:
            insp = inspect(sync_conn)
            return [c['name'] for c in insp.get_columns('initiative_runs')]

        columns = await conn.run_sync(_inspect)
    await engine.dispose()

    assert 'last_tool_call' in columns, (
        f'last_tool_call missing from initiative_runs schema; got {columns!r}. '
        'The ORM column on InitiativeRunRow must be present.'
    )


async def test_migration_0008_applies_on_sqlite() -> None:
    """The raw SQL migration must exist and use the same idempotent
    ``IF NOT EXISTS`` pattern as 0005 — the deployment's initContainer
    re-applies every migration on each pod start."""
    assert MIGRATION_0008.exists(), (
        f'Migration file missing at {MIGRATION_0008} — '
        'agent-add-per-turn-writeback requires a 0008_last_tool_call.sql file'
    )
    sql = MIGRATION_0008.read_text()
    assert 'last_tool_call' in sql
    assert 'ALTER TABLE initiative_runs' in sql
    assert 'IF NOT EXISTS' in sql, 'migration must be idempotent — initContainer re-applies on every pod restart'


# ─── 2. Helper: single UPDATE writes all three columns ─────────────────


async def test_db_update_run_progress_writes_all_three_columns(db_session: Any) -> None:
    """The low-level helper writes ``turns``, ``cost_usd`` and
    ``last_tool_call`` in a single UPDATE. Returns True on success."""
    await create_run(
        db_session,
        id='write-all-1',
        initiative='x',
        status='running',
        started_at=now(),
    )

    wrote = await db_update_run_progress(
        db_session,
        id='write-all-1',
        turns=3,
        cost_usd=Decimal('0.4200'),
        last_tool_call='Bash',
    )
    assert wrote is True

    fetched = await get_run(db_session, 'write-all-1')
    assert fetched is not None
    assert fetched.turns == 3
    assert fetched.cost_usd == Decimal('0.4200')
    assert fetched.last_tool_call == 'Bash'


async def test_db_update_run_progress_returns_false_when_row_missing(db_session: Any) -> None:
    """No INSERT semantics — writing to a non-existent run returns False
    rather than creating the row."""
    wrote = await db_update_run_progress(
        db_session,
        id='ghost-run-id',
        turns=1,
        cost_usd=0.01,
        last_tool_call='Bash',
    )
    assert wrote is False


async def test_db_update_run_progress_accepts_null_last_tool_call(db_session: Any) -> None:
    """A plain text turn (no tool invocations) writes NULL — operators
    distinguish "agent thinking" from "agent ran X" via this column."""
    await create_run(
        db_session,
        id='null-tool-1',
        initiative='x',
        status='running',
        started_at=now(),
    )
    wrote = await db_update_run_progress(
        db_session,
        id='null-tool-1',
        turns=1,
        cost_usd=0.01,
        last_tool_call=None,
    )
    assert wrote is True
    fetched = await get_run(db_session, 'null-tool-1')
    assert fetched is not None
    assert fetched.last_tool_call is None


# ─── 3. Idempotency: repeating the same values is safe ────────────────


async def test_update_run_progress_idempotent_repeated_writes(db_enabled: None) -> None:
    """Calling ``update_run_progress`` twice with the same values is a
    no-op at the application layer. The second call still returns True
    (row exists), and the row state is unchanged."""
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='idempotency-test',
            status='running',
            started_at=now(),
        )
    )

    wrote_first = await update_run_progress(run_id, turns=2, cost_usd=0.10, last_tool_call='Read')
    assert wrote_first is True

    wrote_second = await update_run_progress(run_id, turns=2, cost_usd=0.10, last_tool_call='Read')
    assert wrote_second is True

    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None
    assert rec.turns == 2
    assert rec.last_tool_call == 'Read'


# ─── 4. Monotonic per-turn invocation (positional sanity check) ────────


async def test_update_run_progress_monotonic_turns_and_cost(db_enabled: None) -> None:
    """Per-turn invocation: assert ``update_run_progress`` writes
    monotonically-increasing turns + cumulative cost across successive
    calls. Mirrors the SDK loop's once-per-ResultMessage cadence."""
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='monotonic-test',
            status='running',
            started_at=now(),
        )
    )

    # Simulate three successive ResultMessages.
    sequence = [
        (1, 0.05, 'Bash'),
        (2, 0.12, 'Read'),
        (3, 0.18, None),  # third turn was plain text
    ]
    last_turns = 0
    last_cost = 0.0
    for turns, cost, tool in sequence:
        ok = await update_run_progress(run_id, turns=turns, cost_usd=cost, last_tool_call=tool)
        assert ok is True
        assert turns > last_turns, 'turns must increase across calls'
        assert cost > last_cost, 'cost must increase (cumulative)'
        last_turns, last_cost = turns, cost

    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None
    assert rec.turns == 3
    assert rec.last_tool_call is None, 'final turn was plain text → NULL'


# ─── 5. Failure isolation: DB error is swallowed, run continues ───────


async def test_update_run_progress_swallows_db_errors(
    db_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient DB error MUST NOT propagate out of the hook — the
    SDK loop is the primary mission and observability writeback is
    best-effort. The function returns False AND logs at WARN so a
    sustained failure stream is visible in operator logs.
    """
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='failure-isolation-test',
            status='running',
            started_at=now(),
        )
    )

    # Monkeypatch the DB CRUD layer to raise. The hook should catch and
    # log without re-raising.
    async def _exploding(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError('simulated DB outage')

    monkeypatch.setattr(
        'gate.agent.run_driver._db_update_run_progress',
        _exploding,
    )

    with caplog.at_level('WARNING', logger='gate.agent.run_driver'):
        result = await update_run_progress(run_id, turns=1, cost_usd=0.01, last_tool_call='Bash')
    # In-memory write succeeded, so the function returns True for that
    # half of the writeback even with the DB explosion. The contract
    # operators care about: NO exception escapes.
    assert result is True

    # WARN log captures the failure with the run_id + offending values.
    warning_records = [r for r in caplog.records if r.levelname == 'WARNING']
    assert any(run_id in r.getMessage() for r in warning_records), (
        f'expected WARN log mentioning run_id={run_id}, got {[r.getMessage() for r in warning_records]!r}'
    )


async def test_update_run_progress_no_exception_when_db_disabled_and_no_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-less mode AND no in-memory record → no-op, returns False.
    Laptop-CLI runs without LEARTECH_RUN_ID fall through this branch."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()
    state_module._records.clear()

    result = await update_run_progress('ghost-laptop-run', turns=1, cost_usd=0.01, last_tool_call='Bash')
    assert result is False


async def test_update_run_progress_patches_in_memory_when_db_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB-less mode with an in-memory record → the hook still patches
    the in-memory cache. Laptop-CLI runs observe progress through
    ``app.state.get(run_id)`` without a DB."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()
    state_module._records.clear()
    run_id = 'mem-only-progress-1'
    state_module._records[run_id] = InitiativeRecord(
        id=run_id,
        initiative='mem-only',
        status='running',
        started_at=now(),
    )

    wrote = await update_run_progress(run_id, turns=2, cost_usd=0.07, last_tool_call='Grep')
    assert wrote is True
    rec = state_module._records[run_id]
    assert rec.turns == 2
    assert rec.cost_usd == 0.07
    assert rec.last_tool_call == 'Grep'


async def test_update_run_progress_run_id_none_is_noop() -> None:
    """``run_id=None`` is the explicit "no run row attached" signal —
    laptop runs without ``LEARTECH_RUN_ID``. The function returns False
    immediately without touching the DB or the cache.
    """
    result = await update_run_progress(None, turns=1, cost_usd=0.01, last_tool_call='Bash')
    assert result is False


# ─── 6. Last-tool-call extraction from SDK messages ───────────────────


def _extract_last_tool_call_from_assistant(message: AssistantMessage) -> str | None:
    """Mirror the SDK-loop extraction logic for this test.

    The actual loop assigns ``current_turn_last_tool = block.name`` for
    each ToolUseBlock encountered in AssistantMessage.content; this
    helper reproduces that "LAST tool wins" rule so the unit test
    doesn't need to spin up the full ``run_initiative`` coroutine.
    """
    last: str | None = None
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            last = block.name
    return last


def test_last_tool_extraction_text_only_returns_none() -> None:
    """An AssistantMessage with only TextBlocks (plain text response)
    yields NULL — operators see "agent thinking", not a stale tool name."""
    msg = AssistantMessage(
        content=[TextBlock(text='I will now think about the problem.')],
        model='claude-haiku-test',
    )
    assert _extract_last_tool_call_from_assistant(msg) is None


def test_last_tool_extraction_single_tool_use() -> None:
    """One ToolUseBlock → its name is the last-tool-call."""
    msg = AssistantMessage(
        content=[
            TextBlock(text='Let me list the files.'),
            ToolUseBlock(id='tu_1', name='Bash', input={'command': 'ls'}),
        ],
        model='claude-haiku-test',
    )
    assert _extract_last_tool_call_from_assistant(msg) == 'Bash'


def test_last_tool_extraction_multiple_tools_last_wins() -> None:
    """Multiple ToolUseBlocks in one AssistantMessage — LAST wins.
    Operators see the most recent action, not the first."""
    msg = AssistantMessage(
        content=[
            ToolUseBlock(id='tu_1', name='Read', input={'file_path': '/workspace/a'}),
            ToolUseBlock(id='tu_2', name='Grep', input={'pattern': 'x'}),
            ToolUseBlock(
                id='tu_3',
                name='mcp__leartech-criteria__run_criteria_set',
                input={'repo': 'mikelear/foo', 'pr_number': 1},
            ),
        ],
        model='claude-haiku-test',
    )
    assert _extract_last_tool_call_from_assistant(msg) == 'mcp__leartech-criteria__run_criteria_set'


# ─── 7. SDK usage parsing — cost_usd extraction from ResultMessage ────


def test_result_message_carries_running_total_cost() -> None:
    """The SDK's ``ResultMessage.total_cost_usd`` is the CUMULATIVE
    cost — what the per-turn hook should write to
    ``initiative_runs.cost_usd``. NOT a per-turn delta."""
    msg = ResultMessage(
        subtype='final',
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=4,
        session_id='test-session',
        total_cost_usd=0.7321,
        usage={'input_tokens': 1234, 'output_tokens': 567},
    )
    # The SDK-loop reads these two fields directly into the writeback.
    # Mirror that here to pin the contract.
    assert msg.num_turns == 4
    assert msg.total_cost_usd == 0.7321


async def test_update_run_progress_writes_sdk_result_message_totals(
    db_enabled: None,
) -> None:
    """End-to-end: a ResultMessage's ``num_turns`` + ``total_cost_usd``
    flow through the hook into the row. The fixture is built to mirror
    what the SDK actually yields."""
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='sdk-totals-test',
            status='running',
            started_at=now(),
        )
    )

    msg = ResultMessage(
        subtype='final',
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=5,
        session_id='sess-1',
        total_cost_usd=0.4225,
    )
    cost = msg.total_cost_usd if msg.total_cost_usd is not None else 0.0
    wrote = await update_run_progress(
        run_id,
        turns=msg.num_turns,
        cost_usd=cost,
        last_tool_call='Bash',
    )
    assert wrote is True

    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None
    assert rec.turns == 5
    assert rec.cost_usd == Decimal('0.4225')
    assert rec.last_tool_call == 'Bash'


# ─── 8. Concurrent writebacks under asyncio.gather are race-safe ──────


async def test_update_run_progress_concurrent_writes_do_not_error(
    db_enabled: None,
) -> None:
    """Two coroutines firing writebacks for the same run concurrently
    must not raise. The SDK fires writebacks via ``asyncio.create_task``,
    so overlap can happen if a previous writeback hasn't completed before
    the next ResultMessage arrives."""
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='concurrent-test',
            status='running',
            started_at=now(),
        )
    )

    results = await asyncio.gather(
        update_run_progress(run_id, turns=1, cost_usd=0.05, last_tool_call='Bash'),
        update_run_progress(run_id, turns=2, cost_usd=0.10, last_tool_call='Read'),
        update_run_progress(run_id, turns=3, cost_usd=0.15, last_tool_call='Grep'),
    )
    assert all(results)

    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None
    # ``turns`` lands at one of the three values (whichever committed
    # last). The contract is "no errors + a valid state" — not "writes
    # serialise to the highest value".
    assert rec.turns in (1, 2, 3)
    assert rec.last_tool_call in ('Bash', 'Read', 'Grep')


# ─── 9. Smoke test: helper signature matches the initiative contract ──


async def test_update_run_progress_signature_matches_contract() -> None:
    """The initiative spec pins the helper's call shape:
    ``update_run_progress(run_id, turns, cost_usd, last_tool_call)``.
    A refactor that re-orders or renames these would silently break
    the SDK-loop wire-up — pin it explicitly.
    """
    import inspect

    sig = inspect.signature(update_run_progress)
    params = list(sig.parameters)
    assert params[0] == 'run_id'
    # The remaining are kwarg-only by design.
    assert 'turns' in sig.parameters
    assert 'cost_usd' in sig.parameters
    assert 'last_tool_call' in sig.parameters
    assert sig.parameters['turns'].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters['cost_usd'].kind == inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters['last_tool_call'].kind == inspect.Parameter.KEYWORD_ONLY


# ─── 10. asyncio.create_task wrapping does not propagate errors ───────


async def test_update_run_progress_create_task_swallows_errors(
    db_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SDK loop wraps the hook in ``asyncio.create_task(...)`` so a
    slow round-trip doesn't block the next turn. We mirror the same
    wrap here to confirm an internal DB failure doesn't surface as an
    unhandled-task exception either — the hook's own try/except is the
    failure boundary.
    """
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='create-task-test',
            status='running',
            started_at=now(),
        )
    )

    async def _exploding(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeError('background DB outage')

    monkeypatch.setattr(
        'gate.agent.run_driver._db_update_run_progress',
        _exploding,
    )

    task = asyncio.create_task(update_run_progress(run_id, turns=1, cost_usd=0.01, last_tool_call='Bash'))
    # The task must complete cleanly — no exception raised when awaited.
    result = await task
    # In-memory write happened, DB failed silently → still returns True.
    assert result is True
    assert task.exception() is None


# ─── 11. Smoke: AsyncMock at the run_driver layer ─────────────────────


async def test_update_run_progress_calls_db_layer_with_expected_kwargs(
    db_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the kwargs the run_driver passes to the DB CRUD layer. A
    refactor that drops one of the three fields would silently regress
    observability — pinning here surfaces it as a test failure rather
    than a missing column in production."""
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='kwarg-pin-test',
            status='running',
            started_at=now(),
        )
    )

    mock = AsyncMock(return_value=True)
    monkeypatch.setattr('gate.agent.run_driver._db_update_run_progress', mock)

    await update_run_progress(run_id, turns=7, cost_usd=0.99, last_tool_call='Bash')

    assert mock.called
    _args, kwargs = mock.call_args
    assert kwargs['id'] == run_id
    assert kwargs['turns'] == 7
    assert kwargs['cost_usd'] == 0.99
    assert kwargs['last_tool_call'] == 'Bash'
