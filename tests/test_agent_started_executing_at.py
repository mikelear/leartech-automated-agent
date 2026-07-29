"""Pin the wiring: the SDK loop must invoke ``mark_first_turn`` exactly once.

This file plugs the regression-gap that let
``initiative_runs.started_executing_at`` ship NULL on real runs even though
``mark_first_turn`` itself was unit-tested in isolation.

The pre-existing unit tests in ``tests/agent/test_run_driver_first_turn.py``
exercise ``mark_first_turn`` directly — they pass even when the SDK-loop
call site is silently broken. The bug surfaced in cluster run
``a9699b453342`` (2026-06-12) where the agent ran to completion but the
column stayed NULL for the entire lifetime. The diagnostic root cause:
no integration-level test simulated the SDK-loop calling the wrapper, so
a regression in the wire-up went unnoticed.

Strategy: mock ``claude_agent_sdk.query`` with a controlled message
sequence (mirroring the SDK's protocol shape) and assert ``mark_first_turn``
is called exactly once when the first ``AssistantMessage`` arrives. A
second turn does NOT re-call the writer — idempotency at the call-site,
on top of the SQL-WHERE guard already exercised by the unit tests.

Memory: ``feedback_orch_cant_see_pod_problems`` — the orchestrator's
"is the agent actually executing?" diagnostic depends on this column
being populated within ~seconds of pod startup. A silently-failing
writer leaves orch + chat with no signal, which is the exact stall the
column was added to fix.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import app.state as state_module
from app import db as db_module
from app.db.initiative_runs import create_run, get_run
from app.db.models import Base
from app.state import now
from gate.agent.initiative import RunSummary, run_initiative

# ─── Test doubles (mirror tests/test_initiative_pr_open_emit.py) ──────


@dataclass
class _FakeRepo:
    repo: str = 'mikelear/example-svc'
    branch: str = 'agent/example-fix'
    base: str = 'main'

    @property
    def qualified_repo(self) -> str:
        return self.repo


@dataclass
class _FakeInitiative:
    """Minimal stand-in for the loader's Initiative dataclass."""

    name: str = 'example-initiative'
    is_multi_repo: bool = False
    repos: list[_FakeRepo] = field(default_factory=lambda: [_FakeRepo()])
    # v6p0.5 step 2 — the agent's prompt construction reads this field
    # to inject prior-attempt feedback. Empty default means the no-op
    # branch is taken, mirroring a fresh first-attempt run.
    feedback_payloads: list[dict[str, Any]] = field(default_factory=list)
    # Hold-as-init-option — the agent's prompt construction reads this
    # field to decide whether to render the `/hold` posting instruction.
    # Default False mirrors the "let Tide auto-merge on green" shape.
    hold: bool = False
    # Test-mode directive — default None so these tests hit the real SDK
    # loop path instead of the test-mode short-circuit added later.
    test_mode: dict[str, object] | None = None

    @property
    def primary(self) -> _FakeRepo:
        return self.repos[0]


def _make_query_yielding(messages: list[Any]):
    """Build a fake ``query()`` returning an async iterator over the messages."""

    async def fake_query(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg

    return fake_query


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Set up a minimal cwd + initiative YAML so ``run_initiative`` doesn't
    touch the network or the filesystem outside the test's tmpdir."""
    repo_root = tmp_path / 'example-svc'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — content is irrelevant; loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


def _result_message(turns: int = 1, cost: float = 0.01) -> ResultMessage:
    """Build a minimally-populated ResultMessage closing a turn."""
    return ResultMessage(
        subtype='success',
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=turns,
        session_id='test-session',
        total_cost_usd=cost,
        usage={'input_tokens': 0, 'output_tokens': 0},
    )


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_with_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[str]:
    """Enable a file-backed SQLite DB, create a row, and yield its run_id.

    File-backed (not ``:memory:``) so the row INSERT and the subsequent
    UPDATE landing in different sessions — and across the engine
    dispose/re-init that ``run_initiative`` triggers — all see the same
    schema + data. The test re-inits the engine after run_initiative to
    query the row back.
    """
    db_path = tmp_path / 'first_turn_test.sqlite'
    dsn = f'sqlite+aiosqlite:///{db_path}'
    monkeypatch.setenv(db_module.DSN_ENV, dsn)
    db_module._reset_for_tests()
    state_module._records.clear()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    run_id = 'test-run-id-001'
    async with db_module.session() as sess:
        await create_run(
            sess,
            id=run_id,
            initiative='wiring-test',
            status='running',
            started_at=now(),
        )

    yield run_id

    try:
        await db_module.dispose_engine()
    except Exception:  # noqa: BLE001, S110 — best-effort teardown; engine may already be disposed by run_initiative  # noqa: E501
        pass  # noqa: S110
    db_module._reset_for_tests()
    state_module._records.clear()


# ─── Headline test: SDK loop calls mark_first_turn on first AssistantMessage ──


async def test_sdk_loop_invokes_mark_first_turn_once_on_first_assistant_message(
    db_with_run: str,
    tmp_path: Path,
) -> None:
    """Reproduces the production bug: even though ``mark_first_turn`` is
    wired into the SDK loop in ``initiative.py``, no test exercised the
    integration — so a regression that silently breaks the wire-up
    (LEARTECH_RUN_ID resolution, env-read timing, engine lifecycle) goes
    unnoticed. This test pins the contract.

    Concretely:

    1. A real DB row exists with started_executing_at IS NULL.
    2. LEARTECH_RUN_ID points to that row.
    3. The SDK loop receives one AssistantMessage + ResultMessage.
    4. After the loop, the row's started_executing_at must be set.
    """
    run_id = db_with_run

    messages = [
        AssistantMessage(
            content=[TextBlock(text='Starting the work.')],
            model='claude',
        ),
        _result_message(turns=1),
    ]

    with (
        patch.dict(
            os.environ,
            {'ANTHROPIC_API_KEY': 'test', 'LEARTECH_RUN_ID': run_id},
            clear=False,
        ),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    assert isinstance(summary, RunSummary)

    # ``run_initiative`` disposes the engine on the way out; re-init so
    # the test can query the row back through a fresh session factory.
    # The DSN is still set in env, so init_engine() rebuilds against the
    # same shared-cache SQLite file the loop wrote to.
    db_module.init_engine()

    # The row MUST have started_executing_at populated. The unit-tested
    # mark_first_turn writes ``now()``; we just assert non-NULL — the
    # exact wall-clock value isn't the contract this test is pinning.
    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None, f'run row {run_id} disappeared during the test'
    assert rec.started_executing_at is not None, (
        'started_executing_at was NULL after the SDK loop processed an AssistantMessage. '
        'This is the production bug from run a9699b453342: the writer code in run_driver.py '
        'exists but the SDK-loop call site is not actually invoking it on real runs.'
    )


async def test_sdk_loop_does_not_re_invoke_mark_first_turn_on_subsequent_assistant_messages(
    db_with_run: str,
    tmp_path: Path,
) -> None:
    """A multi-turn run sees several AssistantMessages, but the first-turn
    hook must fire ONLY on the first one. Re-calling the writer is harmless
    at the SQL level (the WHERE started_executing_at IS NULL guard is
    idempotent), but the no-point-making-the-call discipline keeps the
    per-iteration overhead at zero after the first turn.
    """
    run_id = db_with_run

    # Five AssistantMessages interleaved with tool-result UserMessages and
    # ResultMessages — realistic shape of a 3-turn agent run.
    messages: list[Any] = [
        AssistantMessage(
            content=[
                TextBlock(text='Turn 1'),
                ToolUseBlock(id='tu1', name='Bash', input={'command': 'ls'}),
            ],
            model='claude',
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id='tu1', content='ok', is_error=False)],
        ),
        _result_message(turns=1),
        AssistantMessage(
            content=[TextBlock(text='Turn 2 thinking')],
            model='claude',
        ),
        _result_message(turns=2),
        AssistantMessage(
            content=[TextBlock(text='Turn 3 done')],
            model='claude',
        ),
        _result_message(turns=3),
    ]

    # Patch mark_first_turn to count invocations. The writer is imported
    # into initiative.py via ``from gate.agent.run_driver import mark_first_turn``,
    # so the patch target is the local binding in initiative.py.
    mock_writer = AsyncMock(return_value=True)
    with (
        patch.dict(
            os.environ,
            {'ANTHROPIC_API_KEY': 'test', 'LEARTECH_RUN_ID': run_id},
            clear=False,
        ),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
        patch('gate.agent.initiative.mark_first_turn', mock_writer),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    # Exactly once — the in-process ``first_turn_recorded`` flag must
    # short-circuit every subsequent AssistantMessage.
    assert mock_writer.call_count == 1, (
        f'mark_first_turn must be called EXACTLY once across the whole run; '
        f'got {mock_writer.call_count} calls. The wrapper in initiative.py '
        '(`_record_first_turn_once`) is responsible for this gate via its '
        '`first_turn_recorded` flag — a regression there means we hammer '
        'the DB with redundant UPDATEs on every turn.'
    )


# ─── Defence-in-depth: failure observability via WARN log ─────────────


async def test_sdk_loop_logs_at_warn_when_writer_raises(
    db_with_run: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The wrapper catches exceptions from ``mark_first_turn`` so the SDK
    loop never aborts on an observability hiccup. But a SILENT swallow
    is how the production regression went unnoticed for so long — the
    only signal was the eventual NULL column. The wrapper must emit at
    least one WARN-level log line citing run_id + the exception when
    the writer raises, so a log-aggregation query
    (``level:WARN message:"started_executing_at"``) surfaces the next
    regression within seconds of it landing.
    """
    run_id = db_with_run

    # Build a writer that raises — simulating a DB transient or a
    # mis-configured engine.
    async def _exploding(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError('simulated DB outage on first-turn write')

    messages = [
        AssistantMessage(
            content=[TextBlock(text='Starting the work.')],
            model='claude',
        ),
        _result_message(turns=1),
    ]

    with (
        patch.dict(
            os.environ,
            {'ANTHROPIC_API_KEY': 'test', 'LEARTECH_RUN_ID': run_id},
            clear=False,
        ),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
        patch('gate.agent.initiative.mark_first_turn', _exploding),
        caplog.at_level(logging.WARNING, logger='gate.agent.initiative'),
    ):
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    # The run completed (no exception escaped the SDK loop) — confirming
    # that the writer's failure is non-fatal to the SDK loop.
    assert isinstance(summary, RunSummary)

    # AND a WARN log line was emitted citing the run_id and the cause.
    # The exact format isn't pinned — operators query by run_id and
    # the column name (or simulated outage); a substring match keeps the
    # contract loose enough to allow phrasing tweaks.
    warning_messages = [r.getMessage() for r in caplog.records if r.levelname == 'WARNING']
    assert any(run_id in m and ('started_executing_at' in m or 'first_turn' in m) for m in warning_messages), (
        'Expected a WARNING log line citing both the run_id and either '
        f'"started_executing_at" or "first_turn"; got: {warning_messages!r}. '
        'Without this log line a silently-failing writer is invisible to operators.'
    )


# ─── Defence-in-depth: writer is invoked even when the first AssistantMessage
# ─── contains only ThinkingBlock or empty content ─────────────────────


async def test_sdk_loop_invokes_writer_even_when_first_assistant_message_is_text_only(
    db_with_run: str,
    tmp_path: Path,
) -> None:
    """Some real runs start with the agent writing a single sentence
    (no tool use) before issuing its first tool call — the first
    AssistantMessage might contain only a TextBlock. The hook must
    still fire: "agent has begun executing" is the contract, not
    "agent has called a tool". This pin guards against a future
    refactor that gates the hook on ``ToolUseBlock`` presence.
    """
    run_id = db_with_run

    messages = [
        AssistantMessage(
            content=[TextBlock(text='Let me think about this first.')],
            model='claude',
        ),
        _result_message(turns=1),
    ]

    with (
        patch.dict(
            os.environ,
            {'ANTHROPIC_API_KEY': 'test', 'LEARTECH_RUN_ID': run_id},
            clear=False,
        ),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    # See the headline test for the engine re-init rationale.
    db_module.init_engine()

    async with db_module.session() as sess:
        rec = await get_run(sess, run_id)
    assert rec is not None
    assert rec.started_executing_at is not None, (
        "started_executing_at must be populated even when the agent's first response is plain text with no tool use."
    )
