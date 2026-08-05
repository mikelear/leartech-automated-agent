"""Infra agent — cluster-side write-mode loop (repo-factory + release verification).

Mirrors ``gate/agent/main.py``'s query-loop shape but WRITE-mode. It owns the repo/cluster
wiring the dev agent doesn't:

  - ``create-repo``            : create the GitHub repo (README so ``main`` exists to PR against)
  - ``register-source-config``: register the repo in a cluster's source-config (one PR per cluster)
  - ``deploy-config``         : land the deploy/helmfile config for a cluster
  - ``scaffold-pr``           : deterministically scaffold from a language template and open the PR
                                whose preview exercises the Tekton steps
  - ``release-health-check``  : after a dev PR merges, verify the release is HEALTHY (the
                                "PR merged != release healthy" gap)

Scaffolding is DETERMINISTIC via ``gate.tools.repo_factory`` (literal rename, never LLM
grep/replace); the agent orchestrates + handles per-cluster variation but does NOT hand-edit
template files. Its persona (MCPs/tools/model) is the ``infra_agent`` role in
``mcp_catalog.yaml``; it runs on its own gateway virtual key (see
memory project_per_agent_model_routing), so its model can be swapped cheaply without a
redeploy. See memory project_repo_factory_init for the Plan that drives these actions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable

import click
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from gate import obslog
from gate.agent.calibrations import load_jx3_calibration
from gate.agent.initiative import INITIATIVE_TEKTON_TOOLS, WRITE_MODE_TOOLS
from gate.agent.lessons import render_for
from gate.agent.main import DEFAULT_MODEL, MCP_ALLOWED_TOOLS
from gate.agent.release_health import (
    DEFAULT_CLUSTERS,
    INDIVIDUAL_STAGE_ACTIONS,
    ProbeResult,
    StageActionResult,
    compute_release_health,
    is_individual_stage_action,
)
from gate.agent.test_mode import parse_test_mode, run_test_mode
from gate.mcp_servers import build_remote_mcp_servers

DEFAULT_MAX_TURNS = 200

# The repo-factory MCP tools (server-side, on the platform-mcps host). create/register/
# scaffold run with the owner PAT server-side — the agent just calls them.
REPO_FACTORY_TOOLS = [
    'mcp__leartech-repo-factory__create_repo',
    'mcp__leartech-repo-factory__register_source_config',
    'mcp__leartech-repo-factory__scaffold',
    'mcp__leartech-repo-factory__smoke_pr',
]

# jx_release MCP — the JX3 release-check primitives (GitHub-API-first, both clusters). The
# release-health-check action composes these to shepherd a release through jx-promote.
JX_RELEASE_TOOLS = [
    'mcp__leartech-jx-release__release_status',
    'mcp__leartech-jx-release__promote_status',
    'mcp__leartech-jx-release__retest_promote',
]

# k8s MCP — in-cluster read surface (no kubectl needed on the agent side). The
# release-health-check action composes these for stages 3 + 4:
#   * list_jobs_by_label / get_job_state — did the jx-boot Job for this release
#     run and succeed on each cluster? (stage 3)
#   * deploy_health — is the Deployment healthy (>=1 available replica) on each
#     cluster? (stage 4 — replaces the historical unreachable HTTP /health probe)
K8S_TOOLS = [
    'mcp__leartech-k8s__deploy_health',
    'mcp__leartech-k8s__get_job_state',
    'mcp__leartech-k8s__list_jobs_by_label',
]

# Write-mode built-ins + the shared MCP surface + step-aware Tekton tools + the repo-factory,
# jx-release, and k8s MCPs. Deterministic repo ops go through repo-factory (server-side); the
# release check composes jx-release + tekton + k8s (no httpx probe, no kubectl on the agent
# side — the k8s MCP host runs in-cluster with a read-scoped ServiceAccount).
INFRA_ALLOWED_TOOLS = [
    *WRITE_MODE_TOOLS,
    *MCP_ALLOWED_TOOLS,
    *INITIATIVE_TEKTON_TOOLS,
    *REPO_FACTORY_TOOLS,
    *JX_RELEASE_TOOLS,
    *K8S_TOOLS,
]

INFRA_SYSTEM_PROMPT = """\
You are the leartech INFRA AGENT. You own repo/cluster wiring and release verification —
the cluster-side work the dev agent does not do. You are precise, deterministic, and you
prefer proven tools over improvisation.

GROUND RULES
- Repo creation, source-config registration, and scaffolding are DETERMINISTIC and run
  SERVER-SIDE via the repo-factory MCP. CALL the tools — never Bash/gh/git or hand-edit YAML:
    * mcp__leartech-repo-factory__create_repo — creates the repo under the OWNER account
      (rejects bot tokens) and invites the 6 machine bots as collaborators.
    * mcp__leartech-repo-factory__register_source_config — idempotent source-config PR on a
      cluster (skips if already registered). cluster is 'gcp' or 'az'.
    * mcp__leartech-repo-factory__scaffold — renders a template into the target repo (literal
      rename, no grep) via the Git Data API. to_main=true pushes it straight to main (bootstrap
      a new repo: triggers land on main + the release fires); else it opens a scaffold PR.
    * mcp__leartech-repo-factory__smoke_pr — opens a trivial gated PR to verify the bootstrapped
      repo's PR pipelines fire (main now has .lighthouse/ triggers).
  The high-privilege owner credential lives in the MCP host, NOT here. If a tool errors or a
  rename looks wrong, report it as a TOOL bug — do not patch by hand.
- The JX3 release check is DETERMINISTIC — go through the jx-release, tekton, and k8s MCPs,
  never hand-scrape Tekton, GitHub, or attempt a kubectl or /health HTTP probe from this
  sandbox. The infra sandbox CANNOT reach the ingress (no cluster DNS / routing); any HTTP
  probe you attempt against /health will fail transport even on a perfectly healthy deploy.
  The k8s MCP host runs in-cluster with a read-scoped ServiceAccount and gives you the
  authoritative signals directly:
    * mcp__leartech-jx-release__release_status — did the release fire on the repo?
    * mcp__leartech-jx-release__promote_status — promote PRs across both clusters + verify/gate
      state (all_green / gate_failed / merged / all_merged).
    * mcp__leartech-jx-release__retest_promote — chatops /retest to clear ONE flake.
    * mcp__leartech-tekton__list_pipelineruns_for_pr / step_status — cross-check the
      RELEASE PipelineRun's outcome (Succeeded / Failed / Running), so stage 1 fails
      closed when the release pipeline failed rather than merely lacking a tag.
    * mcp__leartech-k8s__list_jobs_by_label / get_job_state — did the jx-boot Job for this
      release run and succeed on each cluster (stage 3)?
    * mcp__leartech-k8s__deploy_health — is the Deployment healthy on each cluster (>=1
      available replica) (stage 4 — replaces the unreachable HTTP probe)?
- The platform runs on TWO clusters (GCP gitops `jx-build-cluster-gsm`, Azure
  `jx-build-cluster-akv`). Registration is ONE PR PER CLUSTER — do the cluster in your inputs;
  a Plan runs one register step per cluster.
- Config lives in repos, platform logic in the pipeline-catalog — the template already
  references the catalog; never copy pipeline logic into a new repo.

ACTIONS (your inputs include `action` + its params):
- create-repo: call mcp__leartech-repo-factory__create_repo with name=<short repo name>. It
  creates under the owner + invites the bots server-side. Params: newRepo (pass its short name).
- register-source-config: call mcp__leartech-repo-factory__register_source_config with
  service + cluster AND run_id=$LEARTECH_RUN_ID, namespace=$AGENT_RUN_NAMESPACE. It edits the
  cluster's source-config, opens the PR, AUTO-APPROVES it (owner /approve so Tide merges), and
  records the PR onto THIS AgentRun so the register step is MERGE-GATED (AwaitingReview until
  merged, then Succeeds) — so a downstream scaffold waits until the repo is really registered
  (Lighthouse/webhook live). Idempotent (no PR if already registered → Succeeds immediately).
  Params: service, cluster ('gcp'|'az'), run_id, namespace.
- scaffold-pr: BOOTSTRAP a brand-new repo — call mcp__leartech-repo-factory__scaffold with
  template, target_repo, name, to_main=true. It renders the template and pushes it (incl.
  .lighthouse/ triggers) STRAIGHT TO main (no PR), which both lets later PRs gate AND fires the
  release off the main push. Do NOT pass run_id here (no PR to record); the step is repo:"" and
  Succeeds on push. Params: template, name (target_repo = mikelear/<name>), to_main=true.
- smoke-pr: after scaffold-pr, OPEN the trivial smoke PR (deterministic plumbing) — call
  mcp__leartech-repo-factory__smoke_pr with target_repo, marker=<name> (the SERVICE name from
  your inputs, so the branch is the deterministic `smoke-<name>`). Do NOT pass run_id/namespace:
  this step just OPENS the PR (fire-and-forget, repo:"") — a downstream Dev-agent step ADOPTS
  branch `smoke-<name>` (via idempotent open_pr) and OWNS it (watch gates, fix failures, merge).
  Infra opens + verifies the plumbing; the Dev agent drives the PR. Params: target_repo =
  mikelear/<name>, name.
- release-health-check: shepherd the service THROUGH the JX3 release pipeline to a landed,
  HEALTHY release — the automation of the manual release watch. You are triggered when the
  dev PR OPENS (AwaitingReview), so nothing has released yet; you WAIT and drive it using
  the jx-release + tekton + k8s MCPs. Bounded by `budgetMinutes` from your inputs (default
  60 if unset) — a real cold-repo multi-cluster release+promote+deploy can take 40-50 min,
  so do NOT give up early. Poll ~60s between checks (`sleep 60`) — never one giant sleep.

  DETERMINISM CONTRACT — this is the WHOLE POINT of the refactor. Every stage is composed
  from MCP calls that return a structured verdict, and you emit ONE MACHINE-READABLE LINE
  per (stage, cluster) that Python then aggregates into the final PASS/FAIL. No httpx probe,
  no kubectl, no ingress /health GET, no free-form narration deciding the outcome. Failing
  to emit a required STAGE_STATUS line is a FAIL (no PASS-by-silence).

  Emit the line EXACTLY in this shape (case + spacing pinned):

      STAGE_STATUS: stage=<n> cluster=<gcp|az|-> verdict=<PASS|FAIL|SKIP> reason=<one-line>

  where cluster='-' is used ONLY for stage 1 (per-repo). The optional reason=... is
  REQUIRED on FAIL and SKIP; on PASS it is optional but helpful (`healthy=true replicas=2`).

  Stages (drive them in order; stop + emit RELEASE_HEALTH: FAIL only when a stage cannot
  progress before the budget elapses):

    1. RELEASE FIRED (per repo, cluster='-') — poll
       mcp__leartech-jx-release__release_status(repo=mikelear/<service>) until released=true
       AND cross-check the release PipelineRun's OUTCOME via
       mcp__leartech-tekton__list_pipelineruns_for_pr / step_status: the release
       PipelineRun must have completed Succeeded (not just "tag missing"). If the release
       PipelineRun FAILED, emit
           STAGE_STATUS: stage=1 cluster=- verdict=FAIL reason=release PipelineRun <name> failed at step <step>
       and STOP (do NOT proceed to later stages). On success emit
           STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=release <tag> Succeeded

    2. PROMOTE / VERIFY / GATE / MERGED (per cluster) — poll
       mcp__leartech-jx-release__promote_status(service, clusters=[gcp,az]). For each
       cluster:
         * found=false → keep polling; do not emit STAGE_STATUS yet.
         * non-green but NOT gate_failed (flake) → call
           mcp__leartech-jx-release__retest_promote(cluster, pr_number) ONCE, then keep
           polling. Do NOT retest-loop.
         * gate_failed=true → this is a real qa-gate failure needing a cross-plan
           remediation. Emit
               STAGE_STATUS: stage=2 cluster=<c> verdict=FAIL reason=qa-gate failed on promote PR #<n>
           followed by
               RELEASE_HEALTH: FAIL: stage=2 cluster=<c> reason=needs-cross-plan-Infra-agent: qa-gate failed on promote PR #<n>
           and STOP. Do NOT try to fix it yourself.
         * merged=true (Tide auto-merged the promote PR on green) → emit
               STAGE_STATUS: stage=2 cluster=<c> verdict=PASS reason=promote PR #<n> merged

    3. BOOT DEPLOYED (per cluster) — call
       mcp__leartech-k8s__list_jobs_by_label(cluster=<c>, namespace=<ns>, label=<selector>)
       (or get_job_state with the specific jx-boot Job name) to confirm the jx-boot Job
       for this release ran and succeeded on the cluster. Poll ~60s if it's still Active.
       On completion:
         * succeeded=true → STAGE_STATUS: stage=3 cluster=<c> verdict=PASS reason=jx-boot Job <name> succeeded
         * failed=true → STAGE_STATUS: stage=3 cluster=<c> verdict=FAIL reason=jx-boot Job <name> failed

    4. DEPLOY HEALTHY (per cluster) — call
       mcp__leartech-k8s__deploy_health(service=<s>, namespace=<ns>, cluster=<c>,
       expected_version=<tag>) where <tag> is the RELEASED version from stage 1's
       `release <tag> Succeeded`. Passing it makes the verdict VERSION-AWARE: healthy is
       true only when the released version is what's actually running (>=1 available AND
       observed_version==expected), so a stale/leftover Deployment of an OLD version can no
       longer pass the gate (the release-check Layer-3 gap). If stage 1 yielded no tag, OMIT
       expected_version (version-blind fallback — weaker; ad-hoc checks only). The MCP returns
       healthy, available_replicas, desired_replicas, observed_version, version_match, reason.
       Emit VERBATIM (do NOT re-interpret):
         * healthy=true → STAGE_STATUS: stage=4 cluster=<c> verdict=PASS reason=healthy=true available_replicas=<N> version=<observed_version>
         * healthy=false → STAGE_STATUS: stage=4 cluster=<c> verdict=FAIL reason=healthy=false available_replicas=<N> desired_replicas=<M> <deploy_health reason>

       If the k8s MCP cannot reach a cluster (host returns isError, or deploy_health
       explicitly reports "cluster unreachable"), emit
           STAGE_STATUS: stage=4 cluster=<c> verdict=FAIL reason=k8s MCP could not reach cluster <c>: <error>
       — the aggregator FAILs. Do NOT silently PASS or SKIP an unreachable cluster.

  Two-cluster coverage is REQUIRED for stages 2, 3, 4 (verdict PASS iff both gcp AND az
  report PASS). If your inputs pin ONE cluster (`cluster: gcp|az`), you only need coverage
  for that cluster; treat the other as SKIP with
      STAGE_STATUS: stage=<n> cluster=<other> verdict=SKIP reason=single-cluster run (inputs.cluster=<c>)
  and set the aggregator's required_clusters accordingly via inputs (`clusters: [gcp]`).

  BUDGET FAIL — if stages 1-3 do NOT complete within `budgetMinutes`, end with
      RELEASE_HEALTH: FAIL: stage=<n> cluster=<c> reason=<one-line what stalled>
  (e.g. "release did not fire within 60min", "GCP promote PR #123 stuck non-green").

  NEVER attempt an HTTP GET against a /health URL from this sandbox. The infra sandbox
  cannot reach the ingress; every such probe returns transport error even when the deploy
  is perfectly healthy. This is the specific bug this refactor closes. deploy_health from
  the k8s MCP is the authoritative signal — it reads Deployment status from inside the
  cluster, no HTTP required.

  Params: service, namespace, cluster (default: both gcp+az), optional clusters (list),
  optional budgetMinutes (default 60). The Python aggregator reads STAGE_STATUS + any
  early-exit RELEASE_HEALTH: FAIL from your transcript; it fails closed on any FAIL
  emitted, any missing (stage, cluster) coverage, or any early-exit line.

- INDIVIDUAL SINGLE-STAGE RELEASE-CHECK ACTIONS (2026-08-03): the five actions below
  DECOMPOSE the composed release-health-check into ONE stage each, so a release-check
  can be authored as a multi-step Plan (dependsOn chain) where each step passes/fails
  on its OWN MCP call — and a FAILED step hands the spawned BA Agent stage-specific
  "where + how to remediate" context (the composed verdict can't localize the failure).
  The composed release-health-check ABOVE stays available for single-step use; these
  five are additive.

  Each individual action follows the SAME DETERMINISM CONTRACT as the composed one:
  emit STAGE_STATUS: lines from your MCP results and the Python aggregator computes
  PASS/FAIL. On FAIL, the aggregator ALSO emits a structured BA failure context so a
  subsequently-spawned BA Agent knows WHERE (stage, cluster, MCP) and HOW to start
  remediation. Do NOT improvise — call the MCP, emit the STAGE_STATUS line matching
  what the MCP returned. No httpx probe from this sandbox — the k8s MCP is the
  authoritative in-cluster signal.

  * release-status — stage 1 (per repo, cluster='-'). Poll
    mcp__leartech-jx-release__release_status(repo=mikelear/<service>) until
    released=true AND cross-check the release PipelineRun outcome via
    mcp__leartech-tekton__list_pipelineruns_for_pr / step_status: the release
    PipelineRun MUST have reached Succeeded (not just "tag missing"). Emit
        STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=release <tag> Succeeded
    on success; on the release PipelineRun failing at a specific step emit
        STAGE_STATUS: stage=1 cluster=- verdict=FAIL reason=release PipelineRun <name> failed at step <step>
    Params: service (or repo=mikelear/<service>), optional budgetMinutes (default 60).

  * promote-status — stage 2 opened (per cluster). Poll
    mcp__leartech-jx-release__promote_status(service, clusters=[...]). PASS iff
    jx-promote opened a promote PR on every requested cluster (per-cluster found=true).
    Does NOT check verify/gate/merged (that's verify-gate). For each requested cluster:
      * found=true  → STAGE_STATUS: stage=2 cluster=<c> verdict=PASS reason=promote PR #<n> opened
      * found=false past budget → STAGE_STATUS: stage=2 cluster=<c> verdict=FAIL reason=jx-promote did not open a promote PR
    Params: service, clusters (list, default [gcp,az]) or cluster (single), optional
    budgetMinutes (default 60).

  * verify-gate — stage 2 green + merged (per cluster). Poll
    mcp__leartech-jx-release__promote_status again but require merged=true + all_green=true.
    On gate_failed=true → real qa-gate failure needing cross-plan Infra-agent — emit
        STAGE_STATUS: stage=2 cluster=<c> verdict=FAIL reason=qa-gate failed on promote PR #<n>
        RELEASE_HEALTH: FAIL: stage=2 cluster=<c> reason=needs-cross-plan-Infra-agent: qa-gate failed on promote PR #<n>
    On non-green but NOT gate_failed (flake) — call
    mcp__leartech-jx-release__retest_promote(cluster, pr_number) ONCE, then keep polling.
    On merged=true (Tide auto-merged on green) — emit
        STAGE_STATUS: stage=2 cluster=<c> verdict=PASS reason=promote PR #<n> merged
    Params: service, clusters (list) or cluster, optional budgetMinutes (default 60).

  * boot-status — stage 3 (per cluster). Call
    mcp__leartech-k8s__list_jobs_by_label(cluster=<c>, namespace=<ns>, label=<selector>)
    (or get_job_state on the specific jx-boot Job name) to confirm the jx-boot Job for
    this release ran and succeeded on the cluster. Poll ~60s if it's still Active.
      * succeeded=true → STAGE_STATUS: stage=3 cluster=<c> verdict=PASS reason=jx-boot Job <name> succeeded
      * failed=true    → STAGE_STATUS: stage=3 cluster=<c> verdict=FAIL reason=jx-boot Job <name> failed
    Params: service, namespace, clusters (list) or cluster, optional budgetMinutes.

  * deploy-health — stage 4 (per cluster). Call
    mcp__leartech-k8s__deploy_health(service=<s>, namespace=<ns>, cluster=<c>,
    expected_version=<v>) — pass expected_version when inputs provide a release version
    (`version`/`expectedVersion`), making the verdict VERSION-AWARE: healthy only when the
    released version is what's running (>=1 available AND observed_version==expected), so a
    stale/leftover Deployment of an old version can't pass. Omit it when no version input is
    given (version-blind fallback — weaker). The MCP returns healthy, available_replicas,
    desired_replicas, observed_version, version_match, reason. Emit VERBATIM (do NOT re-interpret):
      * healthy=true  → STAGE_STATUS: stage=4 cluster=<c> verdict=PASS reason=healthy=true available_replicas=<N> version=<observed_version>
      * healthy=false → STAGE_STATUS: stage=4 cluster=<c> verdict=FAIL reason=healthy=false available_replicas=<N> desired_replicas=<M> <deploy_health reason>
    If the k8s MCP cannot reach the cluster, emit
        STAGE_STATUS: stage=4 cluster=<c> verdict=FAIL reason=k8s MCP could not reach cluster <c>: <error>
    NEVER attempt an HTTP GET against a /health URL from this sandbox.
    Params: service, namespace, clusters (list) or cluster, optional version/expectedVersion.

  ON FAIL, the aggregator hands the BA Agent a structured context per this stage:
    * stage             — the stage number (1..4)
    * cluster           — the failing cluster (or '-' for stage 1)
    * mcp               — the MCP tool(s) whose signal drove the FAIL
    * expected          — what a PASS would look like from that MCP
    * mcp_returned      — what the MCP actually returned (from your STAGE_STATUS reason=...)
    * remediation_hint  — a stage-specific "how to start" guidance line
  You do NOT need to render this yourself — the Python aggregator builds it from your
  STAGE_STATUS + the per-stage guidance registered in gate/agent/release_health.py
  (BA_STAGE_GUIDANCE). Your job is simply to (1) call the RIGHT MCP for this action,
  (2) emit the STAGE_STATUS line reflecting what the MCP returned, and (3) do not
  improvise the verdict — the aggregator decides.

Report concisely what you did, which PRs you opened (numbers), and the pass/fail outcome.
"""


def _build_system_prompt() -> str:
    """JX3 calibration + any encoded infra_agent lessons + the infra system prompt."""
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('infra_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(INFRA_SYSTEM_PROMPT)
    return '\n\n---\n\n'.join(blocks)


def _build_options(model: str, max_turns: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        mcp_servers={**build_remote_mcp_servers()},
        allowed_tools=INFRA_ALLOWED_TOOLS,
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
    )


def _task_prompt(action: str, inputs: dict[str, object]) -> str:
    return (
        f'Perform infra action `{action}` with these inputs:\n\n'
        f'{json.dumps(inputs, indent=2)}\n\n'
        f'Follow the procedure for this action in your system prompt. Use the repo-factory '
        f'tool for any scaffolding. Report the PRs you opened and the outcome.'
    )


def _resolve_required_clusters(inputs: dict[str, object]) -> tuple[str, ...]:
    """Determine which clusters the aggregator requires PASS coverage on.

    Precedence:
      * ``inputs['clusters']`` (list of strings) — explicit set of clusters.
      * ``inputs['cluster']`` (single string) — single-cluster plan step;
        aggregator requires PASS for that cluster only.
      * Default: both ``gcp`` + ``az`` (from :data:`release_health.DEFAULT_CLUSTERS`).

    Malformed inputs fall back to the default rather than crashing — the
    aggregator's fail-closed semantics still apply on missing coverage.
    """
    raw_list = inputs.get('clusters')
    if isinstance(raw_list, list) and raw_list:
        out = tuple(str(c).strip() for c in raw_list if str(c).strip())
        if out:
            return out
    raw_one = inputs.get('cluster')
    if isinstance(raw_one, str) and raw_one.strip():
        return (raw_one.strip(),)
    return tuple(DEFAULT_CLUSTERS)


def _health_check_verdict(
    inputs: dict[str, object],
    transcript: str,
    *,
    aggregator: Callable[..., ProbeResult] = compute_release_health,
) -> ProbeResult:
    """Compute the DETERMINISTIC release-health-check verdict.

    Contract:
      * The verdict is a function of the LLM's ``STAGE_STATUS:`` lines +
        any early-exit ``RELEASE_HEALTH: FAIL`` line — not of free-form
        narration. See :func:`gate.agent.release_health.compute_release_health`
        for the full rules.
      * No httpx probe, no kubectl, no ingress /health GET — the k8s MCP's
        ``deploy_health`` is the authoritative stage-4 signal, called by
        the LLM in-cluster (see the release-health-check procedure in the
        system prompt).
      * Missing (stage, cluster) coverage is a FAIL (no PASS-by-silence).
    """
    required_clusters = _resolve_required_clusters(inputs)
    return aggregator(transcript, required_clusters=required_clusters)


def _stage_action_verdict(
    action: str,
    inputs: dict[str, object],
    transcript: str,
    *,
    aggregator: Callable[..., StageActionResult] | None = None,
) -> StageActionResult:
    """Compute the DETERMINISTIC verdict for one individual single-stage action.

    Contract (same shape as :func:`_health_check_verdict` but scoped to one stage):
      * The verdict is a function of the LLM's STAGE_STATUS lines + any
        early-exit RELEASE_HEALTH: FAIL — never free-form narration.
      * ``inputs['cluster']`` or ``inputs['clusters']`` pins the required
        cluster set for this stage. Stage 1 (``release-status``) is per-repo
        and ignores those params.
      * On FAIL, the ``StageActionResult.ba_failure_context`` field carries
        the structured stage-specific brief the escalation hands to the
        BA Agent. See :data:`gate.agent.release_health.BA_STAGE_GUIDANCE`
        for the per-stage guidance content.

    The ``aggregator`` seam mirrors :func:`_health_check_verdict` — tests
    substitute a fake aggregator to isolate the wiring from the parser.
    Default resolves to the registered aggregator for ``action`` via
    :data:`gate.agent.release_health.INDIVIDUAL_STAGE_ACTIONS`.
    """
    spec = INDIVIDUAL_STAGE_ACTIONS.get(action)
    if spec is None:
        # Should not happen — callers gate on is_individual_stage_action.
        # Fail closed so an unknown action name never accidentally PASSes.
        return StageActionResult(
            verdict='FAIL',
            reason=f'unknown individual-stage action {action!r}',
            stage=0,
            failing_stage=None,
            failing_cluster=None,
            ba_failure_context={
                'action': action,
                'stage': 0,
                'cluster': '-',
                'mcp': 'unknown',
                'expected': 'a registered individual-stage action name',
                'mcp_returned': None,
                'reason': f'unknown action {action!r}',
                'remediation_hint': (
                    'Plan author authored a step targeting an unregistered infra action. '
                    'Add the action to INDIVIDUAL_STAGE_ACTIONS + BA_STAGE_GUIDANCE OR '
                    'change the plan step to a registered action.'
                ),
            },
        )
    stage_num = int(spec['stage'])
    resolved_aggregator = aggregator if aggregator is not None else spec['aggregator']

    # Cluster resolution: stage 1 is per-repo (aggregator handles the
    # cluster='-' default); other stages honour inputs.clusters/cluster.
    if stage_num == 1:
        required_clusters = None  # aggregator picks ('-',)
    else:
        required_clusters = _resolve_required_clusters(inputs)

    if required_clusters is None:
        return resolved_aggregator(transcript)
    return resolved_aggregator(transcript, required_clusters=required_clusters)


async def run_infra_task(
    action: str,
    inputs: dict[str, object],
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> int:
    """Drive the infra agent through one action. Returns the process exit code."""
    # ── TEST-MODE short-circuit ────────────────────────────────────────────
    # A plan step may set ``inputs.testMode`` to skip the LLM/SDK loop
    # entirely. ONLY honored when LEARTECH_AGENT_TEST_MODE_ALLOWED=true is
    # set — otherwise the directive is IGNORED. Placed BEFORE the API-key
    # check because test-mode's whole point is to skip the LLM. The infra
    # agent's PR-backed actions (register-source-config, smoke-pr) don't
    # call open_pr directly — the repo-factory MCP handles that — so we
    # don't build a manual open_pr_args here; the MCP's own test-mode
    # coverage exercises those flows via a different plan step.
    test_mode_spec = parse_test_mode(inputs)
    if test_mode_spec is not None:
        obslog.info(
            'run_start',
            f'infra agent action={action} (test-mode)',
            logger='infra',
            action=action,
            test_mode=True,
        )
        exit_code = await run_test_mode(test_mode_spec, open_pr_args=None)
        obslog.info(
            'run_end',
            f'infra agent action={action} done (test-mode)',
            logger='infra',
            action=action,
            exit_code=exit_code,
            test_mode=True,
        )
        return exit_code

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return 2

    obslog.info('run_start', f'infra agent action={action}', logger='infra', action=action)
    options = _build_options(model, max_turns)
    prompt = _task_prompt(action, inputs)

    exit_code = 0
    transcript: list[str] = []
    try:
        # Drain the iterator fully (return inside `async for` breaks the SDK's generator
        # shutdown — see gate/agent/main.py).
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                    elif isinstance(block, ThinkingBlock | ToolResultBlock):
                        pass  # internal reasoning / tool results — surface the synthesis instead
            elif isinstance(message, ResultMessage):
                exit_code = 1 if message.is_error else 0
    except Exception as exc:
        obslog.error('run_end', f'infra agent crashed: {exc}', logger='infra', action=action, exit_code=1)
        raise

    # Judgment actions must drive the exit code from the OUTCOME, not just SDK errors.
    # release-health-check: verdict is DETERMINISTIC — the Python aggregator reads the LLM's
    # per-stage STAGE_STATUS lines + any early-exit RELEASE_HEALTH: FAIL line and computes
    # PASS/FAIL from them. A merged PR / undeployed release / unhealthy Deployment must
    # never read as healthy (closes the historical false-success where exit_code tracked
    # only is_error, AND the "kubectl unavailable => FAIL" / "curled once and PASSED"
    # cluster-asymmetric improvisation of the httpx probe).
    if action == 'release-health-check':
        result = _health_check_verdict(
            inputs,
            '\n'.join(transcript),
        )
        # Exit code: only an explicit PASS survives; FAIL forces 1.
        exit_code = 0 if result.verdict == 'PASS' else 1
        obslog.info(
            'health_verdict',
            f'release-health-check verdict={result.verdict}',
            logger='infra',
            action=action,
            verdict=result.verdict,
            reason=result.reason,
            failing_stage=result.failing_stage,
            failing_cluster=result.failing_cluster,
            stages=[s.as_dict() for s in result.stages],
            exit_code=exit_code,
        )

    # Individual single-stage actions (release-status / promote-status /
    # verify-gate / boot-status / deploy-health) — same deterministic
    # verdict shape as release-health-check but scoped to ONE stage.
    # On FAIL, the ba_failure_context is what the escalation carries to
    # the spawned BA Agent so the BA knows WHERE + HOW to remediate the
    # stage. Same fail-closed exit-code semantics — PASS → 0, else 1.
    elif is_individual_stage_action(action):
        stage_result = _stage_action_verdict(action, inputs, '\n'.join(transcript))
        exit_code = 0 if stage_result.verdict == 'PASS' else 1
        obslog.info(
            'stage_verdict',
            f'{action} verdict={stage_result.verdict}',
            logger='infra',
            action=action,
            stage=stage_result.stage,
            verdict=stage_result.verdict,
            reason=stage_result.reason,
            failing_stage=stage_result.failing_stage,
            failing_cluster=stage_result.failing_cluster,
            stages=[s.as_dict() for s in stage_result.stages],
            ba_failure_context=stage_result.ba_failure_context,
            exit_code=exit_code,
        )

    obslog.info('run_end', f'infra agent action={action} done', logger='infra', action=action, exit_code=exit_code)
    return exit_code


# The controller inlines the Plan step's `inputs` JSON into this env var (jobspawn.go);
# an entrypoint-override AgentType gets NO CLI args, so inputs arrive here, not via flags.
INPUTS_ENV = 'LEARTECH_INITIATIVE_YAML'


@click.command()
@click.option('--action', default=None, help='Infra action; defaults to inputs["action"].')
@click.option('--inputs', 'inputs_opt', default=None, help=f'JSON inputs; defaults to ${INPUTS_ENV}.')
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option('--max-turns', default=DEFAULT_MAX_TURNS, type=int, show_default=True, help='Max agent turns.')
def main(action: str | None, inputs_opt: str | None, model: str, max_turns: int) -> None:
    """Run the infra agent for one action (the entrypoint an infra AgentType spawns).

    Inputs default to ``$LEARTECH_INITIATIVE_YAML`` (the controller's contract — the Plan
    step's inputs JSON, which carries ``action`` + params); ``--inputs``/``--action``
    override for local use.
    """
    raw = inputs_opt if inputs_opt is not None else os.environ.get(INPUTS_ENV, '')
    if not raw.strip():
        raise click.BadParameter(f'no inputs: set ${INPUTS_ENV} or --inputs')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f'inputs must be valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise click.BadParameter('inputs must be a JSON object')
    act = action or parsed.get('action')
    if not isinstance(act, str) or not act:
        raise click.BadParameter('no action: set --action or inputs["action"]')
    params = {k: v for k, v in parsed.items() if k != 'action'}
    sys.exit(asyncio.run(run_infra_task(act, params, model=model, max_turns=max_turns)))


if __name__ == '__main__':
    main()
