"""Tests for the ``leartech-agent-local`` MCP server.

Covers the two tools that couldn't move to the remote tekton MCP because
they depend on state inside the agent pod:

- ``classify_step_failure`` — LLM-adjacent heuristic classifier (imports
  :mod:`gate.agent.step_failure_diagnosis`).
- ``rebase_branch_on_base`` — git ops on the cloned consumer-repo workspace.

The `_run_git` subprocess seam is monkeypatched so tests don't invoke a real
git binary. Tests assert on the parsed return-shape of the helper functions
and on the builder's tool inventory.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from gate.mcp_servers import agent_local
from gate.mcp_servers.agent_local import (
    _ProcResult,
    build_agent_local_server,
    rebase_branch_on_base,
)

# ─── _run_git seam fixtures ───────────────────────────────────────────────────


@pytest.fixture
def mock_git(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every ``_run_git`` call and hand back canned responses.

    Tests append canned responses via ``_queue_canned_git`` (FIFO). The
    call record is read back via ``_git_calls`` — the fixture skips the
    sentinel entry it uses to expose the canned-queue reference.
    """
    recorded: list[dict[str, Any]] = []
    canned: list[_ProcResult] = []

    def fake_run_git(args: list[str], cwd: str, timeout: int = 60) -> _ProcResult:
        recorded.append({'args': list(args), 'cwd': cwd, 'timeout': timeout})
        if canned:
            return canned.pop(0)
        return _ProcResult(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(agent_local, '_run_git', fake_run_git)
    recorded.append({'_canned_ref': canned})
    yield recorded


def _queue_canned_git(mock_git: list[dict[str, Any]], *results: _ProcResult) -> None:
    sentinel = mock_git[0]
    assert '_canned_ref' in sentinel
    sentinel['_canned_ref'].extend(results)


def _git_calls(mock_git: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in mock_git if '_canned_ref' not in c]


# ─── rebase_branch_on_base ────────────────────────────────────────────────────


def test_rebase_clean_pushes_with_force_with_lease(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """No conflicts → fetch + rebase + force-with-lease push, all green."""
    _queue_canned_git(
        mock_git,
        _ProcResult(returncode=0, stdout='', stderr=''),  # fetch
        _ProcResult(returncode=0, stdout='Successfully rebased', stderr=''),  # rebase
        _ProcResult(returncode=0, stdout='everything up-to-date', stderr=''),  # push
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo')
    assert result['status'] == 'rebased'
    assert result['pushed'] is True
    assert result['conflicted_files'] == []
    calls = _git_calls(mock_git)
    assert calls[0]['args'] == ['fetch', 'origin', 'main']
    assert calls[1]['args'] == ['rebase', '-Xtheirs', 'origin/main']
    # CRITICAL: force-with-lease, not plain force (safety against concurrent human push)
    assert calls[2]['args'] == ['push', '--force-with-lease', 'origin', 'agent/foo']


def test_rebase_with_unmergeable_conflicts_aborts_and_returns_files(
    mock_git: list[dict[str, Any]],
    tmp_path: Path,
) -> None:
    """UU paths → abort + return conflicted file list. NO push attempted."""
    porcelain_output = 'UU gate/foo.py\nUU tests/test_foo.py\n M unrelated.py\n'
    _queue_canned_git(
        mock_git,
        _ProcResult(returncode=0, stdout='', stderr=''),  # fetch
        _ProcResult(returncode=1, stdout='', stderr='CONFLICT (content)'),  # rebase fails
        _ProcResult(returncode=0, stdout=porcelain_output, stderr=''),  # status
        _ProcResult(returncode=0, stdout='', stderr=''),  # abort
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo', base='main')
    assert result['status'] == 'conflict'
    assert result['pushed'] is False
    assert sorted(result['conflicted_files']) == ['gate/foo.py', 'tests/test_foo.py']
    calls = _git_calls(mock_git)
    # 4 calls: fetch, rebase, status, abort. No push.
    assert len(calls) == 4
    assert ['push', '--force-with-lease', 'origin', 'agent/foo'] not in [c['args'] for c in calls]
    # Abort was called to leave the worktree sane.
    assert calls[3]['args'] == ['rebase', '--abort']


def test_rebase_fetch_failure_returns_error(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    _queue_canned_git(
        mock_git,
        _ProcResult(returncode=1, stdout='', stderr='Could not resolve host'),
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo')
    assert result['status'] == 'error'
    assert result['pushed'] is False
    assert 'Could not resolve host' in result['message']
    # We bail BEFORE attempting rebase — only the fetch call should have run.
    assert len(_git_calls(mock_git)) == 1


def test_rebase_push_failure_returns_error(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """Clean rebase but ``--force-with-lease`` push rejected (concurrent push) → error."""
    _queue_canned_git(
        mock_git,
        _ProcResult(returncode=0, stdout='', stderr=''),  # fetch
        _ProcResult(returncode=0, stdout='', stderr=''),  # rebase clean
        _ProcResult(returncode=1, stdout='', stderr='stale info'),  # push rejected
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo')
    assert result['status'] == 'error'
    assert result['pushed'] is False
    assert 'stale info' in result['message']


def test_rebase_uses_custom_base(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """``base='release/v2'`` → fetch + rebase against that ref."""
    _queue_canned_git(
        mock_git,
        _ProcResult(returncode=0, stdout='', stderr=''),
        _ProcResult(returncode=0, stdout='', stderr=''),
        _ProcResult(returncode=0, stdout='', stderr=''),
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo', base='release/v2')
    assert result['status'] == 'rebased'
    calls = _git_calls(mock_git)
    assert calls[0]['args'] == ['fetch', 'origin', 'release/v2']
    assert calls[1]['args'] == ['rebase', '-Xtheirs', 'origin/release/v2']


# ─── classify_step_failure tool wrapper ───────────────────────────────────────


@pytest.mark.asyncio
async def test_classify_step_failure_tool_dispatches_to_diagnosis() -> None:
    """The MCP tool wrapper forwards args to :func:`classify_step_failure`.

    We don't reimplement the classification tests here — those live in
    ``tests/test_step_failure_diagnosis.py``. This is a smoke test that the
    wrapper marshals args, invokes the diagnosis module, and JSON-encodes
    the result.
    """
    from gate.mcp_servers.agent_local import _classify_step_failure_tool

    # A ruff-format-error shape — a well-known heuristic hit.
    log_tail = 'Would reformat: gate/foo.py\n'
    result = await _classify_step_failure_tool.handler(
        {
            'step_name': 'ruff',
            'log_tail': log_tail,
            'pipelinerun': 'foo-lint-abc',
        }
    )
    assert 'content' in result
    text = result['content'][0]['text']
    parsed = json.loads(text)
    assert parsed['classification'] == 'ruff_format_error'
    assert parsed['action'] == 'fix_code'
    assert parsed['step_name'] == 'ruff'
    assert parsed['pipelinerun'] == 'foo-lint-abc'


@pytest.mark.asyncio
async def test_classify_step_failure_tool_empty_log_returns_unknown() -> None:
    """Empty log → unknown/escalate (matches ``classify_step_failure``'s contract)."""
    from gate.mcp_servers.agent_local import _classify_step_failure_tool

    result = await _classify_step_failure_tool.handler({'step_name': 'ruff', 'log_tail': ''})
    parsed = json.loads(result['content'][0]['text'])
    assert parsed['classification'] == 'unknown'
    assert parsed['action'] == 'escalate'


# ─── Builder smoke test ───────────────────────────────────────────────────────


def test_build_agent_local_server_exposes_two_tools() -> None:
    """The MCP builder wires exactly the two tools that couldn't move remote."""
    server = build_agent_local_server()
    assert server is not None
    # Best-effort tool-name extraction — same shape as other MCP-server tests.
    instance = server['instance'] if isinstance(server, dict) else getattr(server, 'instance', None)
    if instance is not None and hasattr(instance, '_tool_handlers'):
        names = {t.name for t in instance._tool_handlers.values()}
        assert names == {'classify_step_failure', 'rebase_branch_on_base'}


def test_agent_local_wired_into_initiative_role() -> None:
    """``initiative.py`` allowlists the two agent-local tools under the new server name."""
    from gate.agent.initiative import INITIATIVE_TEKTON_TOOLS

    assert 'mcp__leartech-agent-local__classify_step_failure' in INITIATIVE_TEKTON_TOOLS
    assert 'mcp__leartech-agent-local__rebase_branch_on_base' in INITIATIVE_TEKTON_TOOLS
    # And the old names must NOT still be present.
    assert 'mcp__leartech-tekton__classify_step_failure' not in INITIATIVE_TEKTON_TOOLS
    assert 'mcp__leartech-tekton__rebase_branch_on_base' not in INITIATIVE_TEKTON_TOOLS
