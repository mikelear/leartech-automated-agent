"""Deterministic verdict aggregator for the infra release-health-check action
and the FIVE individual single-stage actions that decompose it.

Root cause this closes (verified live 2026-07-30): the historical stage-4
"confirm the deploy is healthy" step relied on an httpx GET against the
ingress /health URL from the infra sandbox. The sandbox cannot reach the
ingress (no cluster DNS, no in-cluster routing), so the probe returned
transport errors even when the deploy was perfectly healthy — the same run
would FAIL on GCP and PASS on AZ depending on which cluster's ingress the
sandbox happened to be closest to. Spurious FAILs → spurious BA
escalations → alert fatigue → real incidents missed.

The fix — driven by the initiative wiring the ``leartech-k8s`` MCP
(``deploy_health``, ``get_job_state``, ``list_jobs_by_label``) into the
infra agent — makes stage 4 an **in-cluster** MCP call that reports
``healthy=true`` iff the Deployment has >=1 available replica. No ingress,
no /health HTTP GET, no kubectl. This module CODIFIES how the LLM's stage
transcript is parsed into a verdict:

* the LLM drives stages 1..4 via the ``jx_release`` / ``tekton`` / ``k8s``
  MCPs (see the ``INFRA_SYSTEM_PROMPT`` in :mod:`gate.agent.infra_agent`);
* after each stage per cluster, it emits ONE machine-readable line:

      STAGE_STATUS: stage=<n> cluster=<gcp|az|-> verdict=<PASS|FAIL|SKIP> reason=<one-line>

  where ``cluster=-`` means the stage is not cluster-scoped (e.g. stage 1
  release-fired is per repo, not per cluster);
* this module parses those lines from the transcript and computes the final
  verdict — PASS iff every required (stage, cluster) pair reports PASS,
  FAIL otherwise, naming the first failing (stage, cluster) with its reason;
* the LLM MAY ALSO emit an early-exit ``RELEASE_HEALTH: FAIL: <reason>``
  when a stage produces a signal it cannot proceed past (release did not
  fire within budget, qa-gate failed on promote), and that line short-
  circuits the aggregator with a specific ``stage=?`` reason.

DECOMPOSITION — FIVE INDIVIDUAL SINGLE-STAGE ACTIONS (2026-08-03)
-----------------------------------------------------------------
A release-check can be authored as a multi-step Plan (dependsOn chain)
where each step passes/fails on its OWN MCP call and, on FAIL, hands the
spawned BA Agent stage-specific "where + how to remediate" context. The
composed ``release-health-check`` verdict can't localize the failure — a
FAIL just says "stage X cluster Y", which is what the BA needs *when the
plan runs as a single blob*, but doesn't let a multi-step Plan skip
downstream stages once an upstream one has failed.

The five actions and the stages they cover:

* ``release-status``  — stage 1 (per repo, cluster='-') — jx_release.release_status
  + tekton PipelineRun outcome cross-check.
* ``promote-status``  — stage 2 opened (per cluster) — jx_release.promote_status
  says the promote PRs were opened on every requested cluster.
* ``verify-gate``     — stage 2 green (per cluster) — the promote PRs' verify
  + qa-gate green + merged (jx_release promote_status detail; retest_promote
  for a single flake). PASS iff merged green.
* ``boot-status``     — stage 3 (per cluster) — k8s.list_jobs_by_label /
  get_job_state — jx-boot Job for this release ran + succeeded per cluster.
* ``deploy-health``   — stage 4 (per cluster) — k8s.deploy_health — >=1
  available replica per cluster (the authoritative in-cluster signal).

Each per-stage helper below (``compute_release_status_verdict``,
``compute_promote_status_verdict``, …) reuses the SAME STAGE_STATUS parser
+ early-exit rules the composed verdict uses. The composed action stays
identical to its previous shape — the decomposition is additive.

On FAIL, each per-stage helper emits a STRUCTURED **BA failure context**
(``StageActionResult.ba_failure_context``) with fields the escalation
carries to the BA Agent so it knows WHERE (stage, cluster, mcp) and HOW
(``remediation_hint``) to start. The hints are stage-specific — matching
the shape the composed action's Maestro→BA escalation already emits but
scoped to ONE stage rather than "somewhere in stages 1..4".

Provider-agnostic Python — no ``claude_agent_sdk`` / ``anthropic`` imports,
no shell-outs, no kubectl, no httpx. Runs the same on Anthropic, DeepSeek,
or a laptop. Determinism is CODE-ENFORCED (a stray narrated "looks healthy"
is ignored — only STAGE_STATUS lines count).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Stage numbers the aggregator expects to see. Each is emitted at least
# once per applicable cluster (stage 1 is per repo, so cluster='-'; the
# rest are per cluster).
#
# Stage 1 — RELEASE FIRED: jx_release.release_status(repo) says released=true
#   AND (cross-check) the release Tekton PipelineRun outcome is Succeeded
#   (not just "tag missing"). Emitted once, cluster='-'.
# Stage 2 — PROMOTE/VERIFY/GATE/MERGED: jx_release.promote_status per
#   cluster says found + all_green + not gate_failed + all_merged.
#   Emitted per cluster; the LLM may retest_promote ONCE on a flake.
# Stage 3 — BOOT DEPLOYED: k8s.list_jobs_by_label / get_job_state per
#   cluster shows the jx-boot Job for this release ran and succeeded.
# Stage 4 — DEPLOY HEALTHY: k8s.deploy_health(service, namespace, cluster)
#   per cluster returns healthy=true (>=1 available replica). This is the
#   authoritative, in-cluster verdict — replaces the historical httpx probe.
REQUIRED_STAGES: tuple[int, ...] = (1, 2, 3, 4)

# The two clusters the platform runs on. Stage 1 emits cluster='-' (not
# cluster-scoped); stages 2/3/4 emit one line per cluster.
DEFAULT_CLUSTERS: tuple[str, ...] = ('gcp', 'az')

# Machine-readable stage-per-stage line. Case + spacing are pinned so the
# LLM's habit of "extra whitespace before/after tokens" doesn't drop lines.
# Format:
#     STAGE_STATUS: stage=<n> cluster=<gcp|az|-> verdict=<PASS|FAIL|SKIP> [reason=<...>]
_STAGE_STATUS_RE = re.compile(
    r'^\s*STAGE_STATUS:\s*'
    r'stage\s*=\s*(?P<stage>\d+)\s+'
    r'cluster\s*=\s*(?P<cluster>[a-zA-Z0-9\-_]+)\s+'
    r'verdict\s*=\s*(?P<verdict>PASS|FAIL|SKIP)'
    r'(?:\s+reason\s*=\s*(?P<reason>.+?))?\s*$',
    re.MULTILINE,
)

# Early-exit failure line the LLM may emit when it cannot proceed past a
# stage (release didn't fire in the budget, qa-gate failed on promote). If
# present, it short-circuits the aggregator to FAIL with the reason.
#
# Grammar (either):
#     RELEASE_HEALTH: FAIL: <reason>
#     RELEASE_HEALTH: FAIL: stage=<n> cluster=<gcp|az|-> reason=<...>
#
# The stage= / cluster= form is preferred (deterministic ``stage`` on the
# ProbeResult); the free-form is accepted for early runs.
_HEALTH_FAIL_RE = re.compile(
    r'^\s*RELEASE_HEALTH:\s*FAIL(?:\s*:\s*(?P<detail>.+))?\s*$',
    re.MULTILINE,
)


@dataclass(frozen=True)
class StageVerdict:
    """One (stage, cluster) verdict emitted by the LLM.

    Kept structured so the failure reason stays specific (e.g. "stage=4
    cluster=az reason=deployment has 0/1 available replicas"), which is
    what the infra-remediation loop needs to decide next actions.
    """

    stage: int
    cluster: str  # 'gcp' | 'az' | '-' (stage 1 is per repo)
    verdict: str  # 'PASS' | 'FAIL' | 'SKIP'
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'stage': self.stage,
            'cluster': self.cluster,
            'verdict': self.verdict,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class ProbeResult:
    """Rolled-up release-health verdict.

    Named ``ProbeResult`` for continuity with callers (``_health_check_verdict``
    still returns this shape), even though the stage-4 signal is no longer an
    HTTP probe — the LLM composes ``k8s.deploy_health`` for that.

    * ``verdict`` is 'PASS' iff every required (stage, cluster) pair reports
      PASS AND the LLM did not emit an early-exit ``RELEASE_HEALTH: FAIL``.
    * ``reason`` is None on PASS; on FAIL it names the FIRST failing stage +
      cluster with its LLM-emitted reason (or, when the LLM emitted a bare
      ``RELEASE_HEALTH: FAIL: <reason>``, the reason verbatim).
    * ``stages`` preserves every parsed STAGE_STATUS line in emission order
      so the caller can log the full stage-by-stage narrative.
    * ``failing_stage`` is a convenience field: the first failing stage
      number, or None on PASS. Used by structured logs so downstream
      dashboards can group failures by stage without re-parsing ``reason``.
    """

    verdict: str  # 'PASS' | 'FAIL'
    reason: str | None
    stages: tuple[StageVerdict, ...] = field(default_factory=tuple)
    failing_stage: int | None = None
    failing_cluster: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'verdict': self.verdict,
            'reason': self.reason,
            'stages': [s.as_dict() for s in self.stages],
            'failing_stage': self.failing_stage,
            'failing_cluster': self.failing_cluster,
        }


def parse_stage_verdicts(transcript: str) -> list[StageVerdict]:
    """Extract every ``STAGE_STATUS:`` line from the LLM transcript in emission
    order. A bad line (unknown verdict, missing fields) is dropped silently —
    the aggregator FAILs later because required stages will be missing, which
    is the right behaviour (no "PASS by silence")."""
    out: list[StageVerdict] = []
    for match in _STAGE_STATUS_RE.finditer(transcript):
        try:
            stage_num = int(match.group('stage'))
        except (TypeError, ValueError):
            continue
        cluster = match.group('cluster').strip()
        verdict = match.group('verdict').strip()
        reason = match.group('reason')
        reason = reason.strip() if reason else None
        # Trim a trailing double-quote-style wrap the LLM sometimes emits
        # ("reason=\"deployment not ready\""), so consumers see a clean string.
        if reason and len(reason) >= 2 and reason[0] == reason[-1] == '"':
            reason = reason[1:-1]
        out.append(StageVerdict(stage=stage_num, cluster=cluster, verdict=verdict, reason=reason))
    return out


def parse_early_exit_fail(transcript: str) -> tuple[int | None, str | None, str | None]:
    """If the LLM emitted ``RELEASE_HEALTH: FAIL: ...``, return ``(stage,
    cluster, reason)``. Returns ``(None, None, None)`` when absent.

    Supports two grammars:
      1. ``RELEASE_HEALTH: FAIL: <reason>`` — reason returned as-is, stage +
         cluster None (the aggregator surfaces this as a whole-run FAIL).
      2. ``RELEASE_HEALTH: FAIL: stage=<n> cluster=<c> reason=<r>`` — parsed
         into structured fields for logging + BA correlation.

    LAST match wins so a narrated retry earlier in the transcript doesn't
    override the final line. A stray ``RELEASE_HEALTH: PASS`` from the LLM
    is IGNORED — PASS is decided by parsed STAGE_STATUS lines, never by the
    LLM saying so.
    """
    matches = list(_HEALTH_FAIL_RE.finditer(transcript))
    if not matches:
        return (None, None, None)
    last = matches[-1]
    detail = last.group('detail')
    if not detail:
        return (None, None, 'LLM declared FAIL without a reason')
    detail = detail.strip()
    # Structured form? stage=... cluster=... reason=...
    struct_re = re.compile(r'stage\s*=\s*(\d+)\s+cluster\s*=\s*([a-zA-Z0-9\-_]+)\s+reason\s*=\s*(.+)$')
    struct_match = struct_re.match(detail)
    if struct_match:
        try:
            stage_num = int(struct_match.group(1))
        except (TypeError, ValueError):
            stage_num = None
        cluster = struct_match.group(2).strip()
        reason = struct_match.group(3).strip()
        return (stage_num, cluster or None, reason)
    return (None, None, detail)


def compute_release_health(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] = DEFAULT_CLUSTERS,
    required_stages: tuple[int, ...] = REQUIRED_STAGES,
) -> ProbeResult:
    """Compose the deterministic release-health verdict from an LLM transcript.

    Contract:

    * If the LLM emitted an early-exit ``RELEASE_HEALTH: FAIL`` line, the
      verdict is FAIL with that reason. This is the "genuine failure" path —
      stage 1-3 signals that the aggregator cannot proceed past (release did
      not fire in the budget, qa-gate failed on promote).
    * Otherwise, every required (stage, cluster) pair MUST have a
      ``STAGE_STATUS: ... verdict=PASS`` line. Stage 1 is per repo
      (``cluster='-'`` accepted). Stages 2/3/4 must have a PASS per required
      cluster.
    * On FAIL the aggregator names the FIRST failing (stage, cluster) with
      its LLM-emitted reason; on missing coverage it names the FIRST missing
      (stage, cluster) as the failure. No "PASS by silence".
    * SKIP is treated as a soft SKIP (not a FAIL, not a PASS) — used only for
      stages the LLM explicitly declared not applicable in this run (e.g. a
      single-cluster service where az was intentionally not probed). The
      aggregator still requires SOME PASS/FAIL coverage on each required
      pair, so a SKIP without a corresponding PASS is a coverage gap → FAIL.
    """
    stages_seen = parse_stage_verdicts(transcript)

    # Early-exit FAIL short-circuits before we count STAGE_STATUS lines.
    early_stage, early_cluster, early_reason = parse_early_exit_fail(transcript)
    if early_reason is not None:
        return ProbeResult(
            verdict='FAIL',
            reason=_format_early_exit_reason(early_stage, early_cluster, early_reason),
            stages=tuple(stages_seen),
            failing_stage=early_stage,
            failing_cluster=early_cluster,
        )

    # Aggregate: build an index of PASSing (stage, cluster) pairs, and note
    # the first FAIL we encounter for a specific reason.
    passing: set[tuple[int, str]] = set()
    first_fail: StageVerdict | None = None
    for sv in stages_seen:
        if sv.verdict == 'PASS':
            passing.add((sv.stage, sv.cluster))
        elif sv.verdict == 'FAIL' and first_fail is None:
            first_fail = sv
        # SKIP: no-op (coverage-required pairs still must have PASS).

    if first_fail is not None:
        return ProbeResult(
            verdict='FAIL',
            reason=_format_stage_fail(first_fail),
            stages=tuple(stages_seen),
            failing_stage=first_fail.stage,
            failing_cluster=first_fail.cluster,
        )

    # No FAIL emitted; check coverage. Stage 1 requires cluster='-' (or any
    # of the required clusters — the LLM may accidentally set a cluster on
    # the not-cluster-scoped stage; both accepted). Stages 2/3/4 require a
    # PASS per required cluster.
    missing = _first_missing_coverage(passing, required_stages, required_clusters)
    if missing is not None:
        stage_num, cluster = missing
        return ProbeResult(
            verdict='FAIL',
            reason=(
                f'stage {stage_num} cluster={cluster}: no STAGE_STATUS PASS emitted '
                '(aggregator requires an explicit PASS per required (stage, cluster))'
            ),
            stages=tuple(stages_seen),
            failing_stage=stage_num,
            failing_cluster=cluster,
        )

    return ProbeResult(verdict='PASS', reason=None, stages=tuple(stages_seen))


def _first_missing_coverage(
    passing: set[tuple[int, str]],
    required_stages: tuple[int, ...],
    required_clusters: tuple[str, ...],
) -> tuple[int, str] | None:
    """Return the first (stage, cluster) required but not PASSing.

    Stage 1 is per-repo: accept cluster='-' OR any required cluster (the LLM
    is permitted to attribute the stage-1 PASS to either shape).
    Stages 2..N are per-cluster: require a PASS for each required cluster.
    """
    for stage_num in required_stages:
        if stage_num == 1:
            # Any of {'-', *required_clusters} satisfies stage-1 coverage.
            candidates = {'-', *required_clusters}
            if not any((stage_num, c) in passing for c in candidates):
                return (stage_num, '-')
            continue
        for cluster in required_clusters:
            if (stage_num, cluster) not in passing:
                return (stage_num, cluster)
    return None


def _format_stage_fail(sv: StageVerdict) -> str:
    """Render a STAGE_STATUS FAIL into the verdict's ``reason``."""
    base = f'stage {sv.stage} cluster={sv.cluster}'
    if sv.reason:
        return f'{base}: {sv.reason}'
    return f'{base}: FAIL (no reason given)'


def _format_early_exit_reason(stage: int | None, cluster: str | None, reason: str) -> str:
    """Render an early-exit RELEASE_HEALTH: FAIL into the verdict's ``reason``.

    Includes the structured ``stage=<n> cluster=<c>`` prefix when the LLM
    supplied them, so ``reason`` is diagnosable in isolation.
    """
    if stage is not None and cluster is not None:
        return f'stage {stage} cluster={cluster}: {reason}'
    if stage is not None:
        return f'stage {stage}: {reason}'
    return reason


# ── Per-stage single-action helpers ──────────────────────────────────────────
#
# Each of the five individual actions (``release-status`` /
# ``promote-status`` / ``verify-gate`` / ``boot-status`` /
# ``deploy-health``) reuses the SAME STAGE_STATUS parser above but scopes
# coverage to ONE stage (and the per-action cluster set) rather than all
# four stages the composed ``release-health-check`` requires.
#
# The per-action functions share this contract:
#
#   compute_<action>_verdict(transcript, *, required_clusters=...) ->
#       StageActionResult
#
# The result is:
#
#   verdict            'PASS' | 'FAIL'
#   reason             None on PASS; on FAIL a one-line diagnostic
#   stage              the stage number this action covers (1..4)
#   stages             every STAGE_STATUS line parsed from the transcript
#                      (preserved in emission order for downstream logs)
#   failing_stage      the stage number when verdict=FAIL, else None
#   failing_cluster    the cluster (or '-') when verdict=FAIL, else None
#   ba_failure_context on FAIL, a structured dict the escalation carries to
#                      the BA Agent — {stage, cluster, mcp, expected,
#                      mcp_returned, remediation_hint}. None on PASS.
#
# On FAIL each helper emits a stage-specific BA failure context so a
# spawned BA Agent knows WHERE + HOW to start remediation — the whole
# point of the decomposition. The composed release-health-check verdict
# stays identical (single action's verdict is stage=1..4 coverage roll-up)
# — the individual actions merely narrow the scope to ONE stage each so a
# multi-step Plan can dependsOn-chain them and stop the moment an upstream
# stage fails, handing the BA a stage-scoped brief instead of "somewhere
# in stages 1..4".


# Per-action stage number pins. Used by:
#   - the single-stage aggregators to know which STAGE_STATUS lines to
#     require coverage on;
#   - the tests to guard against silent renumbering;
#   - the infra_agent system prompt (INDIVIDUAL_STAGE_ACTIONS below) as
#     the single source of truth for "which stage does <action> cover".
STAGE_RELEASE_STATUS: int = 1
STAGE_PROMOTE_STATUS: int = 2
STAGE_VERIFY_GATE: int = 2
STAGE_BOOT_STATUS: int = 3
STAGE_DEPLOY_HEALTH: int = 4


@dataclass(frozen=True)
class StageActionResult:
    """Deterministic verdict for one single-stage individual action.

    Parallel shape to :class:`ProbeResult` but scoped to ONE stage. The
    ``ba_failure_context`` field is the whole point of the decomposition
    — on FAIL it carries a structured stage-specific brief the BA Agent
    consumes when the escalation fires (no free-form "figure it out
    yourself" prompt).
    """

    verdict: str  # 'PASS' | 'FAIL'
    reason: str | None
    stage: int
    stages: tuple[StageVerdict, ...] = field(default_factory=tuple)
    failing_stage: int | None = None
    failing_cluster: str | None = None
    ba_failure_context: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            'verdict': self.verdict,
            'reason': self.reason,
            'stage': self.stage,
            'stages': [s.as_dict() for s in self.stages],
            'failing_stage': self.failing_stage,
            'failing_cluster': self.failing_cluster,
            'ba_failure_context': self.ba_failure_context,
        }


# Stage-specific BA remediation guidance. Keyed by action name so the
# per-stage aggregator can render a structured brief on FAIL that names
# WHICH MCP produced the failing signal + a stage-scoped remediation
# hint. Kept as data (not code) so a new action wiring landing later can
# extend the map without touching the aggregator control flow.
BA_STAGE_GUIDANCE: dict[str, dict[str, str]] = {
    'release-status': {
        'mcp': 'mcp__leartech-jx-release__release_status + mcp__leartech-tekton__list_pipelineruns_for_pr',
        'expected': (
            'release_status(repo) returns released=true AND the release PipelineRun '
            'reached completionType Succeeded (not just tag missing)'
        ),
        'remediation_hint': (
            'The release Tekton pipeline did NOT fire or did NOT succeed for this repo. '
            'Common causes: (1) merge into main did not tag a release (check .lighthouse/jenkins-x/release.yaml '
            'triggers + Lighthouse Keeper logs), (2) the release PipelineRun failed at a specific step '
            '(inspect step_status on the release PipelineRun — often kaniko OOM, image push auth, or '
            'jx-release-version race); handoff should surface the failing step name from list_pipelineruns_for_pr.'
        ),
    },
    'promote-status': {
        'mcp': 'mcp__leartech-jx-release__promote_status',
        'expected': (
            'promote_status(service, clusters) reports found=true (jx-promote opened a promote PR) '
            'for EVERY requested cluster'
        ),
        'remediation_hint': (
            'jx-promote did NOT open a promote PR on at least one requested cluster. Common causes: '
            '(1) the release fired but jx-promote is not configured for the cluster (env repo missing '
            'from source-config), (2) the cluster is registered but the env-repo webhook is broken, '
            '(3) the release only pushed to ONE cluster registry (asymmetric release — check the release '
            'PipelineRun outcome per cluster). Handoff should target the affected cluster + the env repo '
            'for that cluster (jx-build-cluster-gsm for gcp, jx-build-cluster-akv for az).'
        ),
    },
    'verify-gate': {
        'mcp': 'mcp__leartech-jx-release__promote_status + mcp__leartech-jx-release__retest_promote',
        'expected': (
            'promote_status(service, clusters) reports all_green=true AND merged=true '
            '(Tide auto-merged the promote PR on green) for every requested cluster; a single flake '
            'may be recovered via retest_promote ONCE, not repeatedly'
        ),
        'remediation_hint': (
            'The promote PR opened but its verify/qa-gate is RED (or the PR is stuck non-green). '
            'Common causes: (1) qa-gate on the env repo failed (real issue in the release — this needs a '
            'cross-plan Infra-agent to fix the env-repo qa policy or the release itself), (2) the promote PR '
            'is blocked by a merge conflict (a human or the env-repo shepherd must rebase), (3) a genuine '
            'test flake on the env-repo pipelines (retest_promote handles ONE flake — repeated flakes are '
            'a real signal). Handoff should include the failing check name from promote_status detail so BA '
            'can either dispatch a fix-env-repo initiative or escalate to a human.'
        ),
    },
    'boot-status': {
        'mcp': 'mcp__leartech-k8s__list_jobs_by_label + mcp__leartech-k8s__get_job_state',
        'expected': (
            'the jx-boot Job for this release ran and reached succeeded=true on every requested cluster '
            "(list_jobs_by_label finds a Job with the release's boot label; get_job_state on that Job "
            'returns succeeded=true, failed=false)'
        ),
        'remediation_hint': (
            'The promote PR merged but the jx-boot Job did NOT succeed on at least one cluster. '
            'Common causes: (1) jx-boot Job failed at helmfile-apply (missing values / bad chart / secret '
            "not seeded — inspect get_pod_events + get_pod_logs on the Job's pod), (2) the Job never "
            'spawned because the boot ExternalSecret / ServiceAccount is missing on the cluster, '
            '(3) node scheduling starvation (get_pod_events shows FailedScheduling). Handoff should include '
            'the Job name + last-N pod log lines so BA can dispatch either a chart fix (dev PR against the '
            'service repo) or a human-owned secret provisioning step (see capability_gaps: '
            'needs-human:provision-secret).'
        ),
    },
    'deploy-health': {
        'mcp': 'mcp__leartech-k8s__deploy_health',
        'expected': (
            'deploy_health(service, namespace, cluster) returns healthy=true '
            '(>=1 available replica; desired_replicas == available_replicas or the deployment has '
            'reached its desired rollout state) on every requested cluster'
        ),
        'remediation_hint': (
            'The jx-boot Job succeeded but the Deployment is NOT healthy on at least one cluster. '
            "Common causes: (1) the pod is crashlooping (get_pod_state / get_pod_logs on the deployment's "
            'pod — usually a config bug or missing secret at runtime), (2) the readiness probe never passes '
            '(app-level bug or wrong probe path in chart), (3) the ReplicaSet scaled to zero due to a HPA '
            'policy or a resource quota. NOTE: NEVER re-introduce the httpx probe against /health from this '
            'sandbox — deploy_health is the authoritative signal because it reads Deployment status from '
            'inside the cluster. Handoff should include available_replicas + desired_replicas + the '
            'deploy_health reason string so BA can dispatch the right fix (dev PR for a code bug vs. an '
            'infra step for a chart/probe fix).'
        ),
    },
}


def _default_clusters_for_stage(stage: int) -> tuple[str, ...]:
    """Stage 1 is per-repo (cluster='-'); every other stage is per cluster.

    Kept as a helper so a future single-cluster mode (say, gcp-only when
    the target service is single-cluster) can be introduced in one place.
    """
    if stage == 1:
        return ('-',)
    return DEFAULT_CLUSTERS


def _first_missing_stage_coverage(
    passing: set[tuple[int, str]],
    stage: int,
    required_clusters: tuple[str, ...],
) -> tuple[int, str] | None:
    """Return the first (stage, cluster) required for THIS stage but not PASSing.

    Stage 1 accepts cluster='-' OR any required cluster (the LLM
    occasionally attributes the per-repo stage-1 PASS to a cluster). All
    other stages require a PASS per required cluster.
    """
    if stage == 1:
        # Any of {'-', *required_clusters} satisfies stage-1 coverage.
        candidates = {'-', *required_clusters}
        if not any((stage, c) in passing for c in candidates):
            return (stage, '-')
        return None
    for cluster in required_clusters:
        if (stage, cluster) not in passing:
            return (stage, cluster)
    return None


def _build_ba_failure_context(
    action: str,
    *,
    stage: int,
    cluster: str,
    reason: str,
    mcp_returned: str | None = None,
) -> dict[str, Any]:
    """Assemble the stage-specific BA failure context.

    The context is what the escalation carries to the spawned BA Agent so
    it knows WHERE + HOW to start — no more "figure out yourself which
    stage failed and which MCP to call next". Fields:

      * ``stage``            — the stage number (1..4)
      * ``cluster``          — the failing cluster (or '-' for stage 1)
      * ``mcp``              — the MCP tool(s) this stage's verdict came from
      * ``action``           — the individual action name (e.g. 'boot-status')
      * ``expected``         — what a PASS would look like from that MCP
      * ``mcp_returned``     — what the MCP actually returned (from
                               ``STAGE_STATUS ... reason=<...>``); ``None``
                               when the failure is missing coverage rather
                               than an explicit STAGE_STATUS FAIL
      * ``reason``           — the aggregator's rendered one-line reason
      * ``remediation_hint`` — stage-specific "how to start remediating"
                               guidance the BA Agent's prompt renders
                               verbatim

    Guidance defaults to a permissive shape if a caller ever passes an
    unknown action name — never crashes.
    """
    guidance = BA_STAGE_GUIDANCE.get(
        action,
        {
            'mcp': 'unknown',
            'expected': f'stage {stage} PASS emitted',
            'remediation_hint': (
                f'No stage-specific guidance registered for action {action!r}; '
                'BA should inspect the transcript and start from the failing stage.'
            ),
        },
    )
    return {
        'stage': stage,
        'cluster': cluster,
        'action': action,
        'mcp': guidance['mcp'],
        'expected': guidance['expected'],
        'mcp_returned': mcp_returned,
        'reason': reason,
        'remediation_hint': guidance['remediation_hint'],
    }


def compute_stage_action_verdict(
    action: str,
    transcript: str,
    stage: int,
    *,
    required_clusters: tuple[str, ...] | None = None,
) -> StageActionResult:
    """Deterministic verdict for a single-stage individual action.

    Contract (mirrors :func:`compute_release_health` but scoped to ONE stage):

    * If the LLM emitted an early-exit ``RELEASE_HEALTH: FAIL`` line, the
      verdict is FAIL with that reason. This is the "genuine failure"
      path — a stage signal that the aggregator cannot proceed past.
    * Otherwise, THIS stage MUST have a ``STAGE_STATUS: ... verdict=PASS``
      line for every required cluster (stage 1 is per-repo).
    * On a STAGE_STATUS FAIL for THIS stage, verdict is FAIL. STAGE_STATUS
      FAIL lines for OTHER stages are ignored (a decomposed action shouldn't
      fail on an unrelated stage's emission).
    * On missing coverage, verdict is FAIL naming the first missing
      (stage, cluster).
    * On FAIL, ``ba_failure_context`` carries the structured stage-specific
      brief the BA Agent consumes.
    """
    stages_seen = parse_stage_verdicts(transcript)

    # Choose the cluster set for THIS stage.
    if required_clusters is None:
        required_clusters = _default_clusters_for_stage(stage)

    # Early-exit FAIL short-circuits before we count STAGE_STATUS lines.
    early_stage, early_cluster, early_reason = parse_early_exit_fail(transcript)
    if early_reason is not None:
        reason = _format_early_exit_reason(early_stage, early_cluster, early_reason)
        cluster_for_ctx = (
            early_cluster
            if early_cluster is not None
            else ('-' if stage == 1 else required_clusters[0] if required_clusters else '-')
        )
        return StageActionResult(
            verdict='FAIL',
            reason=reason,
            stage=stage,
            stages=tuple(stages_seen),
            failing_stage=early_stage if early_stage is not None else stage,
            failing_cluster=cluster_for_ctx,
            ba_failure_context=_build_ba_failure_context(
                action,
                stage=early_stage if early_stage is not None else stage,
                cluster=cluster_for_ctx,
                reason=reason,
                mcp_returned=early_reason,
            ),
        )

    # Build the passing set for THIS stage only, and note the first FAIL we
    # encounter FOR THIS STAGE. FAILs on other stages are ignored — a
    # single-stage action shouldn't be tripped by an unrelated stage's noise.
    passing: set[tuple[int, str]] = set()
    first_fail: StageVerdict | None = None
    for sv in stages_seen:
        if sv.stage != stage:
            continue
        if sv.verdict == 'PASS':
            passing.add((sv.stage, sv.cluster))
        elif sv.verdict == 'FAIL' and first_fail is None:
            first_fail = sv
        # SKIP: no-op (coverage-required pairs still must have PASS).

    if first_fail is not None:
        reason = _format_stage_fail(first_fail)
        return StageActionResult(
            verdict='FAIL',
            reason=reason,
            stage=stage,
            stages=tuple(stages_seen),
            failing_stage=first_fail.stage,
            failing_cluster=first_fail.cluster,
            ba_failure_context=_build_ba_failure_context(
                action,
                stage=first_fail.stage,
                cluster=first_fail.cluster,
                reason=reason,
                mcp_returned=first_fail.reason,
            ),
        )

    missing = _first_missing_stage_coverage(passing, stage, required_clusters)
    if missing is not None:
        stage_num, cluster = missing
        reason = (
            f'stage {stage_num} cluster={cluster}: no STAGE_STATUS PASS emitted '
            '(aggregator requires an explicit PASS per required (stage, cluster))'
        )
        return StageActionResult(
            verdict='FAIL',
            reason=reason,
            stage=stage,
            stages=tuple(stages_seen),
            failing_stage=stage_num,
            failing_cluster=cluster,
            ba_failure_context=_build_ba_failure_context(
                action,
                stage=stage_num,
                cluster=cluster,
                reason=reason,
                mcp_returned=None,
            ),
        )

    return StageActionResult(
        verdict='PASS',
        reason=None,
        stage=stage,
        stages=tuple(stages_seen),
    )


def compute_release_status_verdict(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] | None = None,
) -> StageActionResult:
    """Verdict for the ``release-status`` action (stage 1, per repo).

    PASS iff the release Tekton pipeline fired AND completed Succeeded
    (release_status(repo) says released=true AND the release PipelineRun
    outcome cross-check via leartech-tekton is Succeeded).
    """
    return compute_stage_action_verdict(
        'release-status',
        transcript,
        STAGE_RELEASE_STATUS,
        required_clusters=required_clusters,
    )


def compute_promote_status_verdict(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] = DEFAULT_CLUSTERS,
) -> StageActionResult:
    """Verdict for the ``promote-status`` action (stage 2, per cluster).

    PASS iff jx-promote opened promote PRs on ALL requested clusters
    (jx_release.promote_status reports found=true per requested cluster).
    Does NOT check verify/qa-gate/merged — that's the ``verify-gate`` action.
    """
    return compute_stage_action_verdict(
        'promote-status',
        transcript,
        STAGE_PROMOTE_STATUS,
        required_clusters=required_clusters,
    )


def compute_verify_gate_verdict(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] = DEFAULT_CLUSTERS,
) -> StageActionResult:
    """Verdict for the ``verify-gate`` action (stage 2 green, per cluster).

    PASS iff the promote PRs' verify + qa-gate is green AND the PR merged
    on every requested cluster (jx_release promote_status reports
    all_green=true + merged=true; retest_promote may have been used once
    for a single flake but the final state must still be merged).

    STAGE_STATUS lines for verify-gate use stage=2 (same as promote-status
    — they're both "stage 2" in the composed model), but the LLM's
    verify-gate emission is required to reflect the merged/green state
    (its stage 2 semantics), not merely PR-opened.
    """
    return compute_stage_action_verdict(
        'verify-gate',
        transcript,
        STAGE_VERIFY_GATE,
        required_clusters=required_clusters,
    )


def compute_boot_status_verdict(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] = DEFAULT_CLUSTERS,
) -> StageActionResult:
    """Verdict for the ``boot-status`` action (stage 3, per cluster).

    PASS iff the jx-boot Job for this release ran + succeeded on every
    requested cluster (k8s.list_jobs_by_label / k8s.get_job_state).
    """
    return compute_stage_action_verdict(
        'boot-status',
        transcript,
        STAGE_BOOT_STATUS,
        required_clusters=required_clusters,
    )


def compute_deploy_health_verdict(
    transcript: str,
    *,
    required_clusters: tuple[str, ...] = DEFAULT_CLUSTERS,
) -> StageActionResult:
    """Verdict for the ``deploy-health`` action (stage 4, per cluster).

    PASS iff every requested cluster's Deployment has >=1 available replica
    (k8s.deploy_health). No httpx, no kubectl — the k8s MCP is the
    authoritative in-cluster signal.
    """
    return compute_stage_action_verdict(
        'deploy-health',
        transcript,
        STAGE_DEPLOY_HEALTH,
        required_clusters=required_clusters,
    )


# Registry: individual-action name → verdict function + stage number.
# Consumed by :mod:`gate.agent.infra_agent`'s dispatch so a plan step's
# ``action: <name>`` selects the right per-stage aggregator without a
# large if/elif tree. Kept as data so a future action landing later can
# extend the map in one place.
INDIVIDUAL_STAGE_ACTIONS: dict[str, dict[str, Any]] = {
    'release-status': {
        'stage': STAGE_RELEASE_STATUS,
        'aggregator': compute_release_status_verdict,
    },
    'promote-status': {
        'stage': STAGE_PROMOTE_STATUS,
        'aggregator': compute_promote_status_verdict,
    },
    'verify-gate': {
        'stage': STAGE_VERIFY_GATE,
        'aggregator': compute_verify_gate_verdict,
    },
    'boot-status': {
        'stage': STAGE_BOOT_STATUS,
        'aggregator': compute_boot_status_verdict,
    },
    'deploy-health': {
        'stage': STAGE_DEPLOY_HEALTH,
        'aggregator': compute_deploy_health_verdict,
    },
}


def is_individual_stage_action(action: str) -> bool:
    """True iff ``action`` names one of the FIVE decomposed single-stage actions."""
    return action in INDIVIDUAL_STAGE_ACTIONS
