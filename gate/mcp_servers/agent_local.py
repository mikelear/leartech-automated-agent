"""leartech-agent-local — in-process MCP wrappers for tools that can't move remote.

The rest of the tekton tool surface (``list_pipelineruns_for_pr``,
``step_status``, ``step_logs``, ``cancel_pipelinerun``,
``cancel_superseded_for_pr``, ``wait_first_failure``) is now served by the
Go ``leartech-mcp-servers/tekton`` deployment at
``${LEARTECH_MCP_URL}/mcp/tekton`` — wired in ``gate.mcp_servers.remote``.

Two tools stayed in-process because they depend on state that lives inside
the agent Job pod, not on the cluster:

- ``classify_step_failure`` — LLM-adjacent heuristic classifier that
  imports :mod:`gate.agent.step_failure_diagnosis`. Moving the classifier
  to a remote MCP would either duplicate the heuristic tables in Go or
  force the Go MCP to shell back to Python; both are worse than keeping
  it next to the diagnosis rules.
- ``rebase_branch_on_base`` — runs ``git fetch`` + ``git rebase
  -Xtheirs`` + ``git push --force-with-lease`` on the AGENT's cloned
  workspace. The remote MCP has no view of the agent pod's filesystem;
  moving this remote would require exposing the workspace over a network
  filesystem, which is a much worse boundary.

The MCP is named ``leartech-agent-local`` so tools surface as
``mcp__leartech-agent-local__classify_step_failure`` and
``mcp__leartech-agent-local__rebase_branch_on_base``. Add new agent-local
tools to :func:`build_agent_local_server` when they need in-process state
the remote can't reach.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

# NOTE: ``classify_step_failure`` (from ``gate.agent.step_failure_diagnosis``)
# is imported LAZILY inside ``_classify_step_failure_tool``, not at module
# scope. ``gate.agent.__init__`` eagerly imports ``initiative`` → ``main`` →
# ``gate.mcp_servers``; a top-level import here creates a
# ``gate.mcp_servers`` ⇄ ``gate.agent`` circular import that fails whenever
# ``gate.mcp_servers`` is imported as the first entry point. Keeping it
# function-local breaks the cycle at module-load time.


# ─── subprocess seam ─────────────────────────────────────────────────────────


@dataclass
class _ProcResult:
    """Compact (returncode, stdout, stderr) shape shared by subprocess helpers.

    Kept private to this module — callers use the higher-level helpers. Tests
    monkeypatch :func:`_run_git` and construct :class:`_ProcResult` directly
    to feed canned responses.
    """

    returncode: int
    stdout: str
    stderr: str


def _run_git(args: list[str], cwd: str, timeout: int = 60) -> _ProcResult:
    """Run ``git <args...>`` inside ``cwd``. Single seam — tests monkeypatch this."""
    proc = subprocess.run(  # noqa: S603 — args are agent-controlled, cwd is agent-workspace
        ['git', *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    return _ProcResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# ─── rebase-on-base ─────────────────────────────────────────────────────────


def rebase_branch_on_base(repo_cwd: str, branch: str, base: str = 'main') -> dict[str, Any]:
    """Rebase ``branch`` onto ``origin/<base>`` and force-push-with-lease.

    Strategy: ``git fetch origin <base>`` → ``git rebase -Xtheirs origin/<base>``.
    ``-Xtheirs`` asks git to auto-resolve content conflicts in favour of the
    rebased branch (the agent's own commits), which is the correct choice in
    99% of cases: the agent's PR is the source of truth, main has only moved
    forward independently.

    A UU (unmerged) conflict — typical when both sides modified the same
    range of the same file — cannot be auto-resolved even with ``-Xtheirs``.
    In that case we abort the rebase and return ``{"status": "conflict",
    "conflicted_files": [...]}`` so the agent can post a sticky and escalate.

    On a clean rebase we push with ``--force-with-lease`` (NOT plain
    ``--force``) so a concurrent human push to the same branch isn't
    silently clobbered.

    Returns:
        ``{"status": "rebased" | "conflict" | "error", "pushed": bool,
        "conflicted_files": [...], "message": str}``
    """
    fetch = _run_git(['fetch', 'origin', base], cwd=repo_cwd)
    if fetch.returncode != 0:
        return {
            'status': 'error',
            'pushed': False,
            'conflicted_files': [],
            'message': f'git fetch origin {base} failed: {fetch.stderr.strip()}',
        }

    rebase = _run_git(['rebase', '-Xtheirs', f'origin/{base}'], cwd=repo_cwd)
    if rebase.returncode != 0:
        # Check for UU paths — paths git couldn't auto-resolve.
        status = _run_git(['status', '--porcelain'], cwd=repo_cwd)
        conflicted = [line[3:].strip() for line in status.stdout.splitlines() if line.startswith('UU ')]
        # Abort to leave the worktree in a sane state for the next step.
        _run_git(['rebase', '--abort'], cwd=repo_cwd)
        return {
            'status': 'conflict',
            'pushed': False,
            'conflicted_files': conflicted,
            'message': (
                f'Rebase onto origin/{base} produced unmergeable conflicts in '
                f'{len(conflicted)} file(s); aborted. {rebase.stderr.strip()}'
            ),
        }

    push = _run_git(['push', '--force-with-lease', 'origin', branch], cwd=repo_cwd)
    if push.returncode != 0:
        return {
            'status': 'error',
            'pushed': False,
            'conflicted_files': [],
            'message': f'force-with-lease push failed: {push.stderr.strip()}',
        }

    return {
        'status': 'rebased',
        'pushed': True,
        'conflicted_files': [],
        'message': f'Rebased {branch} onto origin/{base} and force-pushed-with-lease.',
    }


# ─── MCP tool wrappers ───────────────────────────────────────────────────────


@tool(
    'classify_step_failure',
    'Diagnose ONE failed Tekton step. Inputs: step_name (e.g. "ruff", "git-clone", "pytest") '
    'and log_tail (the last ~200 lines from step_logs). '
    'Returns {"classification": "git_merge_conflict|ruff_format_error|ruff_lint_error|mypy_type_error|'
    'pytest_test_failure|kaniko_build_failure|image_pull_backoff|ai_review_red_finding|tekton_step_oom|'
    'tekton_step_timeout|preview_deploy_failure|security_scan_finding|unknown", '
    '"action": "rebase|fix_code|fix_test|retry|escalate", "pipelinerun": str, "step_name": str}. '
    'Use this AFTER mcp__leartech-tekton__step_logs so the agent dispatches on the canonical failure shape '
    'rather than retrying blindly. Unknown failure → action=escalate (do NOT retry).',
    {'step_name': str, 'log_tail': str, 'pipelinerun': str},
)
async def _classify_step_failure_tool(args: dict[str, Any]) -> dict[str, Any]:
    # Lazy import — see the note at the top of this module (breaks the
    # gate.mcp_servers ⇄ gate.agent circular import at module-load time).
    from gate.agent.step_failure_diagnosis import classify_step_failure as _classify_step_failure

    failure = _classify_step_failure(
        step_name=str(args['step_name']),
        log_tail=str(args['log_tail']),
        pipelinerun=str(args.get('pipelinerun') or ''),
    )
    return {'content': [{'type': 'text', 'text': json.dumps(failure.to_dict(), indent=2)}]}


@tool(
    'rebase_branch_on_base',
    'Rebase the current PR branch onto origin/<base> (default `main`) and force-push-with-lease. '
    'Uses `-Xtheirs` to auto-resolve content conflicts in favour of the PR branch. '
    'Returns {"status": "rebased"|"conflict"|"error", "pushed": bool, "conflicted_files": [...], '
    '"message": str}. Use this when `classify_step_failure` returns action=rebase '
    '(git_merge_conflict during git-clone step). On `status: conflict` the agent must NOT '
    'retry — post a sticky listing the conflicted files and escalate.',
    {'repo_cwd': str, 'branch': str, 'base': str},
)
async def _rebase_branch_on_base(args: dict[str, Any]) -> dict[str, Any]:
    result = rebase_branch_on_base(
        repo_cwd=str(args['repo_cwd']),
        branch=str(args['branch']),
        base=str(args.get('base') or 'main'),
    )
    return {'content': [{'type': 'text', 'text': json.dumps(result, indent=2)}]}


def build_agent_local_server() -> McpSdkServerConfig:
    """Build the in-process ``leartech-agent-local`` MCP server.

    Exposes the two tools that couldn't move to the remote tekton MCP because
    they depend on agent-pod-local state — the LLM classifier's heuristic
    tables and the git working tree of the cloned consumer repo. See the
    module docstring for the boundary reasoning.
    """
    return create_sdk_mcp_server(
        name='leartech-agent-local',
        version='0.1.0',
        tools=[
            _classify_step_failure_tool,
            _rebase_branch_on_base,
        ],
    )
