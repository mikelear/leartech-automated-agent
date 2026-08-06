"""Unit tests for :mod:`gate.agent.pr_capture` — the PR-URL parser.

The regex is the source-of-truth for the MCP admin's ``gh pr create`` subprocess
wrapper (``gate/tools/pr_back.py``). Pinning it here means a regex tweak that breaks
the caller can never land without a test flip. (The old SDK-loop
``is_gh_pr_create_command`` classifier was removed when the dev-agent loop stopped
scraping PR URLs — ``open_pr`` records the number authoritatively now.)
"""

from __future__ import annotations

from gate.agent.pr_capture import (
    PR_URL_RE,
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
        we tolerate by taking the first."""
        text = (
            'https://github.com/mikelear/leartech-automated-agent/pull/42\n'
            'https://github.com/mikelear/leartech-automated-agent/pull/99\n'
        )
        # gh pr create returns ONE URL; the sole consumer (pr_back) parses the
        # stdout of a single create subprocess, so taking the first is correct.
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
