"""Unit tests for :mod:`gate.agent.pr_capture` — the shared PR-URL parser.

The regex + classifier are the source-of-truth for BOTH the SDK-loop's
create-return capture (in ``gate/agent/initiative.py``) AND the MCP
admin's ``gh pr create`` subprocess wrapper (in ``gate/tools/pr_back.py``).
Pinning them here means a regex tweak that breaks one caller can never
land without a test flip.
"""

from __future__ import annotations

import pytest

from gate.agent.pr_capture import (
    PR_URL_RE,
    is_gh_pr_create_command,
    parse_pr_number_from_gh_output,
)

# ─── parse_pr_number_from_gh_output ─────────────────────────────────────


class TestParsePrNumber:
    def test_parses_from_bare_url(self) -> None:
        """Canonical ``gh pr create`` stdout: URL + trailing newline."""
        text = 'https://github.com/mikelear/leartech-automated-agent/pull/42\n'
        assert parse_pr_number_from_gh_output(text) == 42

    def test_parses_from_url_without_newline(self) -> None:
        """Some environments strip trailing newlines before this helper sees the stdout."""
        text = 'https://github.com/mikelear/leartech-automated-agent/pull/42'
        assert parse_pr_number_from_gh_output(text) == 42

    def test_parses_from_url_embedded_in_prose(self) -> None:
        """gh pr create sometimes prints ``Remote: ...`` progress lines before the URL."""
        text = (
            'remote: Enumerating objects...\n'
            'To https://github.com/mikelear/leartech-automated-agent.git\n'
            'https://github.com/mikelear/leartech-automated-agent/pull/777\n'
        )
        assert parse_pr_number_from_gh_output(text) == 777

    def test_parses_large_pr_number(self) -> None:
        """Some active repos have 4+ digit PR numbers — no artificial ceiling."""
        text = 'https://github.com/mikelear/leartech-automated-agent/pull/12345\n'
        assert parse_pr_number_from_gh_output(text) == 12345

    def test_returns_none_on_empty_string(self) -> None:
        assert parse_pr_number_from_gh_output('') is None

    def test_returns_none_on_no_url(self) -> None:
        """No GitHub URL in the text → None (no false positive)."""
        assert parse_pr_number_from_gh_output('nothing interesting happened') is None

    def test_returns_none_on_issue_url(self) -> None:
        """``/issues/N`` is not ``/pull/N`` — must not match."""
        text = 'https://github.com/mikelear/leartech-automated-agent/issues/42\n'
        assert parse_pr_number_from_gh_output(text) is None

    def test_returns_none_on_commits_url(self) -> None:
        text = 'https://github.com/mikelear/leartech-automated-agent/commits/main\n'
        assert parse_pr_number_from_gh_output(text) is None

    def test_returns_first_url_when_multiple_present(self) -> None:
        """gh pr create returns ONE URL; multiple would indicate stray content
        we tolerate by taking the first. The classifier + downstream caller
        already narrow this to the specific ``gh pr create`` tool_result."""
        text = (
            'https://github.com/mikelear/leartech-automated-agent/pull/42\n'
            'https://github.com/mikelear/leartech-automated-agent/pull/99\n'
        )
        assert parse_pr_number_from_gh_output(text) == 42


class TestPrUrlRegex:
    """Direct regex characterisation — one place to spot a subtle break."""

    def test_matches_standard_url(self) -> None:
        match = PR_URL_RE.search('https://github.com/foo/bar/pull/42')
        assert match is not None
        assert match.group(1) == '42'

    def test_does_not_match_http_scheme(self) -> None:
        """Only ``https://`` — never plain ``http://``. Matches gh's actual output."""
        assert PR_URL_RE.search('http://github.com/foo/bar/pull/42') is None

    def test_does_not_match_enterprise_github(self) -> None:
        """Enterprise GitHub uses ``github.example.com`` — different host."""
        assert PR_URL_RE.search('https://github.example.com/foo/bar/pull/42') is None


# ─── is_gh_pr_create_command ─────────────────────────────────────────────


class TestIsGhPrCreateCommand:
    def test_matches_bare_command(self) -> None:
        assert is_gh_pr_create_command('gh pr create --title X --body Y') is True

    def test_matches_with_cd_prefix(self) -> None:
        """Agent sometimes prepends ``cd /path && ...`` — must still match."""
        assert is_gh_pr_create_command('cd /workspace/repo && gh pr create --title X') is True

    def test_matches_with_trailing_pipe(self) -> None:
        assert is_gh_pr_create_command('gh pr create --title X | tee /tmp/pr.log') is True

    def test_does_not_match_gh_pr_view(self) -> None:
        """``gh pr view`` is a DIFFERENT subcommand — must not arm capture."""
        assert is_gh_pr_create_command('gh pr view 42') is False

    def test_does_not_match_gh_pr_comment(self) -> None:
        assert is_gh_pr_create_command('gh pr comment 42 --body /hold') is False

    def test_does_not_match_gh_pr_list(self) -> None:
        assert is_gh_pr_create_command('gh pr list --head agent/foo') is False

    def test_does_not_match_git_command(self) -> None:
        assert is_gh_pr_create_command('git commit -m "gh pr create in message"') is False

    def test_returns_false_on_empty(self) -> None:
        assert is_gh_pr_create_command('') is False

    @pytest.mark.parametrize('cmd', ['', '   ', None])
    def test_returns_false_on_empty_or_whitespace(self, cmd: str | None) -> None:
        # None-safety: the SDK may hand us None if the command key is missing.
        # The helper's contract is "no crash on falsy input".
        assert is_gh_pr_create_command(cmd or '') is False
