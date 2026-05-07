"""Unified diff for a PR via gh api. Used by criteria that inspect what changed (e.g. test_no_skipped_or_focused_tests, test_unit_spec_count_changed)."""

from __future__ import annotations

import subprocess


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def fetch_pr_diff(repo: str, pr_number: int) -> str:
    """Returns the unified diff for the PR (across all files) as a single string.

    Equivalent to `gh pr diff <n>` but goes through `gh api` so it works regardless
    of whether the user has the repo cloned locally.
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    return _gh(
        [
            'api',
            f'repos/{qualified}/pulls/{pr_number}',
            '-H',
            'Accept: application/vnd.github.v3.diff',
        ]
    )


def added_lines(diff: str) -> list[str]:
    """Lines added by the PR (lines starting with '+' but excluding diff headers like '+++')."""
    return [line[1:] for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++')]


def added_files(diff: str, pattern: str | None = None) -> list[str]:
    """Files newly created or modified in the PR. With pattern, restricts by suffix match."""
    files: list[str] = []
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            path = line[6:]
            if pattern is None or path.endswith(pattern):
                files.append(path)
    return files
