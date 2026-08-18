"""Unit tests for `_clone_repo` in gate.agent.initiative.

Covers the 2026-05-28 switch from `gh repo clone` to direct
`git clone` over HTTPS with a token-in-URL. The motivation: `gh repo
clone` resolves the clone URL via GitHub's GraphQL API, sharing a
5000pts/h bucket with operator-side `gh` usage. When that bucket is
exhausted, every fresh agent fire fails at the FIRST step (clone) with
`GraphQL: API rate limit already exceeded`. Direct `git clone` hits
no API — just the git wire protocol — so it's immune to the pattern.

What we verify here:
- The command is `git clone` (not `gh repo clone`).
- The URL uses GitHub's documented `x-access-token:<token>@github.com/...`
  auth format.
- `--depth 1` is passed so the clone stays fast on cluster pods.
- Missing GH_TOKEN returns exit code 2 (the existing contract — surfaced
  to callers as `RunSummary(exit_code=2)`).
- A failing clone redacts the token from stderr before logging it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from gate.agent.initiative import _clone_repo


def test_clone_repo_uses_git_not_gh_with_token_in_url(tmp_path: Path) -> None:
    """The clone command must be `git clone` with the GH_TOKEN baked into
    the HTTPS URL as `x-access-token:<token>` (GitHub's documented auth
    format). It must NOT be `gh repo clone`, which resolves the URL via
    the GraphQL API and consumes shared quota.
    """
    target = tmp_path / 'leartech-automated-agent'
    with (
        patch.dict('os.environ', {'GH_TOKEN': 'ghs_secret_token_123'}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout='', stderr='')
        exit_code, failure_reason = _clone_repo(qualified_repo='mikelear/leartech-automated-agent', cwd=target)

    assert exit_code == 0
    assert failure_reason is None, 'success path must report no failure reason'
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0:2] == ['git', 'clone'], (
        f'expected git clone, got {args[0:2]} — see test docstring for why this matters'
    )
    assert '--depth' in args
    assert '1' in args
    url = next(a for a in args if a.startswith('https://'))
    assert 'x-access-token:ghs_secret_token_123@github.com' in url
    assert url.endswith('/mikelear/leartech-automated-agent.git')


def test_clone_repo_returns_2_when_gh_token_missing(tmp_path: Path) -> None:
    """Without GH_TOKEN the clone can't authenticate; the helper must
    return exit code 2 (surfaced as RunSummary(exit_code=2) by the
    caller) and must NOT invoke subprocess.run at all.
    """
    target = tmp_path / 'never-cloned'
    with (
        patch.dict('os.environ', {}, clear=True),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        exit_code, failure_reason = _clone_repo(qualified_repo='mikelear/leartech-automated-agent', cwd=target)

    assert exit_code == 2
    assert failure_reason is not None
    assert failure_reason.startswith('clone_failed:')
    assert 'GH_TOKEN' in failure_reason
    mock_run.assert_not_called()


def test_clone_repo_returns_2_on_git_failure_and_redacts_token(tmp_path: Path, capsys: object) -> None:
    """If `git clone` exits non-zero, the helper must return 2 AND must
    redact the token from any stderr it echoes — token leakage in logs
    would defeat the whole reason we kept it out of `gh`.
    """
    target = tmp_path / 'failed-clone'
    fake_token = 'ghs_super_secret_xyz'  # noqa: S105 — synthetic test fixture, not a real credential
    leaky_stderr = (
        f"fatal: unable to access 'https://x-access-token:{fake_token}@github.com/foo/bar.git/': "
        'The requested URL returned error: 403'
    )
    with (
        patch.dict('os.environ', {'GH_TOKEN': fake_token}, clear=False),
        patch('gate.agent.initiative.subprocess.run') as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=128, stdout='', stderr=leaky_stderr)
        exit_code, failure_reason = _clone_repo(qualified_repo='foo/bar', cwd=target)

    assert exit_code == 2
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert fake_token not in captured.err
    assert '***REDACTED***' in captured.err
    assert failure_reason is not None
    assert failure_reason.startswith('clone_failed:')
    assert fake_token not in failure_reason
