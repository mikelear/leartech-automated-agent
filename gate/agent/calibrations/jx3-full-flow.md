<!--
Mirror of leartech-automated-agent/gate/agent/calibrations/jx3-full-flow.md
and leartech-orchestrator/gate/orch/calibrations/jx3-full-flow.md.

Until repo-specific addenda are needed, KEEP THESE FILES IDENTICAL. If you
find yourself making them differ, open a follow-up initiative to factor out
the shared core into a small package both repos depend on.
-->

# JX3 full-flow calibration

You are one slice of a much longer chain. This doc grounds you in the chain
so you don't conflate "my step finished" with "the change is shipped" and
don't conflate "single-cluster check failed" with "the code is broken."

## A. The rough shape (NOT a hardcoded list)

```
dev-agent push → PR checks → Lighthouse approve → Tide merge → release
   → cluster-specific image + version → jx-promote opens GitOps PR
   → GitOps checks → Lighthouse approve → Tide merge → jx-boot reconciles
   → pods roll → /healthz green
```

That's the shape — about ten stages from "I committed" to "pods are running
the new code." It is **rough**: the actual stages vary per repo, per
language, per pipeline-catalog version. Treat the diagram as priors, not
truth. Section B below tells you how to derive the truth for the repo
you're working in.

Things that follow from the shape:

- The Claude Agent SDK `agent_terminal` event you emit (when the
  initiative loop exits) is **stage 1 of ~10**. The dev-agent thinks
  `agent_terminal == done`; the orchestrator thinks `agent_terminal ==
  step_complete`. Neither means the change is live in the cluster. Done
  means **pods are running the new code in the cluster**.
- The `approved` label is applied by **Lighthouse's approve plugin**
  watching ALL required checks together. It is NOT applied by the
  ai-review worker — `ai-review` is one check that feeds into the
  approve plugin's decision, not the decision itself. It is also NOT a
  human typing `/approve` in the auto path; that's a manual override.
  (Memory cross-ref: `feedback_ai_review_does_not_apply_approved_label`.)
- The `do-not-merge/hold` label is the dev-agent's default safety. It
  blocks Tide regardless of approval. The orchestrator removes it for
  multi-init plans via `auto_unhold`. Standalone dev-agent runs keep it
  so a human can inspect before merge. Never `/hold cancel` yourself —
  that's a human action.

## B. How to find the truth for THIS repo

Before assuming anything about the flow, read THREE files in the consumer
repo:

1. **`.lighthouse/jenkins-x/pullrequest.yaml`** — defines the PR-time
   stages. Each Tekton task in there is one check the PR has to pass.
   A `uses: <pipeline-catalog>@version` reference points at the
   underlying step definition in `leartech-pipeline-catalog`.
2. **`.lighthouse/jenkins-x/release.yaml`** — defines the post-merge
   release pipeline: build, push to the cluster's image registry,
   `jx-release-version`, and the `jx-promote` call that opens the
   GitOps PR against the env repo.
3. **`.lighthouse/triggers.yaml`** — defines which path globs trigger
   which pipelines. Some changes don't run all PR gates (e.g.
   `leartech-dockerfiles` has no PR-time gates on `<image>/app/*`
   changes — only on Dockerfile changes). If you can't find a check
   you expect, this file probably explains it.

For each `uses:` reference, pull the underlying YAML to see what the step
actually does:

```sh
gh api repos/leartech/leartech-pipeline-catalog/contents/<path>?ref=<version> \
  --jq '.content' | base64 -d
```

The catalog tasks are the **source of truth** for what runs in the pipeline.
Consult them before guessing "what does the lint step actually invoke."

## C. Local-test checklist

Convert the YAML readout into a local-test plan:

- `ruff check` + `ruff format --check` — mirrors the `lint` task.
- `pytest -q --no-cov` (or `npm test`, `go test ./...`, `ng test`, etc.) —
  mirrors the unit-test step.
- `uv build` (or `npm run build`, `go build ./...`) — mirrors the
  kaniko build's success criterion. Doesn't catch base-image issues but
  catches build-config issues.
- Pre-push hook OR a `make verify` target SHOULD encapsulate the above.
  If the repo has neither, propose adding one as a follow-up initiative
  — don't fix it inline unless the current initiative asks for it.

**A green local gate is necessary but not sufficient.** Cluster-side gates
— preview deploy, image scan, security scan, ai-review, end2end — cannot
all be mirrored locally. Pushing on a hunch and watching what red lights up
is acceptable, but only AFTER the locally-runnable checks are clean.

### C-pre-1. Async tests that touch a background task

When a pytest async test exercises code that runs a background task
(reconciler, plan-runner SDK loop, watcher), synchronise via
`asyncio.Event` — NOT via `await asyncio.sleep(0.01)`:

```python
# WRONG — sleep-based race
asyncio.create_task(reconciler_loop())
await asyncio.sleep(0.05)
assert state == 'expected'

# RIGHT — Event-based sync
ready = asyncio.Event()
asyncio.create_task(reconciler_loop(on_ready=ready.set))
await asyncio.wait_for(ready.wait(), timeout=2.0)
assert state == 'expected'
```

GCP cluster nodes run tests under more contention than AZ. Sleep
intervals that pass locally + on AZ flake on GCP, producing
cluster-asymmetric release-pipeline failures. Memory cross-ref:
`feedback_async_tests_need_event_not_sleep` — hit ≥4× by 2026-06-08.

Also: tests using aiosqlite + a background reconciler can hit
`(sqlite3.OperationalError) cannot commit transaction - SQL statements
in progress` under contention. Either gate the reconciler behind a
`LEARTECH_RECONCILER_DISABLED=1` env var the test sets, OR ensure the
test session and the reconciler session aren't contending on the same
connection.

### C-pre-2. Adding secret references to charts

When adding a new `secretKeyRef` to a deployment's `env:` block, ALWAYS
think about preview namespaces first:

- Preview namespaces (e.g. `jx-<repo>-pr-<N>`) are ephemeral and
  typically DON'T have the cluster's app secrets — they get their own
  minimal set.
- A mandatory `secretKeyRef` to a secret that doesn't exist in the
  preview namespace causes the pod to fail with
  `CreateContainerConfigError` → rollout never completes → preview
  deploy times out with `Error: UPGRADE FAILED: context deadline
  exceeded`.
- **Default**: mark new `secretKeyRef`s `optional: true` unless the
  secret is genuinely required for the pod to do anything useful. Code
  consuming `os.environ.get("FOO")` should null-check anyway.
- If the env var is genuinely mandatory: either (a) replicate the
  secret into preview namespaces via ExternalSecret, OR (b) make the
  chart support skipping the env block when the secret isn't
  configured (`{{- if .Values.foo.secretName }}` gate).

Memory: PR #36 (wire-anthropic-key-api-pod, 2026-06-08) — agent added a
mandatory `secretKeyRef`, preview pod stuck `CreateContainerConfigError`,
build timed out. Fix was six lines: add `optional: true`.

## D. Chatops command reference

All comments are case-sensitive and posted via
`gh pr comment <N> --body "<command>"`.

| Command | Effect |
|---|---|
| `/test <name>` | Re-run ONE check. Example: `/test pr`, `/test ai-code-review`. Both clusters' keepers listen; each runs ITS cluster's task. |
| `/retest` (no arg) | Re-run ALL failed checks on the PR. |
| `/retest <name>` | **NOT VALID.** Silently ignored — no pipelinerun fires. (Memory: `feedback_lighthouse_retest_syntax`.) |
| `/hold` | Add `do-not-merge/hold` label. Dev-agent default safety. |
| `/hold cancel` | Remove the hold label. Orch posts this when `auto_unhold: true`. **The dev-agent never posts this.** |
| `/approve` | (From an OWNER) → Lighthouse approve plugin adds `approved` label. |
| `/lgtm` | (From an OWNER) → Lighthouse adds `lgtm` label. Separate gate from `approved`. |
| `/ok-to-test` | (From an OWNER) → for repos with branch-protection on outsider PRs. |

**Critical syntax gotcha — `/test <name>` strips the cluster prefix.** The
displayed status name is `<cluster>/<check>` (e.g. `gcp/pr`, `az/lint`)
but the chatops command is `/test <check>`. `/test gcp/pr` does NOT fire
any pipelinerun — it's silently dropped by Lighthouse. Use `/test pr`
and both cluster keepers will react.

## E. Cluster + registry + version variability

- Each cluster has its OWN image registry — GCP uses GAR, AZ uses ACR.
- Each cluster's `release.yaml` pushes to that cluster's registry. The
  same git tag produces two different image tags in two different
  registries.
- `jx-release-version` runs independently per cluster. Races between
  clusters can produce different semver numbers for the same merge —
  this is why git tags are cluster-suffixed but chart/registry tags
  are not. Source of truth:
  `~/leartech/hub/status/multi-cluster-release-pattern.md`.
- **Single-cluster failure is a decision class, not a code class.**
  When one cluster's check fails but the other cluster passes for the
  same commit, the FIRST hypothesis is infra asymmetry — node
  pressure, registry blip, preview pod scheduling. Verify against the
  passing cluster before proposing a code fix. Memory cross-ref:
  `feedback_consult_reference_cluster_before_iterating_on_single_cluster_failure`.

The same applies to single-cluster successes during release: if `gcp/release`
goes green but `az/release` is still pending or failed, the change is NOT
yet live in AZ. The promote PR + GitOps reconcile happens once per cluster.

## End calibration
