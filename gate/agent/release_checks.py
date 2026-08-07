"""Thin deterministic release-verify checks — call ONE Go MCP tool, decide on its typed verdict.

No LLM turn-loop, no ``STAGE_STATUS`` transcription. Each infra ``action`` here maps to exactly
one Go MCP tool; the tool's *structured* verdict (not model narration) decides PASS/FAIL and the
process exit code. This is the runtime side of the ``verify-release-flow`` PlanTemplate
(leartech-plan-catalog): the DAG composes these checks, so each step is a pure Go-tool relay that
is independently re-runnable. Contrast ``gate/agent/release_health.py``, where the LLM emitted
STAGE_STATUS lines that Python regex-parsed — that indirection is what this module removes.

Input contract (consolidated — the duplication the STAGE_STATUS actions carried is gone):
  * ``clusters: [gcp, az]``  ONE list. No ``cluster`` singular alias.
  * ``version: <tag>``       ONE optional field (deploy-health). No ``expectedVersion`` alias.

Cluster routing: ``promote_status`` (jx_release) is natively cross-cluster (GitHub API), so it
takes the clusters list and returns per-cluster in a single call. The k8s/tekton tools
(``release_pipeline_status``, ``deploy_health``, ``bootjob_for_commit``) read the cluster their
MCP host is deployed in, so each cluster is reached through its OWN endpoint:
``LEARTECH_MCP_URL_<CLUSTER>`` (e.g. ``LEARTECH_MCP_URL_GCP``), falling back to
``LEARTECH_MCP_URL``. When two requested clusters resolve to the SAME endpoint (per-cluster URLs
not yet wired — the 'nominal routing' gap), the duplicate is reported as SKIP rather than
silently probing one cluster twice and counting it as two passes.

Step-7 semantics (locked): a boot can APPLY a commit (deploy correct) yet the boot Job later
fail on post-apply housekeeping. ``deploy_health`` (deploy-health) is the authoritative "it
landed" signal; ``bootjob-for-commit`` confirms a boot RAN — a boot that reached a terminal
FAILED state is still a PASS here, because boot-job terminal health is the observer/Alert job's
concern, not a release false-FAIL.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from gate import obslog
from gate.mcp_servers.remote import discover_mounts, mint_mcp_token
from gate.mcp_servers.stdio_bridge import _with_downstream

DEFAULT_CLUSTERS: tuple[str, ...] = ('gcp', 'az')
_CALL_TIMEOUT = 120.0

# The check actions this module owns — the no-LLM deterministic path. Two of these
# names (promote-status, deploy-health) intentionally shadow the legacy STAGE_STATUS
# individual actions: the deterministic path replaces the LLM-transcription one.
CHECK_ACTIONS: frozenset[str] = frozenset(
    {'release-pipeline-status', 'promote-status', 'deploy-health', 'bootjob-for-commit'}
)


def is_check_action(action: str) -> bool:
    """True when ``action`` is a thin deterministic check (routed to the no-LLM path)."""
    return action in CHECK_ACTIONS


# ── result types ──────────────────────────────────────────────────────────────
@dataclass
class ClusterVerdict:
    """One cluster's outcome for a check. ``verdict`` is PASS | FAIL | SKIP."""

    cluster: str
    verdict: str
    reason: str
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {'cluster': self.cluster, 'verdict': self.verdict, 'reason': self.reason}


@dataclass
class CheckResult:
    """The aggregate check verdict. ``verdict`` is PASS | FAIL (drives the exit code)."""

    action: str
    tool: str
    verdict: str
    reason: str
    clusters: list[ClusterVerdict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            'action': self.action,
            'tool': self.tool,
            'verdict': self.verdict,
            'reason': self.reason,
            'clusters': [c.as_dict() for c in self.clusters],
        }


# ── MCP tool caller (seam: tests inject a fake, no network) ─────────────────────
# ToolCaller(base_url, server, tool, args) -> (structured_result, error_or_None)
ToolCaller = Callable[
    [str, str, str, dict[str, Any]], Awaitable[tuple[dict[str, Any], "str | None"]]
]


def _content_text(result: Any) -> str:
    """Concatenate the text of a CallToolResult's content blocks."""
    parts: list[str] = []
    for block in getattr(result, 'content', None) or []:
        text = getattr(block, 'text', None)
        if isinstance(text, str):
            parts.append(text)
    return '\n'.join(parts)


async def _default_tool_caller(
    base_url: str, server: str, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Call one Go MCP tool over the authed streamable-HTTP transport and return its
    structured result (``structuredContent``, else JSON in the text content). Any failure
    is returned as ``(_, error)`` — never raised — so a check FAILs cleanly instead of
    crashing. Reuses the bridge's fresh-token/fresh-connection-per-op primitive."""
    base = base_url.rstrip('/')
    if not base:
        return {}, 'no MCP base URL (LEARTECH_MCP_URL[_<CLUSTER>] unset)'
    token = mint_mcp_token()
    if not token:
        return {}, 'could not mint aud=leartech-mcp token (check LEARTECH_AUTH_* env)'
    mounts = discover_mounts(base, token)
    if not mounts:
        return {}, f'MCP discovery failed against {base}'
    path = mounts.get(server)
    if not path:
        return {}, f'MCP host {base} does not advertise server {server!r}'
    try:
        result = await _with_downstream(
            f'{base}{path}', _CALL_TIMEOUT, lambda s: s.call_tool(tool, args)
        )
    except Exception as exc:  # noqa: BLE001 — surface as a clean check error, never hang/crash
        return {}, f'{type(exc).__name__}: {exc}'
    if getattr(result, 'isError', False):
        return {}, _content_text(result) or 'tool returned isError'
    structured = getattr(result, 'structuredContent', None)
    if isinstance(structured, dict) and structured:
        return structured, None
    text = _content_text(result)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}, f'tool returned no structured content: {text[:200]}'
    return (parsed if isinstance(parsed, dict) else {'result': parsed}), None


# ── input + endpoint resolution ────────────────────────────────────────────────
def _resolve_clusters(inputs: dict[str, object]) -> tuple[str, ...]:
    """The requested cluster set — the ONE ``clusters`` list (no ``cluster`` alias)."""
    raw = inputs.get('clusters')
    if isinstance(raw, list) and raw:
        out = tuple(str(c).strip().lower() for c in raw if str(c).strip())
        if out:
            return out
    return DEFAULT_CLUSTERS


def _endpoint_for(cluster: str) -> str:
    """Per-cluster MCP endpoint: ``LEARTECH_MCP_URL_<CLUSTER>`` else ``LEARTECH_MCP_URL``."""
    per = os.environ.get(f'LEARTECH_MCP_URL_{cluster.upper()}')
    return (per or os.environ.get('LEARTECH_MCP_URL', '')).rstrip('/')


def _req(inputs: dict[str, object], key: str) -> str:
    """Fetch a required string input or raise ValueError (→ deterministic FAIL)."""
    val = inputs.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f'missing required input {key!r}')
    return val.strip()


def _version(inputs: dict[str, object]) -> str:
    """The ONE ``version`` field (no ``expectedVersion`` alias); '' when unset."""
    val = inputs.get('version')
    return val.strip() if isinstance(val, str) and val.strip() else ''


def _fail_reason(cvs: list[ClusterVerdict]) -> str:
    fails = [f'{v.cluster}: {v.reason}' for v in cvs if v.verdict == 'FAIL']
    skips = [f'{v.cluster}: {v.reason}' for v in cvs if v.verdict == 'SKIP']
    parts = fails or ['no cluster probed']
    if skips:
        parts.append('(skipped — ' + '; '.join(skips) + ')')
    return '; '.join(parts)


# ── per-tool verdicts (pure: structured dict -> (ok, reason)) ───────────────────
def _verdict_release_pipeline(s: dict[str, Any]) -> tuple[bool, str]:
    run = s.get('run') or {}
    name = run.get('name', '') if isinstance(run, dict) else ''
    label = name or 'run'
    if not s.get('fired'):
        return False, 'release pipeline did not fire for this commit (no run matched lastCommitSHA)'
    if s.get('failed'):
        return False, f'release pipeline FAILED ({label})'
    if s.get('passed'):
        return True, f'release pipeline passed ({label})'
    return False, f'release pipeline still running ({label})'


def _verdict_deploy_health(expected: str) -> Callable[[dict[str, Any]], tuple[bool, str]]:
    def _v(s: dict[str, Any]) -> tuple[bool, str]:
        avail = s.get('available_replicas')
        obs = s.get('observed_version', '')
        reason = s.get('reason', '')
        if not s.get('healthy'):
            return False, f'deploy unhealthy (available={avail}) {reason}'.strip()
        if expected and s.get('version_match') is False:
            return False, f'version mismatch: running {obs or "?"}, expected {expected}'
        return True, f'healthy available={avail} version={obs or "?"}'

    return _v


def _verdict_bootjob(s: dict[str, Any]) -> tuple[bool, str]:
    job = s.get('job_name', '')
    if not s.get('found'):
        return False, 'no jx-boot Job found reconciling this release commit'
    if s.get('running'):
        return False, f'jx-boot Job {job} still running'
    if s.get('failed'):
        # Locked step-7 semantics: a boot that RAN is a PASS even if the Job ended FAILED
        # (post-apply housekeeping); deploy-health is the authoritative 'landed' gate.
        return True, (
            f'jx-boot Job {job} ran (terminal FAILED — post-apply housekeeping; '
            'deploy-health gates landed)'
        )
    return True, f'jx-boot Job {job} completed'


# ── check runners ──────────────────────────────────────────────────────────────
async def _run_per_cluster(
    action: str,
    server: str,
    tool: str,
    clusters: tuple[str, ...],
    caller: ToolCaller,
    *,
    build_args: Callable[[str], dict[str, Any]],
    verdict: Callable[[dict[str, Any]], tuple[bool, str]],
) -> CheckResult:
    """Run a cluster-local tool once per DISTINCT endpoint, aggregate PASS iff every
    probed cluster passed. Clusters sharing an endpoint (per-cluster URLs unwired) are
    SKIPped, not double-probed."""
    cvs: list[ClusterVerdict] = []
    endpoint_owner: dict[str, str] = {}
    for cl in clusters:
        base = _endpoint_for(cl)
        if not base:
            cvs.append(ClusterVerdict(cl, 'FAIL', 'no MCP endpoint (LEARTECH_MCP_URL[_<CLUSTER>] unset)'))
            continue
        if base in endpoint_owner:
            cvs.append(
                ClusterVerdict(
                    cl,
                    'SKIP',
                    f'shares endpoint with {endpoint_owner[base]} — set LEARTECH_MCP_URL_{cl.upper()} '
                    'for a distinct per-cluster probe',
                )
            )
            continue
        endpoint_owner[base] = cl
        structured, err = await caller(base, server, tool, build_args(cl))
        if err:
            cvs.append(ClusterVerdict(cl, 'FAIL', f'{tool} call failed: {err}'))
            continue
        ok, reason = verdict(structured)
        cvs.append(ClusterVerdict(cl, 'PASS' if ok else 'FAIL', reason, structured))
    probed = [v for v in cvs if v.verdict in ('PASS', 'FAIL')]
    ok = bool(probed) and all(v.verdict == 'PASS' for v in probed)
    reason = 'all probed clusters passed' if ok else _fail_reason(cvs)
    return CheckResult(action, tool, 'PASS' if ok else 'FAIL', reason, cvs)


async def _run_promote_status(
    inputs: dict[str, object], clusters: tuple[str, ...], caller: ToolCaller
) -> CheckResult:
    """promote_status is natively cross-cluster (GitHub API) — one call, clusters list."""
    service = _req(inputs, 'service')
    base = _endpoint_for(clusters[0]) or os.environ.get('LEARTECH_MCP_URL', '').rstrip('/')
    structured, err = await caller(base, 'jx_release', 'promote_status', {'service': service, 'clusters': list(clusters)})
    if err:
        return CheckResult('promote-status', 'promote_status', 'FAIL', f'promote_status call failed: {err}')
    cvs: list[ClusterVerdict] = []
    for c in structured.get('clusters') or []:
        cl = str(c.get('cluster', '?'))
        prn = c.get('pr_number')
        if c.get('gate_failed'):
            cvs.append(ClusterVerdict(cl, 'FAIL', f'qa-gate FAILED on promote PR #{prn} (cross-plan hand-off)', c))
        elif c.get('merged'):
            cvs.append(ClusterVerdict(cl, 'PASS', f'promote PR #{prn} merged', c))
        elif not c.get('found'):
            cvs.append(ClusterVerdict(cl, 'FAIL', 'no promote PR opened yet', c))
        elif c.get('all_green'):
            cvs.append(ClusterVerdict(cl, 'FAIL', f'promote PR #{prn} green but not merged yet', c))
        else:
            cvs.append(ClusterVerdict(cl, 'FAIL', f'promote PR #{prn} checks pending/red', c))
    ok = bool(structured.get('all_merged')) and bool(cvs) and all(v.verdict == 'PASS' for v in cvs)
    reason = 'promote opened + green + merged on all clusters' if ok else _fail_reason(cvs)
    return CheckResult('promote-status', 'promote_status', 'PASS' if ok else 'FAIL', reason, cvs)


async def run_check(
    action: str, inputs: dict[str, object], *, caller: ToolCaller = _default_tool_caller
) -> CheckResult:
    """Dispatch one check action to its Go MCP tool and return the typed verdict."""
    clusters = _resolve_clusters(inputs)

    if action == 'promote-status':
        return await _run_promote_status(inputs, clusters, caller)

    if action == 'release-pipeline-status':
        repo, sha = _req(inputs, 'repo'), _req(inputs, 'sha')
        return await _run_per_cluster(
            action, 'tekton', 'release_pipeline_status', clusters, caller,
            build_args=lambda _c: {'repo': repo, 'sha': sha},
            verdict=_verdict_release_pipeline,
        )

    if action == 'deploy-health':
        service = _req(inputs, 'service')
        namespace = str(inputs.get('namespace') or 'jx-staging')
        version = _version(inputs)

        def _deploy_args(cl: str) -> dict[str, Any]:
            args: dict[str, Any] = {'service': service, 'namespace': namespace, 'cluster': cl}
            if version:
                args['expected_version'] = version
            return args

        return await _run_per_cluster(
            action, 'k8s', 'deploy_health', clusters, caller,
            build_args=_deploy_args, verdict=_verdict_deploy_health(version),
        )

    if action == 'bootjob-for-commit':
        service = _req(inputs, 'service')  # noqa: F841 — required-input assertion (used by the template contract)
        namespace = str(inputs.get('namespace') or 'jx-git-operator')
        # sha correlation is best-effort: the boot commit-sha annotation is unreliable
        # (names only the tip commit) — the #59 follow-up matches commit-message/PR#.
        sha = str(inputs.get('sha') or '')
        repo_url = str(inputs.get('repo') or '')

        def _boot_args(cl: str) -> dict[str, Any]:
            args: dict[str, Any] = {'namespace': namespace, 'cluster': cl}
            if sha:
                args['sha'] = sha
            if repo_url:
                args['repo_url'] = repo_url
            return args

        return await _run_per_cluster(
            action, 'k8s', 'bootjob_for_commit', clusters, caller,
            build_args=_boot_args, verdict=_verdict_bootjob,
        )

    return CheckResult(action, '', 'FAIL', f'unknown check action {action!r}')


async def run_check_action(
    action: str, inputs: dict[str, object], *, caller: ToolCaller = _default_tool_caller
) -> int:
    """Entry point for the infra agent's no-LLM check path. Runs the check, emits one
    structured ``check_verdict`` log line, and returns the process exit code
    (PASS → 0, else 1 — fail-closed)."""
    obslog.info('run_start', f'infra check action={action}', logger='infra', action=action, check=True)
    try:
        result = await run_check(action, inputs, caller=caller)
    except ValueError as exc:  # missing/invalid required input → deterministic FAIL
        obslog.error(
            'check_verdict', f'{action} FAIL: {exc}', logger='infra',
            action=action, verdict='FAIL', reason=str(exc), exit_code=1,
        )
        obslog.info('run_end', f'infra check action={action} done', logger='infra', action=action, exit_code=1)
        return 1
    exit_code = 0 if result.verdict == 'PASS' else 1
    obslog.info(
        'check_verdict', f'{action} verdict={result.verdict}: {result.reason}', logger='infra',
        action=action, tool=result.tool, verdict=result.verdict, reason=result.reason,
        clusters=[c.as_dict() for c in result.clusters], exit_code=exit_code,
    )
    print(f'{action}: {result.verdict} — {result.reason}')
    obslog.info('run_end', f'infra check action={action} done', logger='infra', action=action, exit_code=exit_code)
    return exit_code
