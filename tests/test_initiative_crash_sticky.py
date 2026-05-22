"""Unit tests for the crash-sticky helpers in gate.agent.initiative.

Covers the gap surfaced by run 005527f67608: the SDK raised at turn 146/1000
(Anthropic rate limit) and the agent never reached its step-11 sticky, so the
PR was left with no closing comment. The harness now posts a crash sticky in
both exception branches (cap-hit and unexpected).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from gate.agent.initiative import _build_crash_sticky_body, _post_crash_sticky


def test_build_crash_sticky_body_includes_marker_and_metadata() -> None:
    body = _build_crash_sticky_body(
        reason='SDK crashed unexpectedly: `Command failed with exit code 1`',
        turn_count=146,
        max_turns=1000,
        cost=7.8862,
        hint='Re-fire is idempotent.',
    )
    assert '<!-- leartech-agent-run -->' in body, 'sticky must include the tooling marker'
    assert '146/1000' in body
    assert '$7.8862' in body
    assert 'Re-fire is idempotent.' in body
    assert 'Command failed with exit code 1' in body


def test_build_crash_sticky_body_handles_no_cost() -> None:
    body = _build_crash_sticky_body(
        reason='hit the `max_turns` ceiling (60).',
        turn_count=60,
        max_turns=60,
        cost=None,
        hint='Re-run with --max-turns 250.',
    )
    assert '**Cost so far**: unknown' in body
    assert '60/60' in body


def test_post_crash_sticky_skips_when_no_pr() -> None:
    """No PR resolved → log and skip; never call gh."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        _post_crash_sticky(qualified_repo='owner/repo', pr_number=None, body='body')
        mock_run.assert_not_called()


def test_post_crash_sticky_calls_gh_with_pr_and_repo() -> None:
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')
        _post_crash_sticky(qualified_repo='owner/repo', pr_number=42, body='hello')
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0:4] == ['gh', 'pr', 'comment', '42']
        assert '-R' in args
        assert 'owner/repo' in args
        assert '--body' in args
        assert 'hello' in args


def test_post_crash_sticky_swallows_gh_failure() -> None:
    """gh exit-nonzero must not raise — we're already in an error path."""
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='auth required')
        # Must not raise.
        _post_crash_sticky(qualified_repo='owner/repo', pr_number=42, body='hello')


def test_post_crash_sticky_swallows_subprocess_timeout() -> None:
    with patch('gate.agent.initiative.subprocess.run') as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='gh', timeout=15)
        # Must not raise.
        _post_crash_sticky(qualified_repo='owner/repo', pr_number=42, body='hello')
