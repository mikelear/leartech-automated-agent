"""Tests for the leartech-tekton MCP server (`gate.mcp_servers.tekton`).

We mock the kubectl shellout via `_run_kubectl` — the single seam that all
higher-level helpers go through. Tests assert on parsed dict-of-strings shapes
returned by the public helpers, NOT on the MCP tool wrappers themselves (those
are thin re-encoders and are smoke-checked via `build_tekton_server`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from gate.mcp_servers import tekton
from gate.mcp_servers.tekton import (
    _KubectlResult,
    build_tekton_server,
    cancel_pipelinerun,
    cancel_superseded_for_pr,
    list_pipelineruns_for_pr,
    rebase_branch_on_base,
    step_logs,
    step_status,
)

# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_kubectl(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture every `_run_kubectl` call and return a queue of canned results.

    Tests append result dicts onto the returned `recorded` list as well so they
    can assert on the cmd args passed to kubectl. Each call pops one canned
    response in FIFO order.
    """
    recorded: list[dict[str, Any]] = []
    canned: list[_KubectlResult] = []

    def fake_run_kubectl(args: list[str], cluster: str, timeout: int = 30) -> _KubectlResult:
        recorded.append({'args': list(args), 'cluster': cluster, 'timeout': timeout})
        if canned:
            return canned.pop(0)
        return _KubectlResult(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(tekton, '_run_kubectl', fake_run_kubectl)
    # Expose the canned-queue back to the test via the recorded list's metadata.
    # We attach it as the first item's `_canned_ref` if needed — but tests prefer
    # to mutate `canned` directly via the returned reference below.
    recorded.append({'_canned_ref': canned})  # sentinel; tests pop it before assertions
    yield recorded


def _queue_canned(mock_kubectl: list[dict[str, Any]], *results: _KubectlResult) -> None:
    """Append canned results into the fake kubectl's FIFO queue."""
    sentinel = mock_kubectl[0]
    assert '_canned_ref' in sentinel
    sentinel['_canned_ref'].extend(results)


def _calls(mock_kubectl: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the recorded kubectl calls minus the canned-queue sentinel."""
    return [c for c in mock_kubectl if '_canned_ref' not in c]


# ─── Sample payloads ──────────────────────────────────────────────────────────


_PR_LIST_TWO_RUNS = {
    'items': [
        {
            'metadata': {
                'name': 'webcoder-ui-pr-7-lint-abc',
                'labels': {
                    'lighthouse.jenkins-x.io/lastCommitSHA': 'sha-OLD',
                    'lighthouse.jenkins-x.io/context': 'lint',
                    'tekton.dev/pipeline': 'lint',
                },
            },
            'status': {
                'startTime': '2026-05-29T10:00:00Z',
                'completionTime': '2026-05-29T10:02:00Z',
                'conditions': [{'status': 'True', 'reason': 'Succeeded'}],
            },
        },
        {
            'metadata': {
                'name': 'webcoder-ui-pr-7-pr-def',
                'labels': {
                    'lighthouse.jenkins-x.io/lastCommitSHA': 'sha-NEW',
                    'lighthouse.jenkins-x.io/context': 'pr',
                    'tekton.dev/pipeline': 'pullrequest',
                },
            },
            'status': {
                'startTime': '2026-05-29T10:10:00Z',
                'conditions': [{'status': 'Unknown', 'reason': 'Running'}],
            },
        },
    ]
}


_PR_WITH_FAILED_STEP = {
    'status': {
        'taskRuns': {
            'lint-tr-1': {
                'pipelineTaskName': 'lint',
                'status': {
                    'podName': 'lint-tr-1-pod',
                    'steps': [
                        {
                            'name': 'git-clone',
                            'terminated': {'exitCode': 0, 'reason': 'Completed'},
                        },
                        {
                            'name': 'ruff',
                            'terminated': {'exitCode': 1, 'reason': 'Error'},
                        },
                        {
                            'name': 'mypy',
                            # never ran — earlier step failed
                            'waiting': {'reason': 'PodInitializing'},
                        },
                    ],
                },
            }
        }
    }
}


_PR_WITH_RUNNING_STEPS = {
    'status': {
        'taskRuns': {
            'pr-tr-1': {
                'pipelineTaskName': 'pr',
                'status': {
                    'podName': 'pr-tr-1-pod',
                    'steps': [
                        {'name': 'git-clone', 'terminated': {'exitCode': 0, 'reason': 'Completed'}},
                        {'name': 'pytest', 'running': {}},
                    ],
                },
            }
        }
    }
}


# ─── list_pipelineruns_for_pr ─────────────────────────────────────────────────


def test_list_pipelineruns_parses_json_and_orders_newest_first(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(_PR_LIST_TWO_RUNS), stderr=''),
    )
    rows = list_pipelineruns_for_pr('webcoder-ui', 7, 'gcp')
    assert len(rows) == 2
    # Newest first by startTime
    assert rows[0]['name'] == 'webcoder-ui-pr-7-pr-def'
    assert rows[0]['status'] == 'Running'
    assert rows[0]['sha'] == 'sha-NEW'
    assert rows[1]['name'] == 'webcoder-ui-pr-7-lint-abc'
    assert rows[1]['status'] == 'Succeeded'
    assert rows[1]['sha'] == 'sha-OLD'
    # And the call used the right selector + cluster (kubectl ctx is added by
    # _run_kubectl after our seam, so we assert on the cluster name we passed
    # to the seam, not on the resolved context).
    call = _calls(mock_kubectl)[0]
    assert call['cluster'] == 'gcp'
    assert 'lighthouse.jenkins-x.io/refs.pull=7' in ' '.join(call['args'])
    assert 'lighthouse.jenkins-x.io/refs.repo=mikelear/webcoder-ui' in ' '.join(call['args'])


def test_list_pipelineruns_empty_on_no_results(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=0, stdout='', stderr=''))
    assert list_pipelineruns_for_pr('webcoder-ui', 999, 'az') == []


def test_list_pipelineruns_empty_when_kubectl_errors(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=1, stdout='', stderr='no such resource'))
    assert list_pipelineruns_for_pr('webcoder-ui', 7, 'az') == []


def test_list_pipelineruns_unknown_cluster_raises() -> None:
    # Reaches into _context_for via the kubectl seam — but list_pipelineruns_for_pr
    # calls _run_kubectl which validates the cluster. Tests that error surfaces.
    with pytest.raises(ValueError, match='Unknown cluster'):
        list_pipelineruns_for_pr('webcoder-ui', 7, 'aws')


# ─── step_status ──────────────────────────────────────────────────────────────


def test_step_status_surfaces_failed_step_with_reason(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(_PR_WITH_FAILED_STEP), stderr=''),
    )
    rows = step_status('webcoder-ui-pr-7-lint-abc', 'az')
    by_step = {r['step']: r for r in rows}
    assert by_step['git-clone']['state'] == 'Succeeded'
    assert by_step['ruff']['state'] == 'Failed'
    assert by_step['ruff']['exit_code'] == 1
    assert by_step['mypy']['state'] == 'Pending'
    # Pod is surfaced so step_logs can find it
    assert all(r['pod'] == 'lint-tr-1-pod' for r in rows)


def test_step_status_handles_running_steps(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(_PR_WITH_RUNNING_STEPS), stderr=''),
    )
    rows = step_status('webcoder-ui-pr-7-pr-def', 'gcp')
    by_step = {r['step']: r for r in rows}
    assert by_step['git-clone']['state'] == 'Succeeded'
    assert by_step['pytest']['state'] == 'Running'
    assert by_step['pytest']['exit_code'] is None


def test_step_status_empty_when_kubectl_errors(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=1, stdout='', stderr='not found'))
    assert step_status('does-not-exist', 'az') == []


# ─── step_logs ────────────────────────────────────────────────────────────────


def test_step_logs_returns_log_text_when_pod_resolves(mock_kubectl: list[dict[str, Any]]) -> None:
    # First call: step_status lookup; second call: kubectl logs.
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(_PR_WITH_FAILED_STEP), stderr=''),
        _KubectlResult(returncode=0, stdout='E901 SyntaxError: invalid syntax\n', stderr=''),
    )
    text = step_logs('webcoder-ui-pr-7-lint-abc', 'ruff', 'az', tail=50)
    assert 'SyntaxError' in text
    # Confirm second call shape: `kubectl logs <pod> -c step-<name> --tail=50`
    logs_call = _calls(mock_kubectl)[1]
    assert 'logs' in logs_call['args']
    assert 'lint-tr-1-pod' in logs_call['args']
    assert 'step-ruff' in logs_call['args']
    assert '--tail=50' in logs_call['args']


def test_step_logs_returns_empty_when_pod_missing(mock_kubectl: list[dict[str, Any]]) -> None:
    # The step exists in the payload but has no pod attached.
    payload_no_pod = {
        'status': {
            'taskRuns': {
                'lint-tr-1': {
                    'pipelineTaskName': 'lint',
                    'status': {
                        'podName': '',  # GC'd
                        'steps': [{'name': 'ruff', 'terminated': {'exitCode': 1}}],
                    },
                }
            }
        }
    }
    _queue_canned(mock_kubectl, _KubectlResult(returncode=0, stdout=json.dumps(payload_no_pod), stderr=''))
    assert step_logs('some-run', 'ruff', 'az') == ''


def test_step_logs_returns_empty_when_step_name_wrong(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(_PR_WITH_FAILED_STEP), stderr=''),
    )
    assert step_logs('webcoder-ui-pr-7-lint-abc', 'no-such-step', 'az') == ''


# ─── cancel_pipelinerun ───────────────────────────────────────────────────────


def test_cancel_pipelinerun_returns_true_on_success(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=0, stdout='patched', stderr=''))
    assert cancel_pipelinerun('webcoder-ui-pr-7-lint-abc', 'az') is True
    call = _calls(mock_kubectl)[0]
    assert 'patch' in call['args']
    assert 'webcoder-ui-pr-7-lint-abc' in call['args']
    # The merge-patch body carries PipelineRunCancelled
    body = next((a for a in call['args'] if 'PipelineRunCancelled' in a), '')
    assert 'PipelineRunCancelled' in body


def test_cancel_pipelinerun_returns_false_when_patch_fails(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=1, stdout='', stderr='not found'))
    assert cancel_pipelinerun('does-not-exist', 'gcp') is False


# ─── cancel_superseded_for_pr ─────────────────────────────────────────────────


def test_cancel_superseded_only_cancels_non_matching_sha(mock_kubectl: list[dict[str, Any]]) -> None:
    """Two runs on different SHAs, neither terminal → only the non-keep one is cancelled."""
    payload = {
        'items': [
            {
                'metadata': {
                    'name': 'old-run',
                    'labels': {'lighthouse.jenkins-x.io/lastCommitSHA': 'sha-OLD'},
                },
                'status': {
                    'startTime': '2026-05-29T10:00:00Z',
                    'conditions': [{'status': 'Unknown', 'reason': 'Running'}],
                },
            },
            {
                'metadata': {
                    'name': 'new-run',
                    'labels': {'lighthouse.jenkins-x.io/lastCommitSHA': 'sha-NEW'},
                },
                'status': {
                    'startTime': '2026-05-29T10:10:00Z',
                    'conditions': [{'status': 'Unknown', 'reason': 'Running'}],
                },
            },
        ]
    }
    _queue_canned(
        mock_kubectl,
        _KubectlResult(returncode=0, stdout=json.dumps(payload), stderr=''),  # list call
        _KubectlResult(returncode=0, stdout='patched', stderr=''),  # cancel old-run
    )
    n = cancel_superseded_for_pr('webcoder-ui', 7, keep_sha='sha-NEW', cluster='az')
    assert n == 1
    # Verify we patched the OLD run, not the new one
    calls = _calls(mock_kubectl)
    assert len(calls) == 2
    patch_call = calls[1]
    assert 'old-run' in patch_call['args']
    assert 'new-run' not in patch_call['args']


def test_cancel_superseded_skips_already_terminal_runs(mock_kubectl: list[dict[str, Any]]) -> None:
    """A Succeeded run on an old SHA should not be patched — it's already done."""
    payload = {
        'items': [
            {
                'metadata': {
                    'name': 'old-but-succeeded',
                    'labels': {'lighthouse.jenkins-x.io/lastCommitSHA': 'sha-OLD'},
                },
                'status': {
                    'startTime': '2026-05-29T10:00:00Z',
                    'completionTime': '2026-05-29T10:01:00Z',
                    'conditions': [{'status': 'True', 'reason': 'Succeeded'}],
                },
            },
        ]
    }
    _queue_canned(mock_kubectl, _KubectlResult(returncode=0, stdout=json.dumps(payload), stderr=''))
    n = cancel_superseded_for_pr('webcoder-ui', 7, keep_sha='sha-NEW', cluster='az')
    assert n == 0
    # Only the list call should have happened — no patch
    assert len(_calls(mock_kubectl)) == 1


def test_cancel_superseded_returns_zero_when_no_runs(mock_kubectl: list[dict[str, Any]]) -> None:
    _queue_canned(mock_kubectl, _KubectlResult(returncode=0, stdout='', stderr=''))
    assert cancel_superseded_for_pr('webcoder-ui', 7, keep_sha='sha-NEW', cluster='gcp') == 0


# ─── Builder smoke test ───────────────────────────────────────────────────────


def test_build_tekton_server_constructs_with_g2_tools() -> None:
    """The MCP builder wires every tool the catalog promises — G.1 + G.2."""
    server = build_tekton_server()
    assert server is not None
    # Best-effort tool-name extraction — same shape as test_mcp_servers.py.
    instance = server['instance'] if isinstance(server, dict) else getattr(server, 'instance', None)
    if instance is not None and hasattr(instance, '_tool_handlers'):
        names = [t.name for t in instance._tool_handlers.values()]
        expected = {
            # G.1 — Tekton inspection
            'list_pipelineruns_for_pr',
            'step_status',
            'step_logs',
            'cancel_pipelinerun',
            'cancel_superseded_for_pr',
            'wait_first_failure',
            # G.2 — step-aware diagnosis + rebase
            'classify_step_failure',
            'rebase_branch_on_base',
        }
        assert set(names) == expected


# ─── G.2: rebase_branch_on_base ──────────────────────────────────────────────


@pytest.fixture
def mock_git(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Mirror of `mock_kubectl` for the `_run_git` seam.

    Tests append canned `_KubectlResult` responses via `_queue_canned_git`
    and read the call shape back via `_git_calls`.
    """
    recorded: list[dict[str, Any]] = []
    canned: list[_KubectlResult] = []

    def fake_run_git(args: list[str], cwd: str, timeout: int = 60) -> _KubectlResult:
        recorded.append({'args': list(args), 'cwd': cwd, 'timeout': timeout})
        if canned:
            return canned.pop(0)
        return _KubectlResult(returncode=0, stdout='', stderr='')

    monkeypatch.setattr(tekton, '_run_git', fake_run_git)
    recorded.append({'_canned_ref': canned})
    return recorded


def _queue_canned_git(mock_git: list[dict[str, Any]], *results: _KubectlResult) -> None:
    sentinel = mock_git[0]
    assert '_canned_ref' in sentinel
    sentinel['_canned_ref'].extend(results)


def _git_calls(mock_git: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in mock_git if '_canned_ref' not in c]


def test_rebase_clean_pushes_with_force_with_lease(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """No conflicts → fetch + rebase + force-with-lease push, all green."""
    _queue_canned_git(
        mock_git,
        _KubectlResult(returncode=0, stdout='', stderr=''),  # fetch
        _KubectlResult(returncode=0, stdout='Successfully rebased', stderr=''),  # rebase
        _KubectlResult(returncode=0, stdout='everything up-to-date', stderr=''),  # push
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
        _KubectlResult(returncode=0, stdout='', stderr=''),  # fetch
        _KubectlResult(returncode=1, stdout='', stderr='CONFLICT (content)'),  # rebase fails
        _KubectlResult(returncode=0, stdout=porcelain_output, stderr=''),  # status
        _KubectlResult(returncode=0, stdout='', stderr=''),  # abort
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
        _KubectlResult(returncode=1, stdout='', stderr='Could not resolve host'),
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo')
    assert result['status'] == 'error'
    assert result['pushed'] is False
    assert 'Could not resolve host' in result['message']
    # We bail BEFORE attempting rebase — only the fetch call should have run.
    assert len(_git_calls(mock_git)) == 1


def test_rebase_push_failure_returns_error(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """Clean rebase but `--force-with-lease` push rejected (concurrent push) → error."""
    _queue_canned_git(
        mock_git,
        _KubectlResult(returncode=0, stdout='', stderr=''),  # fetch
        _KubectlResult(returncode=0, stdout='', stderr=''),  # rebase clean
        _KubectlResult(returncode=1, stdout='', stderr='stale info'),  # push rejected
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo')
    assert result['status'] == 'error'
    assert result['pushed'] is False
    assert 'stale info' in result['message']


def test_rebase_uses_custom_base(mock_git: list[dict[str, Any]], tmp_path: Path) -> None:
    """`base='release/v2'` → fetch + rebase against that ref."""
    _queue_canned_git(
        mock_git,
        _KubectlResult(returncode=0, stdout='', stderr=''),
        _KubectlResult(returncode=0, stdout='', stderr=''),
        _KubectlResult(returncode=0, stdout='', stderr=''),
    )
    result = rebase_branch_on_base(str(tmp_path), 'agent/foo', base='release/v2')
    assert result['status'] == 'rebased'
    calls = _git_calls(mock_git)
    assert calls[0]['args'] == ['fetch', 'origin', 'release/v2']
    assert calls[1]['args'] == ['rebase', '-Xtheirs', 'origin/release/v2']


def test_catalog_registers_leartech_tekton() -> None:
    """The committed catalog YAML wires leartech-tekton into initiative_agent."""
    from gate.agent.mcp_catalog import get_role, load_catalog

    load_catalog.cache_clear()
    catalog = load_catalog()
    assert 'leartech-tekton' in catalog.mcp_servers
    mcp = catalog.mcp_servers['leartech-tekton']
    assert mcp.type == 'sdk'
    assert mcp.builder == 'gate.mcp_servers.tekton:build_tekton_server'
    role = get_role('initiative_agent')
    assert 'leartech-tekton' in role.mcps
    # Other roles must NOT get it — initiative-runner-only per the goal spec.
    for other in ('review_agent', 'ba_agent', 'forensic_agent'):
        assert 'leartech-tekton' not in get_role(other).mcps
