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

from gate.agent.initiative import EXPECTED_PR_MISSING_EXIT_CODE, RunSummary, run_initiative


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
    feedback_payloads: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False
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


def _wait_for_terminal_all_passed_messages() -> list[Any]:
    """A `wait_for_terminal → all_passed` exchange — the CONFIRMED-GREEN signal.

    Flips ``terminal_all_passed_seen`` in the loop: an AssistantMessage invoking
    the full-terminal check (matched on the ``wait_for_terminal`` suffix), then a
    UserMessage[ToolResultBlock] whose payload carries the ``all_passed`` token.
    This is the prompt's mandated completion signal — "every required check is
    green, YOUR JOB IS COMPLETE". Post-2026-08-05 the exit-code normalisation
    (Gate 2) rescues a crash/max-turns exit to 0 ONLY when this was seen — a PR
    being open is no longer sufficient (Gate 1 fails a PR-opened-but-never-green
    run). So the "work genuinely shipped, don't retry" cases must include this.
    """
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id='wft1',
                    name='mcp__leartech-jx3-flow__wait_for_terminal',
                    input={'pr': 777},
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='wft1',
                    content='{"status": "all_passed", "checks": []}',
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

    IMPORTANT (containment fix #3 in sanitise-subprocess-identity):
    explicitly REMOVE the AgentRun identity env vars from ``os.environ``
    for the duration of every test in this file. ``clear=False`` preserves
    the pod env by design (so ``ANTHROPIC_API_KEY`` and friends are inherited),
    but that same defence let the pod's real ``AGENT_RUN_NAME`` /
    ``AGENT_RUN_NAMESPACE`` / ``LEARTECH_AGENTRUN_STATUS`` slip into every
    test — and any test hitting the ``_backstop_target_pr`` code path then
    issued a live k8s patch against the AgentRun this pytest is running
    inside (the 12:48:43 incident). Belt-and-braces on top of the
    ``gate.identity.capture_and_strip`` guard: the strip protects
    subprocesses; this scrub protects the pytest process itself.
    Mirrors how :file:`tests/test_agent_test_mode.py` already defends
    every case with ``monkeypatch.delenv('LEARTECH_RUN_ID')``.
    """
    stack.enter_context(patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False))
    for _var in ('AGENT_RUN_NAME', 'AGENT_RUN_NAMESPACE', 'LEARTECH_AGENTRUN_STATUS', 'LEARTECH_RUN_ID'):
        os.environ.pop(_var, None)
    stack.enter_context(patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()))
    stack.enter_context(patch('gate.agent.initiative.query', _make_query_yielding(messages, raise_at_end=raise_at_end)))
    stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=resolved_pr))
    stack.enter_context(patch('gate.agent.initiative._write_pr_number_hint'))


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
        *_wait_for_terminal_all_passed_messages(),
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
        f'expected exit_code=0 (downgraded from 2: confirmed-green before max_turns hit); got {summary.exit_code}'
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
        *_wait_for_terminal_all_passed_messages(),
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
        f'expected exit_code=0 (downgraded from 1: confirmed-green before SDK crash); got {summary.exit_code}'
    )
    captured = capsys.readouterr()
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
async def test_clean_success_exits_zero(
    tmp_path: Path,
) -> None:
    """Baseline: no exception, no max_turns, no cancel, PR opened cleanly.
    Exit 0 unchanged — the normaliser is a no-op on the happy path.

    ``resolved_pr`` is set to a real number so the end-of-run expected-PR
    fail-fast sees the PR on the branch and stays a no-op (the happy path
    genuinely produced a PR)."""
    messages = [
        *_pr_opened_messages(),
        *_wait_for_terminal_all_passed_messages(),
        _result_message(turns=2),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=513)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0


@pytest.mark.asyncio
async def test_clean_run_with_no_pr_fails_expected_pr_missing(
    tmp_path: Path,
) -> None:
    """The false-Succeed the fail-fast fixes: the SDK loop finished cleanly
    (would-be exit 0) but NO PR was resolved on the branch. A PR-backed dev
    agent that produces no PR must NOT report success — it forces a non-zero
    exit so the AgentRun goes Failed and K8s can retry. Mirrors the az-infra
    register step that exited 0 without pushing a PR (bot push-perms)."""
    messages = [
        *_no_pr_messages(),
        _result_message(turns=2),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=None)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code != 0, 'PR-backed step with no PR must not false-Succeed'
    assert summary.exit_code == EXPECTED_PR_MISSING_EXIT_CODE
    assert summary.pr_number is None


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
        *_wait_for_terminal_all_passed_messages(),
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
    mock_sticky.assert_called_once()
    call_kwargs = mock_sticky.call_args.kwargs
    assert call_kwargs.get('pr_number') == 777
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
        *_wait_for_terminal_all_passed_messages(),
        _result_message(turns=2, is_error=True),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=513)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=200,
        )

    assert summary.exit_code == 0, (
        f'is_error ResultMessage after confirmed-green: expected downgrade to 0; got {summary.exit_code}'
    )


@pytest.mark.asyncio
async def test_sdk_crash_after_pr_opened_without_green_keeps_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PR opened, SDK crashes, but wait_for_terminal never reported all_passed.

    Pre-2026-08-05 this downgraded to 0 ("a PR was opened"). Corrected: no green
    → nothing confirmed-shipped → exit 1 stays so K8s retries. The crash sticky
    still fires (operator visibility preserved).
    """
    messages = [
        *_pr_opened_messages(),
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

    assert summary.exit_code == 1, (
        f'PR opened but never green + SDK crash: expected exit_code=1 (no rescue); got {summary.exit_code}'
    )
    captured = capsys.readouterr()
    assert 'Unexpected SDK exception' in captured.err
    assert 'exit_code normalisation' not in captured.err, (
        f'no confirmed-green → normalisation must not fire. stderr: {captured.err!r}'
    )


@pytest.mark.asyncio
async def test_max_turns_after_pr_opened_without_green_keeps_exit_two(
    tmp_path: Path,
) -> None:
    """Max_turns hit after a PR was opened but before it ever went green.

    Nothing confirmed-shipped → exit 2 stays (real failure, retry as designed).
    """
    max_turns = 10
    messages = [
        *_pr_opened_messages(),
        _result_message(turns=max_turns),
    ]

    with ExitStack() as stack:
        _enter_common_patches(stack, messages, raise_at_end=Exception('SDK terminated at max_turns'), resolved_pr=513)
        summary = await run_initiative(
            **_build_run_kwargs(tmp_path),
            max_turns=max_turns,
        )

    assert summary.exit_code == 2, (
        f'PR opened but never green + max_turns: expected exit_code=2 (no rescue); got {summary.exit_code}'
    )
