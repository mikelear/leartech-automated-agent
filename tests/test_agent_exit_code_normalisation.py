"""Integration tests for the post-loop exit-code normalisation in ``run_initiative``.

Initiative ``agent-fix-exit-code-after-pr-opened``. Pins the contract that when the
substantive PR-opening work completed mid-run, the process exits 0 even if the SDK
later raised an exception, hit the ``max_turns`` ceiling, or returned a
``ResultMessage`` with ``is_error=True``. Without this normalisation, K8s sees a
non-zero exit, retries the Job, hits the same SDK regression on every retry, and
eventually trips ``BackoffLimitExceeded`` — bogus failure marker for substantive
work that already shipped (canonical case: run ``59aefbd8f2d8``, PR #111 merged
cleanly while ``agent_run.status=failed``).

The cancel path is the key carve-out. Operator-intent-to-terminate must surface to
the Job condition layer even when a PR is open, so ``exit_via_cancel=True`` blocks
the downgrade.

Tests mock ``claude_agent_sdk.query`` with hand-built message sequences (mirroring
``test_initiative_pr_open_emit.py``) and assert the ``RunSummary.exit_code`` matches
the contract.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack
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
    # v6p0.5 step 2 — the loader's Initiative carries this field; the agent's
    # prompt-construction path reads it to render the previous-attempt
    # feedback block. The fakes default to an empty list (fresh run, no
    # respawn) so the prompt code path takes its no-op branch.
    feedback_payloads: list[dict[str, Any]] = field(default_factory=list)
    # Hold-as-init-option — the loader's Initiative now carries an opt-in
    # `hold: bool` field (default False); the agent's prompt-construction
    # path reads it to decide whether to render the `/hold` posting
    # instruction. Fakes default to False so the compose call matches the
    # historical (no-hold) prompt shape.
    hold: bool = False
    # Test-mode directive — the loader's Initiative now carries an opt-in
    # ``test_mode: dict | None`` field (default None); the agent's run path
    # reads it to decide whether to short-circuit the SDK loop for
    # orchestration testing. Fakes default to None so these tests exercise
    # the real SDK-loop path unchanged.
    test_mode: dict[str, object] | None = None

    @property
    def primary(self) -> _FakeRepo:
        return self.repos[0]


def _make_query_yielding(
    messages: list[Any],
    raise_at_end: BaseException | None = None,
) -> Callable[..., AsyncIterator[Any]]:
    """Build a fake ``query()`` callable returning an async iterator.

    If ``raise_at_end`` is set, the iterator yields every message and then raises
    that exception on the next ``__anext__`` — simulating the SDK's behaviour when
    it terminates abnormally (cap-hit, transport error, etc.).
    """

    async def fake_query(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg
        if raise_at_end is not None:
            raise raise_at_end

    return fake_query


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Set up a minimal cwd + initiative YAML so ``run_initiative`` doesn't
    touch the network or the filesystem outside the test's tmpdir."""
    repo_root = tmp_path / 'example-svc'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — content is irrelevant; loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


def _result_message(turns: int = 1, cost: float = 0.01, is_error: bool = False) -> ResultMessage:
    """Build a minimally-populated ResultMessage closing a turn."""
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


def _pr_opened_messages() -> list[Any]:
    """A realistic message sequence that opens a PR mid-run.

    The watcher fires on the ``UserMessage[ToolResultBlock]`` carrying the PR URL,
    setting the in-process ``pr_emitted`` flag the normaliser reads.
    """
    return [
        AssistantMessage(
            content=[
                TextBlock(text='Opening the PR.'),
                ToolUseBlock(id='t1', name='Bash', input={'command': 'gh pr create ...'}),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t1',
                    content='remote: ...\nhttps://github.com/mikelear/example-svc/pull/777\n',
                    is_error=False,
                ),
            ],
        ),
    ]


def _no_pr_messages() -> list[Any]:
    """A message sequence with NO PR URL — the watcher never fires."""
    return [
        AssistantMessage(
            content=[TextBlock(text='Looking at the code.')],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t1',
                    content='nothing interesting here\n',
                    is_error=False,
                ),
            ],
        ),
    ]


def _enter_common_patches(
    stack: ExitStack,
    messages: list[Any],
    *,
    raise_at_end: BaseException | None = None,
    resolved_pr: int | None = None,
) -> None:
    """Push the per-test patch stack so ``run_initiative`` runs hermetically.

    ``_resolve_pr_number`` is patched to ``resolved_pr`` (default None). The
    exit-code normaliser now derives "was a PR opened on this branch?" from this
    authoritative, branch-scoped lookup for non-resume runs — the loose
    tool-result prose scrape was removed (it mis-captured cited PRs / the
    targetPR wrong-PR bug). So the "PR opened → downgrade" cases set
    ``resolved_pr`` to a real number; the "no PR" cases leave it None.
    """
    stack.enter_context(patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False))
    stack.enter_context(patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()))
    stack.enter_context(patch('gate.agent.initiative.query', _make_query_yielding(messages, raise_at_end=raise_at_end)))
    stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=resolved_pr))
    stack.enter_context(patch('gate.agent.initiative._write_pr_number_hint'))


# ─── Headline cases from the initiative goal ─────────────────────────


@pytest.mark.asyncio
async def test_max_turns_hit_after_pr_opened_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The canonical bug: agent opens a PR, the SDK then trips its max_turns
    ceiling, the exception handler sets ``exit_code=2``, but the substantive
    work shipped so the process should exit 0 — no K8s retry, no
    BackoffLimitExceeded.
    """
    max_turns = 10
    messages = [
        *_pr_opened_messages(),
        # last_turn_count must equal max_turns when the exception fires so the
        # cap-hit branch (not the unexpected-error branch) is exercised.
        _result_message(turns=max_turns),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, raise_at_end=Exception('SDK terminated at max_turns'), resolved_pr=513)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=max_turns,
        )

    assert isinstance(summary, RunSummary)
    assert summary.exit_code == 0, (
        f'expected exit_code=0 (downgraded from 2: PR opened before max_turns hit); got {summary.exit_code}'
    )
    captured = capsys.readouterr()
    assert 'exit_code normalisation' in captured.err, (
        f'normaliser must emit a click message so operators see the downgrade. stderr: {captured.err!r}'
    )


@pytest.mark.asyncio
async def test_sdk_exception_after_pr_opened_exits_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mid-loop SDK crash (transport error, Anthropic 5xx, etc.) after PR opened.

    The exception handler sets ``exit_code=1``; substantive work shipped; downgrade
    to 0. The warn-level log and crash sticky still fire — operators retain
    visibility into the crash path; only the process exit code changes.
    """
    messages = [
        *_pr_opened_messages(),
        # last_turn_count well below max_turns → unexpected-error branch (not cap-hit).
        _result_message(turns=3),
    ]

    with ExitStack() as stack:
        _enter_common_patches(
            stack, messages, raise_at_end=RuntimeError('simulated SDK transport error'), resolved_pr=513
        )
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0, (
        f'expected exit_code=0 (downgraded from 1: PR opened before SDK crash); got {summary.exit_code}'
    )
    captured = capsys.readouterr()
    # The warn-level log must still be visible — the crash isn't hidden.
    assert 'Unexpected SDK exception' in captured.err, (
        f'crash warn log must still emit even when exit_code is downgraded. stderr: {captured.err!r}'
    )
    assert 'exit_code normalisation' in captured.err


@pytest.mark.asyncio
async def test_sdk_exception_before_pr_opened_keeps_exit_one(
    tmp_path: Path,
) -> None:
    """When the SDK crashes BEFORE a PR was opened, no substantive work shipped.

    Re-firing isn't wasteful (nothing to detect on re-entry), so exit_code=1
    surfaces correctly and K8s retries as designed.
    """
    messages = [
        *_no_pr_messages(),
        _result_message(turns=3),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, raise_at_end=RuntimeError('simulated SDK transport error'))
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 1, f'expected exit_code=1 (no PR was opened — no downgrade); got {summary.exit_code}'


@pytest.mark.asyncio
async def test_max_turns_hit_before_pr_opened_keeps_exit_two(
    tmp_path: Path,
) -> None:
    """Max_turns hit BEFORE the agent opened a PR — no substantive work shipped.

    Exit 2 must surface so the Job retry can make a fresh attempt (which may
    succeed: cap-hit isn't deterministic, the next run may have a leaner prompt
    or a different trajectory).
    """
    max_turns = 10
    messages = [
        *_no_pr_messages(),
        _result_message(turns=max_turns),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, raise_at_end=Exception('SDK terminated at max_turns'))
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=max_turns,
        )

    assert summary.exit_code == 2, (
        f'expected exit_code=2 (no PR — max_turns hit is a real failure); got {summary.exit_code}'
    )


@pytest.mark.asyncio
async def test_cancel_after_pr_opened_keeps_exit_two(
    tmp_path: Path,
) -> None:
    """Operator-cancel intent is preserved across the normaliser.

    Even when a PR was opened mid-run, the operator deliberately triggered
    shutdown. The Job condition layer needs that signal so dashboards reflect
    "cancelled" rather than masking it as success.
    """
    messages = [
        *_pr_opened_messages(),
        _result_message(turns=2),
    ]

    async def fake_drain(_run_id: str | None, sink: Any) -> int:
        # Mutate the loop_state via the sink the way a real cancel command would.
        # The next ``cancel_requested`` check inside ``_drain_then_check_cancel``
        # observes the flag and the loop breaks with ``exit_via_cancel=True``.
        sink.request_cancel('cancel_after_pr_opened: simulated')
        return 1

    with ExitStack() as stack:
        _enter_common_patches(stack, messages)
        stack.enter_context(patch('gate.agent.initiative.drain_commands', fake_drain))
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 2, (
        f'expected exit_code=2 (operator cancel intent preserved even with PR open); got {summary.exit_code}'
    )


@pytest.mark.asyncio
async def test_cancel_before_pr_opened_keeps_exit_two(
    tmp_path: Path,
) -> None:
    """Operator cancel before any PR was opened. Exit 2 stays — same rationale
    as ``test_cancel_after_pr_opened_keeps_exit_two`` (operator intent > PR
    presence)."""
    messages = [
        *_no_pr_messages(),
        _result_message(turns=2),
    ]

    async def fake_drain(_run_id: str | None, sink: Any) -> int:
        sink.request_cancel('cancel_before_pr_opened: simulated')
        return 1

    with ExitStack() as stack:
        _enter_common_patches(stack, messages)
        stack.enter_context(patch('gate.agent.initiative.drain_commands', fake_drain))
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 2


@pytest.mark.asyncio
async def test_clean_success_exits_zero(
    tmp_path: Path,
) -> None:
    """Baseline: no exception, no max_turns, no cancel, PR opened cleanly.
    Exit 0 unchanged — the normaliser is a no-op on the happy path."""
    messages = [
        *_pr_opened_messages(),
        _result_message(turns=2),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0


@pytest.mark.asyncio
async def test_crash_sticky_still_emitted_in_exception_branch(
    tmp_path: Path,
) -> None:
    """Defence-in-depth: the exit-code downgrade MUST NOT silence the crash
    sticky. Operators rely on the sticky landing on the PR to see "agent
    crashed at turn N — substantive work likely already pushed; re-fire is
    idempotent". Silencing it would create the opposite failure mode (PR
    looks clean but agent crashed).

    The normaliser only flips ``exit_code``; it doesn't touch
    ``crash_sticky_body`` or the subsequent ``_post_crash_sticky`` call.
    """
    messages = [
        *_pr_opened_messages(),
        _result_message(turns=3),
    ]

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            raise_at_end=RuntimeError('simulated SDK transport error'),
            resolved_pr=777,
        )
        mock_sticky = stack.enter_context(patch('gate.agent.initiative._post_crash_sticky'))
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0, 'expected downgrade from 1 → 0 (PR was opened)'
    # The crash sticky must still have been posted — the downgrade is
    # purely about the process exit code, not operator visibility.
    mock_sticky.assert_called_once()
    call_kwargs = mock_sticky.call_args.kwargs
    assert call_kwargs.get('pr_number') == 777
    # The sticky body must mention the SDK exception so operators understand
    # what happened despite exit_code=0.
    body = call_kwargs.get('body', '')
    assert 'simulated SDK transport error' in body or 'SDK crashed' in body, (
        f'crash sticky body must cite the exception. got: {body!r}'
    )


@pytest.mark.asyncio
async def test_is_error_result_message_after_pr_opened_exits_zero(
    tmp_path: Path,
) -> None:
    """Defence-in-depth: when a ``ResultMessage`` arrives with ``is_error=True``
    after a PR was opened, the loop sets ``exit_code=1`` but doesn't raise.

    The post-loop normaliser still applies — substantive work shipped, no
    cancel intent, so K8s shouldn't retry. This path isn't enumerated in the
    initiative's edge-case list but follows from the same principle and is
    covered by the same code path.
    """
    messages = [
        *_pr_opened_messages(),
        _result_message(turns=2, is_error=True),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=513)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0, (
        f'is_error ResultMessage after PR opened: expected downgrade to 0; got {summary.exit_code}'
    )
