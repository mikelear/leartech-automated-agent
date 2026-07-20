"""Integration tests for the ``agent-capture-and-publish`` initiative.

Contract pinned across four surfaces:

  1. **Create-return capture yields the right number.** When the SDK
     loop observes an AssistantMessage with a ``gh pr create`` Bash
     invocation followed by a UserMessage with a matching
     ``ToolResultBlock`` whose content is a PR URL, the harness parses
     the number authoritatively (branch-scoped, not a prose scrape).

  2. **Publish writes targetPR AND attempts the maestro announce.**
     The inline capture calls :func:`gate.agent.agentrun_status.patch_pr_number`
     with the captured number AND calls :func:`gate.agent.maestro.emit_run_pr_opened`
     with the fan-out payload.

  3. **Announce failure doesn't fail the run.** When
     :func:`emit_run_pr_opened` raises internally (simulated via a
     patch that raises), the SDK loop still completes successfully.

  4. **Resume path uses the fallback.** When a retry pod resumes with
     ``resume_context.pr_number`` set, the same publish-once helper
     fires with ``source='resume'`` BEFORE the SDK loop starts — no
     tool_result capture required.

Test fixtures mirror ``tests/test_initiative_resume_on_retry.py`` and
``tests/test_agent_exit_code_normalisation.py`` so a wiring regression
in one place surfaces in all three.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from gate.agent.initiative import ResumeContext, RunSummary, run_initiative

# ─── Test doubles ────────────────────────────────────────────────────────


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
    name: str = 'example-initiative'
    is_multi_repo: bool = False
    repos: list[_FakeRepo] = field(default_factory=lambda: [_FakeRepo()])
    feedback_payloads: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False

    @property
    def primary(self) -> _FakeRepo:
        return self.repos[0]


def _make_query_yielding(messages: list[Any]) -> Callable[..., AsyncIterator[Any]]:
    async def fake_query(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg

    return fake_query


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    repo_root = tmp_path / 'example-svc'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


def _result_message(turns: int = 1, cost: float = 0.01) -> ResultMessage:
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


def _gh_pr_create_capture_sequence(
    *,
    pr_url: str = 'https://github.com/mikelear/example-svc/pull/777',
    tool_use_id: str = 't-pr-create-1',
) -> list[Any]:
    """A realistic AssistantMessage → UserMessage pair mirroring the SDK's
    protocol shape when the agent runs ``gh pr create``.

    The AssistantMessage carries the Bash ToolUseBlock with a
    ``gh pr create`` command; the following UserMessage carries the
    matching ToolResultBlock whose content includes the PR URL.
    """
    return [
        AssistantMessage(
            content=[
                TextBlock(text='Opening the PR now.'),
                ToolUseBlock(
                    id=tool_use_id,
                    name='Bash',
                    input={
                        'command': (
                            'gh pr create --repo mikelear/example-svc --base main '
                            '--head agent/example-fix --title "Fix X" --body "Do Y"'
                        )
                    },
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id=tool_use_id,
                    content=f'{pr_url}\n',
                    is_error=False,
                ),
            ],
        ),
    ]


def _enter_common_patches(
    stack: ExitStack,
    messages: list[Any],
    *,
    patch_pr_number_mock: AsyncMock | None = None,
    emit_mock: AsyncMock | None = None,
    resume_context: ResumeContext | None = None,
) -> None:
    """Patch stack shared by all tests.

    ``patch_pr_number_mock`` / ``emit_mock`` — if provided, replace the
    two publish surfaces with mocks so we can assert exact call args.

    ``resume_context`` — if provided, drives the resume path.
    """
    stack.enter_context(patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False))
    stack.enter_context(patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()))
    stack.enter_context(patch('gate.agent.initiative.query', _make_query_yielding(messages)))
    stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=None))
    stack.enter_context(patch('gate.agent.initiative._write_pr_number_hint'))

    if resume_context is not None:
        stack.enter_context(patch('gate.agent.initiative._detect_resume_context', return_value=resume_context))
        # If resume is active and branch_exists_on_remote is True, the harness
        # calls _fetch_and_checkout_existing. Mock it to succeed so the resume
        # path fully engages.
        stack.enter_context(patch('gate.agent.initiative._fetch_and_checkout_existing', return_value=True))
    if patch_pr_number_mock is not None:
        stack.enter_context(patch('gate.agent.initiative.patch_pr_number', patch_pr_number_mock))
    if emit_mock is not None:
        stack.enter_context(patch('gate.agent.initiative.emit_run_pr_opened', emit_mock))


# ─── Contract 1 + 2: capture from create-return + publish both surfaces ─


@pytest.mark.asyncio
async def test_capture_at_create_publishes_both_surfaces(tmp_path: Path) -> None:
    """The headline pin: when the agent runs ``gh pr create`` and the
    tool_result comes back with a PR URL, BOTH publish surfaces fire
    with the captured number, in one place, before end-of-run.
    """
    messages = [
        *_gh_pr_create_capture_sequence(pr_url='https://github.com/mikelear/example-svc/pull/777'),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    assert isinstance(summary, RunSummary)

    # AUTHORITATIVE surface: CR status write called with the captured number.
    # patch_pr_number is called ONCE at capture-time (create_return source).
    # The end-of-run fallback also runs, but since ``_resolve_pr_number`` is
    # patched to None, only the inline capture fires.
    patch_pr_calls = patch_pr_mock.await_args_list
    assert any(call.args == (777,) for call in patch_pr_calls), (
        f'patch_pr_number(777) must be called from the inline capture; got: {patch_pr_calls!r}'
    )

    # REACTIVE surface: maestro announce fired with the fan-out payload.
    assert emit_mock.await_count == 1
    kwargs = emit_mock.await_args.kwargs
    assert kwargs['pr_number'] == 777
    assert kwargs['repo'] == 'mikelear/example-svc'
    assert kwargs['head_branch'] == 'agent/example-fix'
    # tenant + run are best-effort — no strict shape assertion but they
    # must be passed as keyword args (per the API contract).
    assert 'tenant' in kwargs
    assert 'run' in kwargs


# ─── Contract 1 negative: bogus URL in a non-create tool_result ─────────


@pytest.mark.asyncio
async def test_pr_url_in_non_create_tool_result_is_ignored(tmp_path: Path) -> None:
    """Anti-regression pin — the deleted ``_extract_pr_from_tool_result``
    scraped arbitrary PR URLs the agent quoted in narrative or in
    ``gh pr view`` output, silently overwriting the real number with an
    unrelated PR. The new capture MUST be gated on the classifier: a
    ``gh pr view`` command whose output cites a PR URL must be ignored.
    """
    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id='t-view-1',
                    name='Bash',
                    # NOT gh pr create — the classifier must not arm.
                    input={'command': 'gh pr view 42 --repo mikelear/other-repo'},
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t-view-1',
                    content='https://github.com/mikelear/other-repo/pull/42\nsome pr info\n',
                    is_error=False,
                ),
            ],
        ),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        await run_initiative(**_build_run_kwargs(tmp_path))

    # The inline capture must NOT fire — gh pr view isn't a create.
    # patch_pr_number IS called once from end-of-run fallback with None
    # (the historical-behaviour preserving path), but never with 42.
    for call in patch_pr_mock.await_args_list:
        assert call.args != (42,), f'patch_pr_number(42) leaked from a non-create tool_result: {call!r}'
    # maestro is never emitted when no create-return was captured
    # (unless the fallback path finds a PR — which it can't since
    # _resolve_pr_number is patched to None).
    assert emit_mock.await_count == 0


# ─── Contract 1 negative: is_error result skipped ────────────────────────


@pytest.mark.asyncio
async def test_gh_pr_create_error_result_is_skipped(tmp_path: Path) -> None:
    """When ``gh pr create`` fails (is_error=True), we must not attempt
    to publish. The error path in the SDK loop lets the agent iterate
    on the failure — a publish attempt would fan out a garbage number
    (or 0) to consumers.
    """
    messages = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id='t-create-fail',
                    name='Bash',
                    input={'command': 'gh pr create --title Broken --body Nope'},
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='t-create-fail',
                    content='HTTP 422: validation failed on branch\n',
                    is_error=True,
                ),
            ],
        ),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        await run_initiative(**_build_run_kwargs(tmp_path))

    # No inline publish — the error result was skipped.
    assert emit_mock.await_count == 0
    # patch_pr_number is called ONCE from end-of-run fallback with None
    # (no PR resolved). Only that call is present.
    assert all(call.args == (None,) for call in patch_pr_mock.await_args_list)


# ─── Contract 3: announce failure doesn't fail the run ──────────────────


@pytest.mark.asyncio
async def test_maestro_announce_failure_does_not_fail_the_run(tmp_path: Path) -> None:
    """The maestro emit is best-effort — a raise inside the emit helper
    (should not happen since the helper swallows internally, but pin
    the contract at the caller layer too) MUST NOT propagate."""
    messages = [
        *_gh_pr_create_capture_sequence(pr_url='https://github.com/mikelear/example-svc/pull/513'),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()

    async def _exploding_emit(**_kwargs: Any) -> None:
        raise RuntimeError('simulated maestro bus outage')

    emit_mock = AsyncMock(side_effect=_exploding_emit)

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    # The run completed — the emit failure was contained.
    assert isinstance(summary, RunSummary)
    # patch_pr_number DID fire — the CR write is not blocked by the
    # emit failure (although the emit runs AFTER the patch, so it
    # never had a chance to interfere).
    assert any(call.args == (513,) for call in patch_pr_mock.await_args_list)
    # And the emit was attempted exactly once — the retry loop must not
    # hammer maestro on failure.
    assert emit_mock.await_count == 1


# ─── Contract 4: resume path uses the same publish-once helper ──────────


@pytest.mark.asyncio
async def test_resume_path_publishes_at_loop_entry(tmp_path: Path) -> None:
    """When resume detects an existing PR, the harness publishes
    IMMEDIATELY at loop entry (before the SDK sees any message),
    via the same ``_publish_pr_once`` helper (source='resume').

    This is the "fallback for resume pods" the initiative spec calls
    out — the prior pod may have died before emitting the announce,
    so the retry pod re-emits."""
    messages = [
        AssistantMessage(content=[TextBlock(text='Resuming.')], model='claude'),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
            resume_context=ResumeContext(is_resume=True, pr_number=42, branch_exists_on_remote=True),
        )
        # The end-of-run fallback also calls _resolve_pr_number; patch
        # it to return 42 so the fallback path is deterministic (but
        # since publish_done is already True, no second publish fires).
        stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=42))
        await run_initiative(**_build_run_kwargs(tmp_path))

    # BOTH surfaces fired ONCE at loop entry (resume source).
    # patch_pr_number is called with 42 (the resumed PR number).
    # emit_run_pr_opened is called with the resume-derived payload.
    assert any(call.args == (42,) for call in patch_pr_mock.await_args_list), (
        f'expected patch_pr_number(42) from resume path; got: {patch_pr_mock.await_args_list!r}'
    )
    # Exactly one emit — the publish-once gate must have blocked the
    # end-of-run fallback from re-emitting.
    assert emit_mock.await_count == 1
    kwargs = emit_mock.await_args.kwargs
    assert kwargs['pr_number'] == 42


# ─── End-of-run fallback path — when create-return capture didn't fire ──


@pytest.mark.asyncio
async def test_fallback_publishes_when_inline_capture_missed(tmp_path: Path) -> None:
    """When no ``gh pr create`` was observed in the SDK loop but the
    end-of-run resolver finds an open PR on the branch (e.g. the agent
    used an unclassified shell shape), the fallback path publishes via
    the same helper — same fan-out (patch + emit)."""
    messages = [
        AssistantMessage(content=[TextBlock(text='Nothing to do.')], model='claude'),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        # Override the resolver to return a number — mimicking the branch
        # having a PR the inline capture missed.
        stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=999))
        await run_initiative(**_build_run_kwargs(tmp_path))

    # Fallback publish fires: patch_pr_number(999) + emit(pr_number=999).
    assert any(call.args == (999,) for call in patch_pr_mock.await_args_list)
    assert emit_mock.await_count == 1
    assert emit_mock.await_args.kwargs['pr_number'] == 999


@pytest.mark.asyncio
async def test_inline_capture_prevents_duplicate_fallback_publish(tmp_path: Path) -> None:
    """When the inline capture fires AND the end-of-run resolver ALSO
    finds the same PR (the common case: agent opened the PR mid-run,
    the resolver rediscovers it), the fallback must skip the publish
    to honour the "Publish once" contract. patch_pr_number + emit
    fire ONCE total across the whole run."""
    messages = [
        *_gh_pr_create_capture_sequence(pr_url='https://github.com/mikelear/example-svc/pull/123'),
        _result_message(turns=1),
    ]

    patch_pr_mock = AsyncMock()
    emit_mock = AsyncMock()

    with ExitStack() as stack:
        _enter_common_patches(
            stack,
            messages,
            patch_pr_number_mock=patch_pr_mock,
            emit_mock=emit_mock,
        )
        # End-of-run resolver would find the same PR — but publish-once
        # gates the fallback.
        stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=123))
        await run_initiative(**_build_run_kwargs(tmp_path))

    # patch_pr_number(123) called EXACTLY once — the publish-once gate
    # blocked the fallback from re-patching. The gate is enforced at
    # the ``_publish_pr_once`` level so both surfaces are consistent.
    patch_calls_with_123 = [c for c in patch_pr_mock.await_args_list if c.args == (123,)]
    assert len(patch_calls_with_123) == 1, (
        f'expected exactly one patch_pr_number(123) call; got: {patch_pr_mock.await_args_list!r}'
    )
    # emit fired exactly once with pr=123.
    assert emit_mock.await_count == 1
    assert emit_mock.await_args.kwargs['pr_number'] == 123
