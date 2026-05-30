---
id: consult-reference-cluster-before-iterating
title: Single-cluster check failure must trigger a check on the other cluster BEFORE the agent iterates on code — two clusters is a built-in fact-check
applies_to:
  - initiative_agent
  - orchestrator_agent
status: open
captured_at: 2026-05-30T17:00:00Z
source:
  type: agent_run
  reference: leartech-orchestrator init #6 PR #11 release pipeline — AZ build-python-test flake; GCP passed same test same commit
  observer: mike.lear@leartech
  latency_to_capture: minutes
category: diagnostic_reflex
slipped_past_criteria: []
proposed_criterion: |
  Before the agent iterates on code in response to a single-cluster check
  failure, it MUST query the corresponding check on the other cluster. If
  the other cluster's result is GREEN on the same commit, the failure is
  cluster asymmetry — DO NOT iterate the code; surface the asymmetry and
  retrigger (empty commit, /retest, or skip). If the other cluster is
  still running, wait for the second opinion before iterating.

  Concrete enforcement: a pre-iteration gate. When the agent's
  decision loop receives "check X failed on cluster Y", the criterion
  checks "did same check pass on the other cluster?" If YES → exit the
  iteration cycle with `kind=cluster_asymmetry_detected`. If NO →
  proceed to iterate.
---

## The principle

The leartech platform runs every check on TWO clusters (AZ + GCP). That's
not just deploy redundancy — **it's a built-in fact-check for every CI
signal**. Any agent reasoning about a check failure should treat the
second cluster's result as a mandatory second-opinion before iterating
on the code.

## The decision table

```
check_X fails on cluster Y    →    consult cluster Z's same check_X
```

| Other cluster says | Action |
|---|---|
| ✅ Same check passed | **Cluster asymmetry confirmed.** Don't iterate code. Retrigger (empty commit, /retest). Optionally log a finding for the cluster-team |
| ❌ Same check failed | **Real bug.** Diagnose + fix the code. Iterate. |
| ⏳ Same check still running | Wait. Don't pre-emptively iterate on the cluster Y signal alone |
| ⚠️ Same check missing/not configured | Configuration asymmetry — fix that first |

## The asymmetry sources to expect

Cluster asymmetry isn't pathological; it's a property of running on real
heterogeneous infrastructure:

- **Build node capacity** (AZ build pool is smaller / has different
  concurrency) → async-timing flakes
- **AI reviewer non-determinism** (same model, different sampling) →
  false-positive critical findings on one side
- **Regional API quotas** (Google Vision used on GCP not AZ, etc.) →
  one side hits rate-limit when other doesn't
- **Network paths to upstream registries** → image pull timeouts asymmetric
- **DNS propagation** (preview subdomain only configured on one cluster)
  → external probes fail asymmetrically

None of these are bugs in the PR's code. Iterating on the code chases
phantoms.

## The bug that surfaced this lesson

`leartech-orchestrator` init #6 (PR #11, merged 2026-05-30) release
pipeline:

- AZ release `step-build-python-test`: failed with `test_plans_router_job_spawn.py::test_submit_plan_spawns_job_and_marks_row_running AssertionError: 'submitted' == 'running'`
- GCP release SAME step SAME commit: passed (100% coverage on the same test file)

The agent had already applied this principle on the same PR's
ai-review (AZ claude flagged 3 false positives, GCP claude 100/100 —
agent quoted `when-end2end-fails-on-one-cluster-but-passes-on-the-other`
and chose not to iterate). The new lesson generalises that reflex to
EVERY check class — tests, build steps, helm renders, security
scans — not just ai-review.

The right response was the empty-commit retest. It passed.

## Cross-agent application (per cross-agent-retrospect-routing)

| Agent | Where to wire this |
|---|---|
| `initiative_agent` | Before iterating in response to a failing PR gate, check the matching gate on the other cluster |
| `orchestrator_agent` | When watching a plan's PRs, treat the dual-cluster check pair as a single composite signal. Asymmetry is its own decision class — `kind=cluster_asymmetry_detected` in `plan_decisions` |
| `infrastructure_agent` (future) | Repeated asymmetry is a cluster-health signal. Aggregate occurrences; flag for capacity / driver investigation |

## Related lessons

- `cross-cluster-failure-needs-infra-diagnosis` — what to do AFTER you've
  confirmed asymmetry (kubectl-inspect the failing cluster to understand
  why)
- `cross-tier-failure-asymmetry-is-diagnostic-signal` — the e2e/e2e-ui
  cross-tier version of the same principle
- `when-end2end-fails-on-one-cluster-but-passes-on-the-other` — the
  earlier ai-review-only framing this lesson generalises
