"""Unit + integration tests for resume-on-retry (reliability part 3).

When the agent Job pod dies mid-run and Kubernetes' backoffLimit spawns a
retry pod, the retry must RESUME from the pushed git branch + open PR
instead of restarting the initiative from scratch. The empirical
motivator: run B1 (2026-07-13) whose first pod died and whose retry
redid ~all the work before the duplicate-PR path was blocked by
GitHub's "PR already exists on branch" check.

This file pins the contract at three layers:

1. ``_remote_branch_exists`` — the git-ls-remote wrapper. Returns
   True/False deterministically, swallows every error mode into
   ``False`` (safe-fallback to fresh-start).

2. ``_detect_resume_context`` — combines the PR-lookup signal
   (``_resolve_pr_number``) with the branch-lookup signal
   (``_remote_branch_exists``) into a ``ResumeContext``. Either alone
   flips ``is_resume`` True. Both being False keeps the run on the
   fresh-start path.

3. ``_fetch_and_checkout_existing`` — the git fetch+checkout wrapper.
   Returns True on success; False on any subprocess failure (caller
   falls back to fresh-start on False).

4. ``_build_resume_preamble`` — the RESUME MODE prompt block that gets
   prepended to the user prompt when resume is active.

5. Integration through ``run_initiative`` — the wiring: with the
   detection helpers mocked to report a resume, the SDK loop is
   invoked with a user prompt containing the RESUME preamble AND the
   ``git fetch`` + ``git checkout`` calls are issued.

Memory ``feedback_async_tests_need_event_not_sleep`` — the SDK-loop
integration test uses ``AsyncMock`` + hand-fed message sequences, no
sleep-based races.
"""

from __future__ import annotations

import os
import subprocess
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
)

from gate.agent.initiative import (
    ResumeContext,
    RunSummary,
    _build_resume_preamble,
    _detect_resume_context,
    _fetch_and_checkout_existing,
    _remote_branch_exists,
    run_initiative,
)

# ─── _remote_branch_exists ─────────────────────────────────────────────


def test_remote_branch_exists_returns_true_on_git_success_with_output(tmp_path: Path) -> None:
    """``git ls-remote --exit-code`` exits 0 and prints the ref line when the
    branch exists. Helper must return True in that case."""
    stdout = 'deadbeef1234\trefs/heads/agent/foo\n'
    with (
        patch.dict(os.environ, {'GH_TOKEN': 'ghs_test_token'}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr='')
        assert _remote_branch_exists(qualified_repo='mikelear/example', branch='agent/foo') is True

    # Sanity: the URL embeds the token (matches _clone_repo shape).
    args = mock_run.call_args[0][0]
    assert args[0] == 'git'
    assert 'ls-remote' in args
    assert '--exit-code' in args
    url = next(a for a in args if a.startswith('https://'))
    assert 'x-access-token:ghs_test_token@github.com/mikelear/example.git' in url


def test_remote_branch_exists_returns_false_on_exit_code_2(tmp_path: Path) -> None:
    """``git ls-remote --exit-code`` exits 2 when no matching ref is
    found — the branch does not exist on the remote. Helper returns False."""
    with (
        patch.dict(os.environ, {'GH_TOKEN': 'ghs_test_token'}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=2, stdout='', stderr='')
        assert _remote_branch_exists(qualified_repo='mikelear/example', branch='agent/nope') is False


def test_remote_branch_exists_returns_false_when_gh_token_missing() -> None:
    """Without GH_TOKEN the wrapper cannot authenticate. It must return
    False and NOT invoke subprocess (private repos would 401 anyway)."""
    with (
        patch.dict(os.environ, {}, clear=True),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        assert _remote_branch_exists(qualified_repo='mikelear/example', branch='agent/foo') is False
    mock_run.assert_not_called()


def test_remote_branch_exists_returns_false_on_subprocess_timeout() -> None:
    """Timeouts are swallowed — the caller treats False as "unknown →
    fall back to fresh-start". Broken network must not crash the
    initiative loop."""
    with (
        patch.dict(os.environ, {'GH_TOKEN': 'ghs_test_token'}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='git', timeout=15)
        assert _remote_branch_exists(qualified_repo='mikelear/example', branch='agent/foo') is False


def test_remote_branch_exists_returns_false_on_empty_stdout_even_with_exit_0() -> None:
    """Defensive edge: some git plumbing outputs empty stdout with exit
    0 (e.g. some remote configurations). The helper treats that as "no
    ref found" to avoid false-positive resume detection."""
    with (
        patch.dict(os.environ, {'GH_TOKEN': 'ghs_test_token'}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout='   \n', stderr='')
        assert _remote_branch_exists(qualified_repo='mikelear/example', branch='agent/foo') is False


# ─── _detect_resume_context ─────────────────────────────────────────────


def test_detect_resume_context_true_when_pr_and_branch_exist() -> None:
    """Common retry case: prior pod pushed the branch AND opened the PR.
    Both signals fire; resume is active; pr_number surfaces the number."""
    with (
        patch('gate.agent.initiative._resolve_pr_number', return_value=42),
        patch('gate.agent.initiative._remote_branch_exists', return_value=True),
    ):
        ctx = _detect_resume_context(qualified_repo='mikelear/example', branch='agent/foo')
    assert ctx == ResumeContext(is_resume=True, pr_number=42, branch_exists_on_remote=True)


def test_detect_resume_context_true_when_only_branch_exists() -> None:
    """Edge case: prior pod pushed the branch but died before running
    ``gh pr create``. Branch signal alone still flips resume True; PR
    number is None (agent will open one itself)."""
    with (
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._remote_branch_exists', return_value=True),
    ):
        ctx = _detect_resume_context(qualified_repo='mikelear/example', branch='agent/foo')
    assert ctx.is_resume is True
    assert ctx.pr_number is None
    assert ctx.branch_exists_on_remote is True


def test_detect_resume_context_true_when_only_pr_exists() -> None:
    """Defensive edge: the PR API returns a number but the branch
    lookup reports absent (should not happen in practice but the helper
    must return the union correctly). Resume is active; the caller's
    wire-up decides not to fetch since ``branch_exists_on_remote`` is
    False — this is the safety-fallback branch inside
    ``run_initiative``."""
    with (
        patch('gate.agent.initiative._resolve_pr_number', return_value=42),
        patch('gate.agent.initiative._remote_branch_exists', return_value=False),
    ):
        ctx = _detect_resume_context(qualified_repo='mikelear/example', branch='agent/foo')
    assert ctx.is_resume is True
    assert ctx.pr_number == 42
    assert ctx.branch_exists_on_remote is False


def test_detect_resume_context_false_when_neither_signal_fires() -> None:
    """Fresh-run case: no branch, no PR → resume False → caller stays
    on the current fresh-start path. This is the "unchanged fresh runs"
    guarantee the initiative goal calls out."""
    with (
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._remote_branch_exists', return_value=False),
    ):
        ctx = _detect_resume_context(qualified_repo='mikelear/example', branch='agent/foo')
    assert ctx == ResumeContext(is_resume=False, pr_number=None, branch_exists_on_remote=False)


# ─── _fetch_and_checkout_existing ───────────────────────────────────────


def test_fetch_and_checkout_returns_true_when_both_commands_succeed(tmp_path: Path) -> None:
    """Success path: ``git fetch`` returns 0 AND ``git checkout -B`` returns 0."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
            subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
        ]
        assert _fetch_and_checkout_existing(cwd=tmp_path, branch='agent/foo') is True

    # First call: git fetch origin <branch>
    first = mock_run.call_args_list[0][0][0]
    assert first[0:3] == ['git', 'fetch', 'origin']
    assert 'agent/foo' in first
    # Second call: git checkout -B <branch> origin/<branch>
    second = mock_run.call_args_list[1][0][0]
    assert second[0:3] == ['git', 'checkout', '-B']
    assert 'agent/foo' in second
    assert 'origin/agent/foo' in second


def test_fetch_and_checkout_returns_false_when_fetch_fails(tmp_path: Path) -> None:
    """Non-zero fetch → False; checkout must NOT be attempted (nothing to check out)."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=128, stdout='', stderr='fatal: no such remote'
        )
        assert _fetch_and_checkout_existing(cwd=tmp_path, branch='agent/foo') is False
    # Exactly one call — the fetch. Checkout is skipped because there's
    # nothing to check out from.
    assert mock_run.call_count == 1


def test_fetch_and_checkout_returns_false_when_checkout_fails(tmp_path: Path) -> None:
    """Fetch OK but checkout non-zero → False. Caller falls back to fresh-start."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr=''),
            subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='pathspec did not match'),
        ]
        assert _fetch_and_checkout_existing(cwd=tmp_path, branch='agent/foo') is False


def test_fetch_and_checkout_returns_false_on_subprocess_timeout(tmp_path: Path) -> None:
    """Timeout on fetch → False, matches the "safety fallback" contract."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='git', timeout=60)
        assert _fetch_and_checkout_existing(cwd=tmp_path, branch='agent/foo') is False


# ─── _build_resume_preamble ─────────────────────────────────────────────


def test_resume_preamble_mentions_pr_when_number_known() -> None:
    """When the PR number is known, the preamble cites it explicitly
    and tells the LLM not to open a duplicate."""
    preamble = _build_resume_preamble(branch='agent/foo', base='main', pr_number=42)
    assert 'RESUME MODE' in preamble
    assert 'agent/foo' in preamble
    assert '#42' in preamble
    # The critical injunction — must be phrased strong enough that the
    # LLM won't try `gh pr create` on the resumed branch.
    assert 'duplicate' in preamble.lower() or 'reuse this one' in preamble.lower()


def test_resume_preamble_notes_no_pr_when_number_unknown() -> None:
    """Branch pushed but no PR yet → preamble states this AND tells the
    agent it may open the PR itself when work is ready — without first
    redoing the pushed commits."""
    preamble = _build_resume_preamble(branch='agent/foo', base='main', pr_number=None)
    assert 'RESUME MODE' in preamble
    assert 'agent/foo' in preamble
    assert 'No open PR' in preamble
    # Must NOT contain a specific PR number when we don't know one.
    assert '#' not in preamble.replace('#### ', '').replace('##', '')


def test_resume_preamble_tells_agent_not_to_recreate_branch() -> None:
    """The whole point of resume mode is to stop the LLM from restarting
    from scratch. The preamble must explicitly instruct against creating
    a fresh branch."""
    preamble = _build_resume_preamble(branch='agent/foo', base='main', pr_number=None)
    lowered = preamble.lower()
    # We check for the semantic instruction, not a specific phrasing.
    assert 'do not re-create' in lowered or "don't re-create" in lowered or 'do not' in lowered
    assert 'main' in preamble  # cites the base branch as "the thing NOT to reset to"


# ─── Integration: run_initiative wire-up ────────────────────────────────


@dataclass
class _FakeRepo:
    """Minimal stand-in matching the loader's ``RepoTarget`` shape."""

    repo: str = 'mikelear/example-svc'
    branch: str = 'agent/resume-test'
    base: str = 'main'

    @property
    def qualified_repo(self) -> str:
        return self.repo


@dataclass
class _FakeInitiative:
    """Minimal stand-in matching the loader's ``Initiative`` shape.

    Mirrors the fixtures in ``test_initiative_pr_open_emit.py`` and
    ``test_agent_started_executing_at.py`` so a wiring regression in
    one place surfaces in all three.
    """

    name: str = 'resume-test'
    is_multi_repo: bool = False
    repos: list[_FakeRepo] = field(default_factory=lambda: [_FakeRepo()])
    feedback_payloads: list[dict[str, Any]] = field(default_factory=list)
    # Hold-as-init-option — the agent renders `/hold` posting only when true.
    hold: bool = False

    @property
    def primary(self) -> _FakeRepo:
        return self.repos[0]


def _make_query_capturing(messages: list[Any], captured_prompts: list[str]):
    """Build a fake ``query()`` that records the prompt it was called with.

    ``run_initiative`` invokes ``query(prompt=..., options=...)``. We
    capture the prompt string into ``captured_prompts`` so tests can
    assert on its contents (specifically: the RESUME MODE preamble
    presence)."""

    async def fake_query(**kwargs: Any) -> AsyncIterator[Any]:
        prompt = kwargs.get('prompt', '')
        captured_prompts.append(prompt)
        for msg in messages:
            yield msg

    return fake_query


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


def _build_run_kwargs(tmp_path: Path) -> dict[str, Any]:
    """Set up a stub cwd + initiative YAML for the integration test."""
    repo_root = tmp_path / 'example-svc'
    repo_root.mkdir()
    initiative_path = tmp_path / 'init.yaml'
    initiative_path.write_text('# stub — loader is patched\n')
    return {'initiative_path': initiative_path, 'repo_root': repo_root}


@pytest.mark.asyncio
async def test_run_initiative_resumes_when_branch_and_pr_already_exist(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline integration test — pin the whole resume flow.

    Setup: mock detection to return ``is_resume=True, pr_number=42,
    branch_exists_on_remote=True``; mock the fetch+checkout to succeed.

    Expected:
      * ``_fetch_and_checkout_existing`` is called with the initiative's
        branch (proves the harness picked resume mode).
      * The prompt sent to ``query()`` contains "RESUME MODE" AND the
        PR number so the LLM knows not to open a duplicate.
      * The ``--- pr_open pr=42`` marker emits from the resume-seed
        path (proves the exit-code normalisation would fire if this
        pod re-crashed).
    """
    captured_prompts: list[str] = []
    messages = [
        AssistantMessage(content=[TextBlock(text='Understood — resuming from prior work.')], model='claude'),
        _result_message(turns=1),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch(
            'gate.agent.initiative._detect_resume_context',
            return_value=ResumeContext(is_resume=True, pr_number=42, branch_exists_on_remote=True),
        ),
        patch(
            'gate.agent.initiative._fetch_and_checkout_existing',
            return_value=True,
        ) as mock_fetch,
        patch('gate.agent.initiative.query', _make_query_capturing(messages, captured_prompts)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=42),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        summary = await run_initiative(**_build_run_kwargs(tmp_path))

    assert isinstance(summary, RunSummary)
    # The fetch/checkout wrapper must have been invoked with the
    # initiative's target branch.
    mock_fetch.assert_called_once()
    assert mock_fetch.call_args.kwargs['branch'] == 'agent/resume-test'

    # The prompt the SDK received must contain the RESUME preamble.
    assert len(captured_prompts) == 1, 'query should be invoked exactly once'
    assert 'RESUME MODE' in captured_prompts[0]
    assert '#42' in captured_prompts[0]
    # And the standard base prompt must still be present — the preamble
    # is prepended, not a replacement.
    assert 'Run this initiative end-to-end' in captured_prompts[0]

    # Resume-seed emits the marker BEFORE the SDK loop runs so the
    # reconciler's log-parse path can see it even on a same-pod re-crash.
    err = capsys.readouterr().err
    assert '--- pr_open pr=42' in err, f'resume-seed did not emit marker; stderr was: {err!r}'


@pytest.mark.asyncio
async def test_run_initiative_fresh_start_when_no_resume_signals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fresh-run guarantee — no branch, no PR → no RESUME preamble, no
    fetch/checkout call, prompt is identical to the pre-fix baseline."""
    captured_prompts: list[str] = []
    messages = [
        AssistantMessage(content=[TextBlock(text='Starting fresh.')], model='claude'),
        _result_message(turns=1),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch(
            'gate.agent.initiative._detect_resume_context',
            return_value=ResumeContext(is_resume=False, pr_number=None, branch_exists_on_remote=False),
        ),
        patch(
            'gate.agent.initiative._fetch_and_checkout_existing',
            return_value=True,
        ) as mock_fetch,
        patch('gate.agent.initiative.query', _make_query_capturing(messages, captured_prompts)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=None),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    # Fresh runs must NOT trigger fetch/checkout.
    mock_fetch.assert_not_called()

    # Prompt must be free of the RESUME preamble.
    assert len(captured_prompts) == 1
    assert 'RESUME MODE' not in captured_prompts[0]
    # And the standard base prompt still fires.
    assert 'Run this initiative end-to-end' in captured_prompts[0]

    # No resume-seed marker.
    err = capsys.readouterr().err
    assert '--- pr_open' not in err, f'no PR emitted expected; stderr was: {err!r}'


@pytest.mark.asyncio
async def test_run_initiative_falls_back_to_fresh_when_fetch_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Detection says resume but the fetch fails (network blip, git not
    installed, branch deleted between detection and fetch) → harness
    falls back to the fresh-start path — no RESUME preamble, no
    resume-seed marker. This is the "safety fallback" the goal spec
    calls out: guard on detection, don't break existing runs."""
    captured_prompts: list[str] = []
    messages = [
        AssistantMessage(content=[TextBlock(text='Fallback path.')], model='claude'),
        _result_message(turns=1),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch(
            'gate.agent.initiative._detect_resume_context',
            return_value=ResumeContext(is_resume=True, pr_number=42, branch_exists_on_remote=True),
        ),
        patch(
            'gate.agent.initiative._fetch_and_checkout_existing',
            return_value=False,  # <-- fetch fails
        ) as mock_fetch,
        patch('gate.agent.initiative.query', _make_query_capturing(messages, captured_prompts)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=42),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    # Fetch was attempted (detection said resume) but returned False.
    mock_fetch.assert_called_once()

    # The RESUME preamble must NOT be present — resume_active is False
    # when the fetch failed.
    assert len(captured_prompts) == 1
    assert 'RESUME MODE' not in captured_prompts[0]

    # And the resume-seed marker must NOT emit either.
    err = capsys.readouterr().err
    assert '--- pr_open pr=42 repo=mikelear/example-svc (from resume detection)' not in err


@pytest.mark.asyncio
async def test_run_initiative_skips_fetch_when_only_pr_signal_fires(
    tmp_path: Path,
) -> None:
    """Defensive path: detector reports resume=True but
    ``branch_exists_on_remote=False`` (should not happen but the guard
    is cheap). Harness must NOT attempt the fetch (nothing to fetch)
    and falls back to fresh-start behaviour."""
    captured_prompts: list[str] = []
    messages = [
        AssistantMessage(content=[TextBlock(text='Fresh.')], model='claude'),
        _result_message(turns=1),
    ]

    with (
        patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test'}, clear=False),
        patch('gate.agent.initiative.load_initiative', return_value=_FakeInitiative()),
        patch(
            'gate.agent.initiative._detect_resume_context',
            return_value=ResumeContext(is_resume=True, pr_number=42, branch_exists_on_remote=False),
        ),
        patch(
            'gate.agent.initiative._fetch_and_checkout_existing',
            return_value=True,
        ) as mock_fetch,
        patch('gate.agent.initiative.query', _make_query_capturing(messages, captured_prompts)),
        patch('gate.agent.initiative._resolve_pr_number', return_value=42),
        patch('gate.agent.initiative._write_pr_number_hint'),
    ):
        await run_initiative(**_build_run_kwargs(tmp_path))

    mock_fetch.assert_not_called()
    assert 'RESUME MODE' not in captured_prompts[0]
