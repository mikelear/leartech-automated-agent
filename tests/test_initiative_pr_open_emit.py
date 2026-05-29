"""End-to-end test for the D.5.1.4 early-emit watcher inside ``run_initiative``.

The earlier ``test_initiative_crash_sticky.py`` covers the helper
functions in isolation (``_extract_pr_from_tool_result``,
``_build_pr_url_pattern``) but never exercises the message-loop
integration that decides WHICH SDK message type the watcher fires on.
That gap let a real bug ship: the watcher matched ``ToolResultBlock``
nested inside ``AssistantMessage.content``, but per the SDK protocol
``ToolResultBlock`` arrives in ``UserMessage.content`` (the tool's
response is a "user" turn, not assistant). The marker line therefore
never emitted in production — silently falling through to the
``_resolve_pr_number`` GH-fallback path on every run.

These tests mock ``claude_agent_sdk.query`` with hand-built message
sequences shaped like real SDK output and assert the marker reaches
stderr — repro-able on a laptop in milliseconds, no cluster cycle.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from gate.agent.initiative import RunSummary, run_initiative


def _result_message(turns: int = 1, cost: float = 0.01, is_error: bool = False) -> ResultMessage:
    """Build a minimally-populated ResultMessage closing a turn. The SDK
    yields one of these at the end of every real run; tests append it so
    ``run_initiative`` reaches the final-summary path with non-None
    ``last_cost``."""
    return ResultMessage(
        subtype='success',
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=turns,
        session_id='test-session',
        total_cost_usd=cost,
        usage={'input_tokens': 0, 'output_tokens': 0},
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


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
    """Minimal stand-in for the loader's Initiative dataclass.

    ``run_initiative`` consults ``.name``, ``.is_multi_repo``, and
    ``.primary`` (which must expose ``.qualified_repo`` + ``.branch``).
    """

    name: str = 'example-initiative'
    is_multi_repo: bool = False
    repos: list[_FakeRepo] = field(default_factory=lambda: [_FakeRepo()])

    @property
    def primary(self) -> _FakeRepo:
        return self.repos[0]


def _make_query_yielding(messages: list[Any]):
    """Build a fake ``query()`` callable returning an async iterator over the
    given messages. The real SDK signature is ``query(*, prompt, options)``;
    we accept arbitrary kwargs so call-shape changes don't break the mock."""

    async def fake_query(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg

    return fake_query


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Set up a minimal cwd + initiative YAML file so ``run_initiative``
    can run end-to-end without touching the network or the filesystem
    outside the test's tmpdir."""
    repo_root = tmp_path / 'example-svc'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — content is irrelevant; loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


# ---------------------------------------------------------------------------
# The actual integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_open_marker_emits_when_tool_result_arrives_in_user_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Realistic SDK message order:

    1. AssistantMessage with a ToolUseBlock for `gh pr create ...`
    2. UserMessage with a ToolResultBlock returning the PR URL
    3. ResultMessage closing the turn

    The watcher must emit ``--- pr_open pr=513 repo=mikelear/example-svc``
    when (2) arrives. This is the scenario that was silently broken on
    main pre-fix.
    """
    messages = [
        AssistantMessage(
            content=[
                TextBlock(text='Opening the PR now.'),
                ToolUseBlock(id='t1', name='Bash', input={'command': 'gh pr create ...'}),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t1',
                    content='remote: ...\nhttps://github.com/mikelear/example-svc/pull/513\n',
                    is_error=False,
                ),
            ],
        ),
        _result_message(),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=513),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    captured = capsys.readouterr()
    assert '--- pr_open pr=513 repo=mikelear/example-svc' in captured.err, (
        f'D.5.1.4 marker must emit on UserMessage.content[ToolResultBlock]. stderr was: {captured.err!r}'
    )
    assert isinstance(summary, RunSummary)


@pytest.mark.asyncio
async def test_pr_open_marker_does_not_double_emit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If subsequent tool results (e.g. ``gh pr view``, ``gh pr list``)
    also contain the same PR URL, the watcher emits ONCE — guarded by
    ``pr_emitted is None``. The reconciler is fine with a single line
    and re-emitting would dilute log signal-to-noise."""
    second_result = UserMessage(
        content=[
            ToolResultBlock(
                tool_use_id='t2',
                content='Latest commit: ...\nURL: https://github.com/mikelear/example-svc/pull/513\n',
                is_error=False,
            ),
        ],
    )
    messages = [
        AssistantMessage(
            content=[ToolUseBlock(id='t1', name='Bash', input={'command': 'gh pr create'})],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t1',
                    content='https://github.com/mikelear/example-svc/pull/513\n',
                    is_error=False,
                ),
            ],
        ),
        AssistantMessage(
            content=[ToolUseBlock(id='t2', name='Bash', input={'command': 'gh pr view'})],
            model='claude',
        ),
        second_result,
        _result_message(turns=2),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=513),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    captured = capsys.readouterr()
    assert captured.err.count('--- pr_open pr=513') == 1, f'expected exactly one pr_open marker, got: {captured.err!r}'


@pytest.mark.asyncio
async def test_pr_open_marker_silent_when_no_pr_url_in_any_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Initiatives that don't open a PR (already-satisfied no-op, agent
    chooses not to push) leave the marker absent. The reconciler then
    falls back to the GH-side lookup (D.5.1.1) or records ``pr=None``."""
    messages = [
        AssistantMessage(
            content=[TextBlock(text='Initiative is already complete. Nothing to do.')],
            model='claude',
        ),
        _result_message(),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    captured = capsys.readouterr()
    assert '--- pr_open' not in captured.err, f'no PR was opened — marker must not appear. stderr: {captured.err!r}'


@pytest.mark.asyncio
async def test_pr_open_marker_scoped_to_initiative_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defensive: if a tool result mentions a PR URL on a DIFFERENT repo
    (e.g. the agent's prose citing prior PRs for context), the marker
    must NOT fire — ``_build_pr_url_pattern`` scopes to the initiative's
    own repo. Pairs with the helper-level test of the same property in
    ``test_initiative_crash_sticky.py``."""
    messages = [
        AssistantMessage(
            content=[ToolUseBlock(id='t1', name='Bash', input={'command': 'gh pr list other-org/other-svc'})],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t1',
                    content='https://github.com/other-org/other-svc/pull/99\n',
                    is_error=False,
                ),
            ],
        ),
        _result_message(),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch('gate.agent.initiative.query', _make_query_yielding(messages)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    captured = capsys.readouterr()
    assert '--- pr_open' not in captured.err, (
        f'PR URL was for a different repo — marker must not fire. stderr: {captured.err!r}'
    )
