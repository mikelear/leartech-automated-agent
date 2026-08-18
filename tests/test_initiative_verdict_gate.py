"""Verdict gate: opening a PR is NOT success — reaching green is (Mike 2026-08-05).

Pins ``run_initiative``'s Gate 1 (initiative.py). The runtime used to record
SUCCESS whenever a PR was opened (``pr_emitted is not None``), so an agent that
adopted a PR, ran the fail-fast wait command, found **zero checks** (the repo had
no webhook), posted a ``## Initiative blocked`` summary, and ended its turn
cleanly (``is_error=False`` → exit 0) was recorded as **Succeeded** — the
false-Succeed that let ``setup-mcp-design`` "pass" while nothing was built.

The corrected state machine (Mike):
  A. red check → agent pushes a fix → waits again        → NORMAL (not tested here; iteration)
  B. wait_for_terminal → all_passed                      → the ONLY success
  C. posts "blocked" / never reaches all-green           → FAILED (recycles the agent)

The single authoritative "shipped" signal is ``terminal_all_passed_seen``
(wait_for_terminal reported all_passed — the prompt's mandated completion signal),
NOT "a PR exists". These tests mock ``claude_agent_sdk.query`` with hand-built
message sequences (mirroring ``test_agent_exit_code_normalisation.py``) that
exercise our INTERNAL MCP tools — ``wait_for_terminal`` (full-terminal check) and
``wait_for_first_failure_or_all_pass`` (in-loop fail-fast primitive) — and assert
``RunSummary.exit_code`` matches the corrected contract.
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

# ─── Test doubles (mirror tests/test_agent_exit_code_normalisation.py) ──────


@dataclass
class _FakeRepo:
    repo: str = 'mikelear/leartech-artifact-api'
    branch: str = 'smoke-leartech-artifact-api'
    base: str = 'main'

    @property
    def qualified_repo(self) -> str:
        return self.repo


@dataclass
class _FakeInitiative:
    name: str = 'artifact-api-smoke-dev'
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
    async def fake_query(**_kwargs: Any) -> AsyncIterator[Any]:
        for msg in messages:
            yield msg
        if raise_at_end is not None:
            raise raise_at_end

    return fake_query


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    repo_root = tmp_path / 'artifact-api'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


def _result_message(turns: int = 2, cost: float = 0.01, is_error: bool = False) -> ResultMessage:
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


# ─── Internal-MCP mock exchanges ────────────────────────────────────


def _adopt_pr_messages() -> list[Any]:
    """The agent adopts/opens a PR. ``pr_emitted`` is driven by the patched
    branch-scoped ``_resolve_pr_number`` (``resolved_pr``), not this prose — this
    just represents the agent doing PR work."""
    return [
        AssistantMessage(
            content=[
                TextBlock(text='Adopting PR #1 via open_pr.'),
                ToolUseBlock(
                    id='op1', name='mcp__leartech-agent-local__open_pr', input={'branch': 'smoke-leartech-artifact-api'}
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[ToolResultBlock(tool_use_id='op1', content='{"pr_number": 1, "url": "…/pull/1"}', is_error=False)],
        ),
    ]


def _wait_for_terminal_all_passed_messages() -> list[Any]:
    """CONFIRMED-GREEN: the full-terminal check returns all_passed (state B)."""
    return [
        AssistantMessage(
            content=[ToolUseBlock(id='wft1', name='mcp__leartech-jx3-flow__wait_for_terminal', input={'pr': 1})],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id='wft1', content='{"status": "all_passed", "checks": []}', is_error=False)
            ],
        ),
    ]


def _fail_fast_no_checks_then_blocked_messages() -> list[Any]:
    """OUR INCIDENT (state C): the agent runs the fail-fast wait command, gets
    ZERO checks (no webhook), then posts a ``## Initiative blocked`` summary and
    ends its turn. ``wait_for_first_failure_or_all_pass`` is the in-loop primitive
    — deliberately NOT the completion signal — so ``terminal_all_passed_seen``
    stays False even though the agent used a wait command."""
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id='ff1',
                    name='mcp__leartech-jx3-flow__wait_for_first_failure_or_all_pass',
                    input={'pr': 1},
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(tool_use_id='ff1', content='{"status": "no_checks", "checks": []}', is_error=False)
            ],
        ),
        AssistantMessage(
            content=[
                TextBlock(
                    text=(
                        '## Initiative blocked\n\n'
                        '0 checks fired on either cluster — the repo has zero webhooks configured, '
                        'so Lighthouse never receives PR events. Handoff sticky posted. Ending the turn.'
                    ),
                ),
            ],
            model='claude',
        ),
    ]


def _fail_fast_some_failed_unresolved_messages() -> list[Any]:
    """State C variant: the fail-fast wait reports a first failure the agent
    couldn't resolve, and it ends without ever reaching all-green."""
    return [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id='ff2',
                    name='mcp__leartech-jx3-flow__wait_for_first_failure_or_all_pass',
                    input={'pr': 1},
                ),
            ],
            model='claude',
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id='ff2', content='{"status": "some_failed", "failed": ["az/verify"]}', is_error=False
                )
            ],
        ),
        AssistantMessage(
            content=[
                TextBlock(text='## Initiative partial\n\naz/verify is red and I have exhausted my iteration budget.')
            ],
            model='claude',
        ),
    ]


def _no_wait_at_all_messages() -> list[Any]:
    """State C variant: the agent opens a PR and ends without ever waiting on
    checks (no wait command). Still never reached green → not success."""
    return [
        AssistantMessage(content=[TextBlock(text='## Initiative complete\n\nPR opened.')], model='claude'),
    ]


def _enter_common_patches(
    stack: ExitStack,
    messages: list[Any],
    *,
    raise_at_end: BaseException | None = None,
    resolved_pr: int | None = None,
) -> None:
    stack.enter_context(patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False))
    stack.enter_context(patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()))
    stack.enter_context(patch('gate.agent.initiative.query', _make_query_yielding(messages, raise_at_end=raise_at_end)))
    stack.enter_context(patch('gate.agent.initiative._resolve_pr_number', return_value=resolved_pr))
    stack.enter_context(patch('gate.agent.initiative._write_pr_number_hint'))


# ─── The matrix ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pr_opened_and_all_passed_exits_zero(tmp_path: Path) -> None:
    """State B — the ONLY success: PR opened AND wait_for_terminal all_passed."""
    messages = [
        *_adopt_pr_messages(),
        *_wait_for_terminal_all_passed_messages(),
        _result_message(),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=1)
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)
    assert isinstance(summary, RunSummary)
    assert summary.exit_code == 0, f'confirmed-green must succeed; got {summary.exit_code}'


@pytest.mark.asyncio
async def test_pr_opened_but_blocked_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OUR INCIDENT (state C): PR opened, fail-fast finds 0 checks, agent posts
    "## Initiative blocked" and ends cleanly. Pre-fix this was Succeeded; now it
    must be FAILED so the step recycles the agent."""
    messages = [
        *_adopt_pr_messages(),
        *_fail_fast_no_checks_then_blocked_messages(),
        _result_message(is_error=False),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=1)
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)
    assert summary.exit_code == 1, (
        f'PR opened but blocked (never green) must FAIL, not false-Succeed; got {summary.exit_code}'
    )
    captured = capsys.readouterr()
    assert 'verdict gate' in captured.err, f'the verdict gate must announce the downgrade. stderr: {captured.err!r}'


@pytest.mark.asyncio
async def test_pr_opened_first_failure_unresolved_exits_one(tmp_path: Path) -> None:
    """State C: the fail-fast wait reported a failure the agent never resolved to
    green. Not all-passed → FAIL."""
    messages = [
        *_adopt_pr_messages(),
        *_fail_fast_some_failed_unresolved_messages(),
        _result_message(),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=1)
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)
    assert summary.exit_code == 1, f'some_failed + unresolved must FAIL; got {summary.exit_code}'


@pytest.mark.asyncio
async def test_pr_opened_never_waited_exits_one(tmp_path: Path) -> None:
    """State C: PR opened, agent ends WITHOUT ever confirming green (no
    wait_for_terminal all_passed). Even a "## Initiative complete" claim must not
    be trusted over the deterministic green signal → FAIL."""
    messages = [
        *_adopt_pr_messages(),
        *_no_wait_at_all_messages(),
        _result_message(),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=1)
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)
    assert summary.exit_code == 1, (
        f'PR opened but never confirmed green must FAIL regardless of the prose status; got {summary.exit_code}'
    )


@pytest.mark.asyncio
async def test_no_pr_blocked_fails_via_expected_pr_missing(tmp_path: Path) -> None:
    """A PR-backed run that opens NO PR must FAIL — but via the expected-PR-missing
    fail-fast (#203), NOT Gate 1. Gate 1's scope is PR-OPENED-but-not-green
    (``pr_emitted`` set); the no-PR-at-all case is #203's domain. Together they
    leave no false-Succeed hole: PR opened + never green → Gate 1 (exit 1);
    no PR opened at all → #203 (non-zero). This pins that the two compose — a
    blocked agent that never opened a PR does not slip through as success."""
    messages = [
        *_fail_fast_no_checks_then_blocked_messages(),
        _result_message(),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=None)
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)
    assert summary.exit_code != 0, (
        f'PR-backed step that opened no PR must fail via expected_pr_missing (#203); got {summary.exit_code}'
    )


@pytest.mark.asyncio
async def test_blocked_verdict_emits_structured_event(tmp_path: Path) -> None:
    """The failure must be observable: a structured ``initiative_verdict`` event is
    emitted, so the controller and forensics read the verdict deterministically
    rather than from prose. (The paired DB failure-reason write is gone — it wrote
    to a database the AgentRun runtime cannot reach.)"""
    messages = [
        *_adopt_pr_messages(),
        *_fail_fast_no_checks_then_blocked_messages(),
        _result_message(),
    ]
    with ExitStack() as stack:
        _enter_common_patches(stack, messages, resolved_pr=1)
        mock_obslog = stack.enter_context(patch('gate.agent.initiative.obslog'))
        summary = await run_initiative(**_build_run_kwargs(tmp_path), max_turns=200)

    assert summary.exit_code == 1
    # A structured initiative_verdict event was emitted.
    verdict_events = [c for c in mock_obslog.emit.call_args_list if 'initiative_verdict' in c.args]
    assert verdict_events, f'expected an initiative_verdict obslog event; got {mock_obslog.emit.call_args_list!r}'
    assert verdict_events[0].kwargs.get('verdict') == 'blocked_or_unfinished'
    assert verdict_events[0].kwargs.get('exit_code') == 1
