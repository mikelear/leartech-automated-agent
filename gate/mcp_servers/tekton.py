"""leartech-tekton-mcp — step-aware Tekton PipelineRun inspection via kubectl.

The pre-existing `leartech-pipeline` MCP exposes only the *aggregate* GitHub-side
status (e.g. ``lint: failure``). That's enough to know *something* went wrong but
not WHAT — was it git-clone, ruff, mypy, pytest, kaniko? Today the agent has to
shell out to ``~/leartech/Hub/scripts/pr-pipelines.sh`` to drill in, which binds
it to a laptop layout and burns GraphQL quota.

This MCP layer goes straight to the cluster via kubectl. It uses the
Lighthouse-attached labels:

  - ``lighthouse.jenkins-x.io/refs.repo=<owner/name>``
  - ``lighthouse.jenkins-x.io/refs.pull=<N>``
  - ``lighthouse.jenkins-x.io/lastCommitSHA=<sha>``

to filter PipelineRuns, then walks the embedded TaskRun → pod → step structure
to surface per-step state + stderr. Nothing in this MCP touches GitHub at all,
so it does NOT consume the operator's GraphQL bucket.

## Cluster contexts

  - ``az``  → ``modern-burro``
  - ``gcp`` → ``gke_product-first_us-east1-b_tf-jx-usable-bird``

These match `scripts/watch_pr_pipelineruns.sh`. Extend `_CLUSTER_CONTEXTS` when
a new cluster comes online.

## Scope

This is Phase G.1. It ships:

  - ``list_pipelineruns_for_pr``  — discover PRs' PipelineRuns
  - ``step_status``               — per-step verdict on one PipelineRun
  - ``step_logs``                 — stderr tail for one step
  - ``cancel_pipelinerun``        — terminate one PipelineRun
  - ``cancel_superseded_for_pr``  — bulk-cancel all but the latest SHA
  - ``wait_first_failure``        — block until any step fails OR all succeed

WIRING these into the agent loop is OUT OF SCOPE — that's
``agent-wire-tekton-into-loop`` (Phase G.2).
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.agent.step_failure_diagnosis import (
    classify_step_failure as _classify_step_failure,
)

# ─── Cluster mapping ──────────────────────────────────────────────────────────

_CLUSTER_CONTEXTS: dict[str, str] = {
    'az': 'modern-burro',
    'gcp': 'gke_product-first_us-east1-b_tf-jx-usable-bird',
}

_NAMESPACE = 'jx'

# Tekton terminal-condition reasons (cf. tektoncd/pipeline docs).
_TERMINAL_SUCCESS = {'Succeeded', 'Completed'}
_TERMINAL_FAILURE = {'Failed', 'PipelineRunCancelled', 'Cancelled', 'PipelineRunTimeout', 'TaskRunTimeout'}


def _context_for(cluster: str) -> str:
    """Resolve a short cluster name (``az``/``gcp``) to its kubectl context."""
    ctx = _CLUSTER_CONTEXTS.get(cluster)
    if not ctx:
        available = ', '.join(sorted(_CLUSTER_CONTEXTS))
        raise ValueError(f'Unknown cluster {cluster!r}. Available: {available}')
    return ctx


# ─── kubectl shellout — single seam for tests to mock ─────────────────────────


@dataclass
class _KubectlResult:
    returncode: int
    stdout: str
    stderr: str


def _run_kubectl(args: list[str], cluster: str, timeout: int = 30) -> _KubectlResult:
    """Invoke ``kubectl --context=<ctx> -n jx <args...>`` and return the result.

    Single seam: tests monkeypatch this function. Production callers go via the
    higher-level helpers below so the seam stays small and explicit.
    """
    ctx = _context_for(cluster)
    cmd = ['kubectl', f'--context={ctx}', '-n', _NAMESPACE, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    return _KubectlResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _qualified_repo(repo: str) -> str:
    """``leartech-automated-agent`` → ``mikelear/leartech-automated-agent``."""
    return repo if '/' in repo else f'mikelear/{repo}'


def _selector_for_pr(repo: str, pr_number: int) -> str:
    return f'lighthouse.jenkins-x.io/refs.repo={_qualified_repo(repo)},lighthouse.jenkins-x.io/refs.pull={pr_number}'


def _parse_pipelinerun_list(stdout: str) -> list[dict[str, Any]]:
    """Convert ``kubectl get pr -o json`` stdout into a list of summary dicts.

    Each entry: name, status (Succeeded|Failed|Running|Pending|Cancelled),
    reason, sha, started_at, completed_at, pipeline (the source PipelineRun name).
    Empty stdout → empty list (matches the case where no PipelineRuns exist yet).
    """
    text = (stdout or '').strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    items = doc.get('items', []) if isinstance(doc, dict) else []
    rows: list[dict[str, Any]] = []
    for item in items:
        meta = item.get('metadata') or {}
        labels = meta.get('labels') or {}
        status = item.get('status') or {}
        conditions = status.get('conditions') or []
        cond = conditions[0] if conditions else {}
        cond_status = cond.get('status', 'Unknown')
        reason = cond.get('reason', '')
        if cond_status == 'True':
            verdict = 'Succeeded'
        elif cond_status == 'False':
            # Failed / Cancelled / Timeout disambiguated by reason.
            verdict = reason or 'Failed'
        elif cond_status == 'Unknown':
            verdict = reason or 'Running'
        else:
            verdict = 'Pending'
        rows.append(
            {
                'name': meta.get('name', ''),
                'status': verdict,
                'reason': reason,
                'sha': labels.get('lighthouse.jenkins-x.io/lastCommitSHA', ''),
                'check': labels.get('lighthouse.jenkins-x.io/context', '') or labels.get('prow.k8s.io/context', ''),
                'started_at': status.get('startTime', ''),
                'completed_at': status.get('completionTime', ''),
                'pipeline': labels.get('tekton.dev/pipeline', ''),
            }
        )
    return rows


def _parse_step_status(stdout: str) -> list[dict[str, Any]]:
    """Walk a PipelineRun's child TaskRuns and surface per-step verdicts.

    Each entry: task (task name), step (step name), state (Succeeded|Failed|Running|Pending),
    reason, exit_code, pod (TaskRun pod name — needed by step_logs).
    """
    text = (stdout or '').strip()
    if not text:
        return []
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return []
    taskruns = (doc.get('status') or {}).get('childReferences') or []
    # Real shape varies by Tekton version: older versions inline `taskRuns` as a
    # map; v0.40+ uses `childReferences` + separate TaskRun fetch. We support
    # the inline form here because step-level state is what we actually need,
    # and tests can stub either shape.
    inline = (doc.get('status') or {}).get('taskRuns')
    if isinstance(inline, dict) and inline:
        rows: list[dict[str, Any]] = []
        for tr_name, tr in inline.items():
            task = tr.get('pipelineTaskName', tr_name)
            tr_status = tr.get('status') or {}
            pod = tr_status.get('podName', '')
            for step in tr_status.get('steps') or []:
                name = step.get('name', '')
                state, reason, exit_code = _summarise_step_state(step)
                rows.append(
                    {
                        'task': task,
                        'step': name,
                        'state': state,
                        'reason': reason,
                        'exit_code': exit_code,
                        'pod': pod,
                    }
                )
        return rows

    # Fall-through for childReferences-only shape: we can at least name the
    # tasks even if step detail isn't inline. Step-level visibility for this
    # shape requires a second `kubectl get taskrun` round-trip which we defer
    # to Phase G.2's loop wiring.
    return [
        {
            'task': c.get('pipelineTaskName', c.get('name', '')),
            'step': '',
            'state': 'Unknown',
            'reason': 'childReferences-only (separate TaskRun fetch needed)',
            'exit_code': None,
            'pod': '',
        }
        for c in taskruns
    ]


def _summarise_step_state(step: dict[str, Any]) -> tuple[str, str, int | None]:
    """Reduce a Tekton step's container-status to (state, reason, exit_code).

    Tekton serialises running/waiting containers as empty objects (``{}``) — so
    we check for the *key* being present, not the value's truthiness.
    """
    if 'terminated' in step:
        terminated = step['terminated'] or {}
        exit_code = terminated.get('exitCode')
        reason = terminated.get('reason', '')
        if exit_code == 0:
            return 'Succeeded', reason or 'Completed', 0
        return 'Failed', reason or 'NonZeroExit', exit_code
    if 'running' in step:
        return 'Running', '', None
    if 'waiting' in step:
        waiting = step['waiting'] or {}
        return 'Pending', waiting.get('reason', 'Waiting'), None
    return 'Pending', 'Unknown', None


# ─── Tool implementations ─────────────────────────────────────────────────────


def list_pipelineruns_for_pr(repo: str, pr_number: int, cluster: str = 'az') -> list[dict[str, Any]]:
    """Discover Tekton PipelineRuns for a PR on one cluster.

    Returns a list ordered by start time (newest first). Empty list when the PR
    has not produced any PipelineRuns yet, or when the labels haven't been
    attached (very fresh PR).
    """
    selector = _selector_for_pr(repo, pr_number)
    result = _run_kubectl(['get', 'pipelinerun', '-l', selector, '-o', 'json'], cluster)
    if result.returncode != 0:
        return []
    rows = _parse_pipelinerun_list(result.stdout)
    # Newest-first ordering: started_at descending (lexicographic ISO8601 sort works).
    rows.sort(key=lambda r: r.get('started_at') or '', reverse=True)
    return rows


def step_status(pipelinerun_name: str, cluster: str) -> list[dict[str, Any]]:
    """Per-step verdict on one PipelineRun. Empty list if the run is unknown."""
    result = _run_kubectl(['get', 'pipelinerun', pipelinerun_name, '-o', 'json'], cluster)
    if result.returncode != 0:
        return []
    return _parse_step_status(result.stdout)


def step_logs(pipelinerun_name: str, step_name: str, cluster: str, tail: int = 200) -> str:
    """Return the last `tail` lines of stderr+stdout for one step.

    Walks step_status to find the TaskRun pod owning that step, then runs
    ``kubectl logs <pod> -c step-<name> --tail=<n>``. Returns an empty string
    when the pod can't be located (TaskRun cleaned up, or wrong name).
    """
    statuses = step_status(pipelinerun_name, cluster)
    target = next((s for s in statuses if s['step'] == step_name and s.get('pod')), None)
    if not target:
        return ''
    pod = target['pod']
    container = f'step-{step_name}'
    result = _run_kubectl(['logs', pod, '-c', container, f'--tail={tail}'], cluster)
    if result.returncode != 0:
        return ''
    return result.stdout


def cancel_pipelinerun(pipelinerun_name: str, cluster: str) -> bool:
    """Mark a PipelineRun cancelled via merge-patch. Returns True on success."""
    patch_body = json.dumps({'spec': {'status': 'PipelineRunCancelled'}})
    result = _run_kubectl(
        ['patch', 'pipelinerun', pipelinerun_name, '--type=merge', '-p', patch_body],
        cluster,
    )
    return result.returncode == 0


def cancel_superseded_for_pr(
    repo: str,
    pr_number: int,
    keep_sha: str,
    cluster: str,
) -> int:
    """Cancel every PipelineRun on this PR whose lastCommitSHA != keep_sha.

    Returns the count of PipelineRuns successfully cancelled. Runs that are
    already terminal (Succeeded/Failed/Cancelled) are skipped — only in-flight
    runs are patched, which avoids redundant API churn.
    """
    runs = list_pipelineruns_for_pr(repo, pr_number, cluster)
    cancelled = 0
    terminal_states = _TERMINAL_SUCCESS | _TERMINAL_FAILURE
    for run in runs:
        if run.get('sha') == keep_sha:
            continue
        if run.get('status') in terminal_states:
            continue
        if cancel_pipelinerun(run['name'], cluster):
            cancelled += 1
    return cancelled


def wait_first_failure(
    repo: str,
    pr_number: int,
    cluster: str,
    timeout_s: int = 1800,
    poll_seconds: int = 15,
) -> dict[str, Any]:
    """Block until any PipelineRun reports a failed step OR all runs succeed.

    Returns a dict ``{"status": "first_failure"|"all_passed"|"timeout",
    "first_failure": {pipelinerun, task, step, reason, exit_code} | None,
    "runs": [...]}``. The fail-fast counterpart to
    ``leartech-pipeline.wait_for_terminal``, but step-aware: surfaces a failed
    `ruff` step within one poll, even while later tasks are still running.
    """
    deadline = time.monotonic() + max(1, int(timeout_s))
    poll = max(5, int(poll_seconds))
    status = 'timeout'
    first_failure: dict[str, Any] | None = None
    runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        runs = list_pipelineruns_for_pr(repo, pr_number, cluster)
        if not runs:
            time.sleep(poll)
            continue
        # Any step in any run that ended with non-zero exit-code wins.
        for run in runs:
            steps = step_status(run['name'], cluster)
            failed = next((s for s in steps if s['state'] == 'Failed'), None)
            if failed:
                first_failure = {
                    'pipelinerun': run['name'],
                    'task': failed['task'],
                    'step': failed['step'],
                    'reason': failed['reason'],
                    'exit_code': failed['exit_code'],
                }
                status = 'first_failure'
                break
        if first_failure is not None:
            break
        if all(r['status'] in _TERMINAL_SUCCESS for r in runs):
            status = 'all_passed'
            break
        time.sleep(poll)
    return {'status': status, 'first_failure': first_failure, 'runs': runs}


# ─── MCP tool wrappers ────────────────────────────────────────────────────────


@tool(
    'list_pipelineruns_for_pr',
    'Discover Tekton PipelineRuns attached to a GitHub PR via Lighthouse labels. '
    'Returns newest-first: [{name, status (Succeeded|Failed|Running|Pending|Cancelled), '
    'reason, sha, check, started_at, completed_at, pipeline}]. Cluster: az|gcp.',
    {'repo': str, 'pr_number': int, 'cluster': str},
)
async def _list_pipelineruns_for_pr(args: dict[str, Any]) -> dict[str, Any]:
    cluster = str(args.get('cluster') or 'az')
    runs = list_pipelineruns_for_pr(str(args['repo']), int(args['pr_number']), cluster)
    return {'content': [{'type': 'text', 'text': json.dumps(runs, indent=2)}]}


@tool(
    'step_status',
    'Walk a PipelineRun and return per-step verdicts: '
    '[{task, step, state (Succeeded|Failed|Running|Pending), reason, exit_code, pod}]. '
    'This is what `leartech-pipeline.list_pr_checks` cannot tell you — the agent '
    'now knows WHICH step failed (git-clone? ruff? pytest?), not just that the '
    'whole pipeline failed. Cluster: az|gcp.',
    {'pipelinerun_name': str, 'cluster': str},
)
async def _step_status(args: dict[str, Any]) -> dict[str, Any]:
    statuses = step_status(str(args['pipelinerun_name']), str(args['cluster']))
    return {'content': [{'type': 'text', 'text': json.dumps(statuses, indent=2)}]}


@tool(
    'step_logs',
    'Tail `tail` lines of stdout+stderr from one step of one PipelineRun. '
    'Looks up the TaskRun pod, then runs `kubectl logs <pod> -c step-<name>`. '
    "Returns an empty string when the pod has been GC'd (run completed too long ago) "
    'or the step name is wrong. Default tail=200. Cluster: az|gcp.',
    {'pipelinerun_name': str, 'step_name': str, 'cluster': str, 'tail': int},
)
async def _step_logs(args: dict[str, Any]) -> dict[str, Any]:
    tail = int(args.get('tail') or 200)
    text = step_logs(
        str(args['pipelinerun_name']),
        str(args['step_name']),
        str(args['cluster']),
        tail=tail,
    )
    return {'content': [{'type': 'text', 'text': text}]}


@tool(
    'cancel_pipelinerun',
    'Cancel one PipelineRun via `kubectl patch --type=merge -p {"spec":{"status":"PipelineRunCancelled"}}`. '
    'Returns {"cancelled": true|false}. Use this when a force-push supersedes an in-flight run '
    'and waiting it out would burn cluster CPU + slow the next cycle.',
    {'pipelinerun_name': str, 'cluster': str},
)
async def _cancel_pipelinerun(args: dict[str, Any]) -> dict[str, Any]:
    ok = cancel_pipelinerun(str(args['pipelinerun_name']), str(args['cluster']))
    return {'content': [{'type': 'text', 'text': json.dumps({'cancelled': ok})}]}


@tool(
    'cancel_superseded_for_pr',
    'Cancel every in-flight PipelineRun on this PR whose lastCommitSHA differs from `keep_sha`. '
    'Use after a force-push: the new push spawns fresh runs, the old ones are wasted cycles. '
    'Returns {"cancelled_count": N}. Already-terminal runs are skipped.',
    {'repo': str, 'pr_number': int, 'keep_sha': str, 'cluster': str},
)
async def _cancel_superseded_for_pr(args: dict[str, Any]) -> dict[str, Any]:
    n = cancel_superseded_for_pr(
        str(args['repo']),
        int(args['pr_number']),
        str(args['keep_sha']),
        str(args['cluster']),
    )
    return {'content': [{'type': 'text', 'text': json.dumps({'cancelled_count': n})}]}


@tool(
    'wait_first_failure',
    'Step-aware fail-fast wait: returns within one poll of any step failing in any PipelineRun on '
    'the PR (e.g. a `ruff` failure surfaces in ~15s even while pytest is still running). '
    'Returns {"status": "first_failure"|"all_passed"|"timeout", "first_failure": {pipelinerun, '
    'task, step, reason, exit_code} | None, "runs": [...]}. Default timeout=1800s, poll=15s.',
    {'repo': str, 'pr_number': int, 'cluster': str, 'timeout_s': int, 'poll_seconds': int},
)
async def _wait_first_failure(args: dict[str, Any]) -> dict[str, Any]:
    payload = wait_first_failure(
        str(args['repo']),
        int(args['pr_number']),
        str(args['cluster']),
        timeout_s=int(args.get('timeout_s') or 1800),
        poll_seconds=int(args.get('poll_seconds') or 15),
    )
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


# ─── G.2 — classification + rebase helpers ────────────────────────────────────


@tool(
    'classify_step_failure',
    'Diagnose ONE failed Tekton step. Inputs: step_name (e.g. "ruff", "git-clone", "pytest") '
    'and log_tail (the last ~200 lines from step_logs). '
    'Returns {"classification": "git_merge_conflict|ruff_format_error|ruff_lint_error|mypy_type_error|'
    'pytest_test_failure|kaniko_build_failure|image_pull_backoff|ai_review_red_finding|tekton_step_oom|'
    'tekton_step_timeout|preview_deploy_failure|security_scan_finding|unknown", '
    '"action": "rebase|fix_code|fix_test|retry|escalate", "pipelinerun": str, "step_name": str}. '
    'Use this AFTER step_logs so the agent dispatches on the canonical failure shape '
    'rather than retrying blindly. Unknown failure → action=escalate (do NOT retry).',
    {'step_name': str, 'log_tail': str, 'pipelinerun': str},
)
async def _classify_step_failure_tool(args: dict[str, Any]) -> dict[str, Any]:
    failure = _classify_step_failure(
        step_name=str(args['step_name']),
        log_tail=str(args['log_tail']),
        pipelinerun=str(args.get('pipelinerun') or ''),
    )
    return {'content': [{'type': 'text', 'text': json.dumps(failure.to_dict(), indent=2)}]}


def _run_git(args: list[str], cwd: str, timeout: int = 60) -> _KubectlResult:
    """Run a git command in `cwd`. Single seam — tests monkeypatch this.

    Re-uses ``_KubectlResult`` for the (returncode, stdout, stderr) shape;
    nothing about that struct is kubectl-specific in practice.
    """
    proc = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)
    return _KubectlResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def rebase_branch_on_base(repo_cwd: str, branch: str, base: str = 'main') -> dict[str, Any]:
    """Rebase ``branch`` onto ``origin/<base>`` and force-push-with-lease.

    Strategy: ``git fetch origin <base>`` → ``git rebase -Xtheirs origin/<base>``.
    The ``-Xtheirs`` flag asks git to auto-resolve content conflicts in favour
    of the rebased branch (the agent's own commits), which is the correct
    choice in 99% of cases: the agent's PR is the source of truth, main has
    only moved forward independently.

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

    Replaces the standalone `self-rebase-on-conflict` initiative (deferred
    in Phase G planning); kept as a Python helper so the agent can invoke
    it via one MCP call rather than orchestrating four git commands.
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


def build_tekton_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-tekton',
        version='0.2.0',
        tools=[
            _list_pipelineruns_for_pr,
            _step_status,
            _step_logs,
            _cancel_pipelinerun,
            _cancel_superseded_for_pr,
            _wait_first_failure,
            _classify_step_failure_tool,
            _rebase_branch_on_base,
        ],
    )
