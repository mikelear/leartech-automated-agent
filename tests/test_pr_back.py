"""Unit tests for gate.tools.pr_back — the GitOps PR-back helper.

Mocks asyncio.create_subprocess_exec to simulate git + gh commands
without touching the filesystem (beyond the temp dir write) or network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gate.tools.pr_back import open_yaml_change_pr

_REPO = 'leartech-automated-agent'
_BASE = 'main'
_BRANCH = 'agent/test-pr-back'
_FILE = 'gate/agent/mcp_catalog.yaml'
_CONTENT = 'mcp_servers: {}\nroles: {}\n'
_MSG = 'test: update catalog'
_TITLE = 'Test PR'
_BODY = 'Test body'
_PR_URL = 'https://github.com/mikelear/leartech-automated-agent/pull/42'


def _proc(returncode: int = 0, stdout: str = '', stderr: str = '') -> MagicMock:
    """Build a mock asyncio.Process returning the given (stdout, stderr) bytes."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


async def test_open_yaml_change_pr_runs_expected_commands() -> None:
    """Assert clone, checkout -b, add, commit, push, gh pr create are all called."""
    procs = [
        _proc(0),                    # gh repo clone
        _proc(0),                    # git checkout -b
        _proc(0),                    # git add
        _proc(0),                    # git commit
        _proc(0),                    # git push
        _proc(0, stdout=_PR_URL),    # gh pr create
    ]

    with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock, side_effect=procs) as mock_exec:
        result = await open_yaml_change_pr(
            repo=_REPO,
            base_branch=_BASE,
            new_branch=_BRANCH,
            file_path=_FILE,
            new_yaml_content=_CONTENT,
            commit_message=_MSG,
            pr_title=_TITLE,
            pr_body=_BODY,
        )

    assert mock_exec.await_count == 6

    calls = mock_exec.call_args_list

    # 1. gh repo clone
    assert calls[0].args[0] == 'gh'
    assert calls[0].args[1] == 'repo'
    assert calls[0].args[2] == 'clone'
    assert 'mikelear/leartech-automated-agent' in calls[0].args

    # 2. git checkout -b
    assert calls[1].args[0] == 'git'
    assert 'checkout' in calls[1].args
    assert '-b' in calls[1].args
    assert _BRANCH in calls[1].args

    # 3. git add
    assert calls[2].args[0] == 'git'
    assert 'add' in calls[2].args
    assert _FILE in calls[2].args

    # 4. git commit
    assert calls[3].args[0] == 'git'
    assert 'commit' in calls[3].args
    assert _MSG in calls[3].args

    # 5. git push
    assert calls[4].args[0] == 'git'
    assert 'push' in calls[4].args
    assert _BRANCH in calls[4].args

    # 6. gh pr create
    assert calls[5].args[0] == 'gh'
    assert 'pr' in calls[5].args
    assert 'create' in calls[5].args
    assert _TITLE in calls[5].args
    assert _BODY in calls[5].args

    # Returned dict must be well-formed
    assert result['pr_url'] == _PR_URL
    assert result['branch'] == _BRANCH


async def test_open_yaml_change_pr_parses_pr_url() -> None:
    """Feed canned gh pr create output; assert pr_url == URL and pr_number == 42."""
    url_with_newline = _PR_URL + '\n'
    procs = [
        _proc(0),                           # gh repo clone
        _proc(0),                           # git checkout -b
        _proc(0),                           # git add
        _proc(0),                           # git commit
        _proc(0),                           # git push
        _proc(0, stdout=url_with_newline),  # gh pr create — trailing newline is typical
    ]

    with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock, side_effect=procs):
        result = await open_yaml_change_pr(
            repo=_REPO,
            base_branch=_BASE,
            new_branch=_BRANCH,
            file_path=_FILE,
            new_yaml_content=_CONTENT,
            commit_message=_MSG,
            pr_title=_TITLE,
            pr_body=_BODY,
        )

    assert result['pr_url'] == _PR_URL       # trailing newline stripped
    assert result['pr_number'] == 42          # parsed from /pull/42
    assert result['branch'] == _BRANCH


async def test_open_yaml_change_pr_propagates_gh_errors() -> None:
    """Simulate gh pr create failure; assert RuntimeError raised with the stderr message."""
    procs = [
        _proc(0),                                           # gh repo clone
        _proc(0),                                           # git checkout -b
        _proc(0),                                           # git add
        _proc(0),                                           # git commit
        _proc(0),                                           # git push
        _proc(1, stderr='HTTP 422 Unprocessable Entity'),   # gh pr create fails
    ]

    with patch('asyncio.create_subprocess_exec', new_callable=AsyncMock, side_effect=procs):
        with pytest.raises(RuntimeError, match='HTTP 422 Unprocessable Entity'):
            await open_yaml_change_pr(
                repo=_REPO,
                base_branch=_BASE,
                new_branch=_BRANCH,
                file_path=_FILE,
                new_yaml_content=_CONTENT,
                commit_message=_MSG,
                pr_title=_TITLE,
                pr_body=_BODY,
            )
