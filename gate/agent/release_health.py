"""Deterministic verdict aggregator for the infra release-health-check action.

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
