---
id: cross-cluster-failure-needs-infra-diagnosis
title: When end2end-* fails on one cluster but passes on the other, run kubectl on the failing cluster BEFORE iterating or retriggering
captured_at: 2026-05-06T15:30:00Z
source:
  type: agent_run
  reference: pr_10_webcoder_ui_initiative_detail_cluster_pressure
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
slipped_past_criteria:
  - test_pr_checks_green
proposed_criterion: |
  test_end2end_cluster_failure_diagnosed — when end2end-* fails on one
  cluster but passes on the other, agent's verdict must reference a
  kubectl-based diagnosis of the failing cluster's preview namespace.
  Future build (slice G) — see project_slice_g_cluster_diagnosis.md.
---

## The pattern

**`end2end-*` fails on ONE cluster but passes on the OTHER** is a strong
diagnostic signal: the failure is by definition NOT in your code (your
code went through both). It's almost always one of:

- Cluster node capacity (Pending pods on the failing cluster)
- Image pull errors (one cluster's registry mirror is down)
- PVC binding failure (storage on one cluster)
- Hydra session race (more often on AZ — memory-DSN drops state)

Retriggering the failing cluster's check does NOT help any of these. It
just re-runs the test against the same broken infrastructure.

## Procedure (mandatory before any retrigger or code-change response)

When you see end2end-* asymmetry — one cluster green, one failing — run
this **before** posting `/test <check>` or proposing a code fix:

```sh
# Identify the cluster context
# - AZ cluster:  modern-burro
# - GCP cluster: gke_product-first_us-east1-b_tf-jx-usable-bird

# Construct the preview namespace
NAMESPACE="jx-mikelear-${REPO}-pr-${PR_NUMBER}"
# (e.g. jx-mikelear-webcoder-ui-pr-10)

# Get pod state on the FAILING cluster
kubectl --context=<failing-context> -n $NAMESPACE get pods
```

Then **classify the result**:

### Verdict 1 — All preview-* pods Running

The failure is in the test code or a transient network race — proceed
with normal investigation (read failure logs, check the spec, etc.).

### Verdict 2 — One or more preview-* pods Pending / CrashLoopBackOff / ImagePullBackOff

**This is INFRA, not code.** Specifically:

- `Pending` for >5 min usually means cluster capacity (memory or CPU) —
  describe the pod's events to confirm: `kubectl describe pod <name>`
- `CrashLoopBackOff` means the pod's image is broken — outside your diff
  unless your initiative changed images
- `ImagePullBackOff` means registry/auth issue — outside your diff

In all infra cases, the agent must:

1. **NOT retrigger** the failing cluster's check. It will fail again.
2. **NOT iterate on code**. The code is fine; the cluster is broken.
3. **Document the cluster-side cause in the sticky comment**, e.g.:

   > "az/end2end and az/end2end-ui failing because
   > `preview-webcoder-service` is stuck Pending on Modern-Burro
   > (cluster capacity). 5 of 6 preview pods healthy. Outside this PR's
   > diff. GCP cluster is fully green — substantive work validated.
   > Awaiting cluster recovery before merge or human override."

4. **Mark the PR as ready** with the GCP-side green verdict, since the
   substantive work has been validated on at least one cluster. Let
   humans decide whether to merge with the AZ-side noise documented or
   wait for cluster recovery.

5. **Stop the loop**. Don't keep watching the AZ checks.

## What this is NOT

- Not "ignore failures" — failures must always be investigated
- Not "retrigger blindly" — retrigger only when verdict 1 (transient race)
- Not a substitute for `read-failure-detail-before-fixing` — read the
  failure logs first; the cluster check supplements, doesn't replace

## How this lesson surfaced

PR #10 (`webcoder-ui-add-initiative-detail-page`, 2026-05-06):

- Substantive work landed cleanly (component + spec + routes + Playwright)
- GCP cluster: 10/10 green ✓
- AZ cluster: end2end and end2end-ui failed
- Agent retriggered both. They failed again identically.
- Mike checked kubectl directly:
  ```
  preview-webcoder-service-5b79949f5d-9gppc  0/1  Pending  46m
  preview-webcoder-service-5b79949f5d-4k6tx  0/1  Pending  13m
  ```
- Modern-Burro had cluster memory pressure preventing webcoder-service
  scheduling. Other 5 preview services were healthy.
- **The agent had no way to know this from the API alone** — it only saw
  "test_pr_checks_green failing" and "az/end2end Pipeline failed". The
  diagnosis required local kubectl access, which the agent has via Bash.

Without this lesson, the agent kept retriggering AZ checks until
exhausting its turn budget — a deterministic spiral on broken
infrastructure.

## Why `calibration`, not `criteria_gap`

Two-mode behaviour now applies as of 2026-05-07 architectural shift:

### Mode A — laptop CLI (current dogfood, no runner)

The agent is invoked directly from `make initiative` with no runner
wrapper. Use the kubectl procedure above to diagnose cross-cluster
asymmetry. The agent has Bash + cluster contexts; it can do this itself.

### Mode B — cluster-deployed service (post phase-B service shape)

When the agent is invoked via `POST /initiatives` (from CRD-spawned Job,
Tekton task, or direct HTTP), **the runner has already done a cluster
pre-flight check**. If the runner invoked the agent at all, the cluster
is sufficient. If asymmetric end2end-* failures appear during the run,
they're either (a) transient — agent may retrigger once — or (b) something
that emerged after the pre-flight, in which case the agent should:

1. NOT run kubectl (no cluster credentials in the agent service pod)
2. Stop the iteration loop
3. Return verdict `infra_emerged_during_run` with a sticky comment
   pointing the human to the runner's pre-flight log

The runner Job (webCoder territory) owns the cluster-side observability;
the agent stays small and trusts the verdict it was invoked with.

## Trigger to upgrade

Mode B activates when the service-deploy phase-B work lands — see
`project_service_deploy_phase_b.md`. The runner Job's pre-flight checks
are scoped as part of webCoder's K8s Job + CRD work, not automated-agent.
Slice G as originally framed (MCP tool + criterion *inside* the agent)
is dissolved; cluster diagnosis lives in the runner.
