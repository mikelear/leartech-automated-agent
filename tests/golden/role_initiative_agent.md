## Calibrations from past runs

_The following lessons were learned from real agent runs and have been canonicalised. They take precedence when in conflict with general guidance below._

### When adding UI surface, the agent must add a Playwright spec — convention, not gate-dependent

When an initiative adds new UI surface to any leartech-angular-service-template
repo (a new component / new route / new `data-testid` anchors), the agent
**must add a corresponding `end2end-ui/*.spec.ts` covering that surface** — even
if the gate's coverage-detection criterion stays silent.

This is a leartech convention, not a per-repo accident. Apply it across every
angular-template consumer (auth-ui, webcoder-ui, future-lending-ui,
next-generation-lending-website, etc.) regardless of which repo's per-repo
criteria are wired today.

## How this surfaced

PR #7 on `mikelear/webcoder-ui` (the dogfood demo). The agent added the new
About component + route + 4 `data-testid` anchors but committed no Playwright
spec. The gate run showed `test_ui_changes_have_playwright_coverage` SKIPPED
(the criterion is currently scoped to auth-ui only — see
`per-repo-criteria-must-be-shareable-across-template-consumers` lesson). With
no failure signal, the agent posted "Ready for client review" without writing
a spec.

The result was a vacuous CI pass: catalog's `gcp/end2end-ui` ran the existing
5 webcoder-ui specs (none referencing /about); `test_ui_changes_have_playwright_coverage`
didn't run. Real coverage of the new feature was zero.

Mike's observation captured this perfectly:

> "I thought with auth-ui though we noticed the gap but then added a test to
>  cover the gap... why hasn't that happened here?"

The structural answer: criterion was scoped wrong. The behavioural answer:
agent treated gate-silence as gate-approval. Both are real; this lesson is
the behavioural fix.

## Procedure

After step 4 (commit + push) and BEFORE running the gate:

1. **Inventory the diff for new UI surface**:
   - new `*.component.ts` files
   - new `data-testid="..."` attributes in `*.html` files
   - new route paths (`{ path: 'foo', ... }` in routes file)
2. **If any new UI surface exists**, check `end2end-ui/*.spec.ts` for at least
   one spec referencing each item:
   - Component selector (e.g. `app-about`) appears in a `locator(...)` call
   - Each `data-testid` appears in a `getByTestId(...)` or `data-testid=`
     selector call
   - The route appears in a `page.goto(...)` call
3. **If any new surface is unreferenced**, draft a spec following the
   conventions in this repo's existing `end2end-ui/` (use Read/Glob to study
   them; mirror imports, describe blocks, selector style, waitFor patterns).
   Commit it as a separate commit (`test(<feature>): add Playwright spec for
   <feature>`).
4. THEN run the gate.

This is defence-in-depth. The gate's `test_ui_changes_have_playwright_coverage`
will *also* check this when it's structurally available for the consumer repo
— but the agent must not depend on the gate firing. Adding the spec is the
right thing regardless.

## Why "even if the gate is silent"

The gate's silence on a given criterion means one of:
- The criterion isn't yet wired for this consumer repo (today's PR #7 case)
- The criterion is in a tier excluded by `gate_marks` filter
- The criterion has a bug or false-skip path

In all three cases, agent silence + gate silence = real coverage gap shipped.
The agent's role is to apply leartech conventions; the gate's role is to verify.
If the gate misses, the agent shouldn't.

## Pairs with the structural fix

This calibration covers the case until
`per-repo-criteria-must-be-shareable-across-template-consumers` lands. After
the structural refactor, both lessons reinforce each other:
- **Gate side**: criterion runs on every angular-template consumer
- **Agent side**: agent applies the convention proactively even before checking
  the gate

If either layer fails, the other catches it.

### Agent-authored PRs must post `/hold` to prevent auto-merge before human review

When an initiative-agent opens a PR, it must immediately post `/hold` as a PR
comment to block auto-merge:

    gh pr comment <pr> -R <repo> --body "/hold"

## Why this matters

PR #39 (the AI-coverage-scanner demo) auto-merged into `main` once all gate checks
went green — **without any human reviewer ever seeing the change**. The catalog's
auto-merge logic doesn't distinguish "agent-authored" from "renovate-authored" from
"human-authored"; once green, all are merge-eligible.

For agent runs that's a real governance gap. The agent's "Ready for client review"
sticky is the *agent's verdict*, not approval. Auto-merging on the agent's own
verdict creates a closed loop with no human in it.

## How `/hold` works

Lighthouse Keeper (the JX3 merge controller) honours chatops commands:

- `/hold` — sets the `do-not-merge/hold` label, blocks auto-merge regardless of checks
- `/hold cancel` — clears the hold, lets auto-merge proceed

The hold stays in place until cleared. Human reviewers cancel it after review.

## Hard rules for the agent

1. **Always post `/hold`** as one of the first comments after `gh pr create`.
2. **Never post `/hold cancel`** — only humans cancel the merge hold.
3. **Don't apologise for the hold in the PR description** — it's the safe default,
   not an exception. State plainly: "Held pending human review (`/hold` posted)."
4. The "Ready for client review" sticky is still posted when gate is green; the
   sticky's job is to summarise *what to review*, not to clear the merge gate.

## Mitigation if the hold was missed (e.g. PR #39)

PR #39 already merged. Going forward:

- For *future* agent PRs, the system prompt now mandates `/hold` (encoded in
  `INITIATIVE_SYSTEM_PROMPT` step 5).
- Catalog-side: a longer-term fix is to add a `requires-human-review` label that
  Keeper rejects for auto-merge by default — only removed by human action. That's
  out-of-scope for this lesson; raise as a `leartech-pipeline-catalog` issue.
- Org-policy: consider adding GitHub branch-protection rules requiring at least
  one human reviewer (different login from the PR author) on `main`. That'd
  belt-and-braces the agent's hold convention.

### Block on `gh pr checks --watch`, then chatops-retrigger if a check stalls > 15 min

When you've pushed and need to wait for the Tekton pipeline to settle:

**Don't use ScheduleWakeup or sleep loops.** Use `gh pr checks` in watch mode — it's a
single Bash call that blocks until every required check reaches a terminal state, with
no reliance on harness-specific scheduling features:

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

`--watch` polls until terminal; `--required` ignores skipped/optional checks; `--interval 30`
is gentle on the API. Wrap with `timeout 900` so a stuck check doesn't hang the agent
indefinitely (15 min ceiling — long enough to catch a real run, short enough to act on
a stall).

If `timeout` fires (exit 124), the pipeline is wedged. Recovery flow:

1. Confirm the stall is real with `mcp__leartech-pipeline__list_pr_checks`. Pending-with-pod-RUNNING
   is normal; pending-no-pod for >15 min usually means the queue is wedged.
2. Post `/test <check-name>` (or `/retest`) as a PR comment via `gh pr comment` to retrigger:

       gh pr comment <pr> -R <repo> --body "/test test"

3. Resume the watch with another `timeout 900 gh pr checks ... --watch`.

This pattern was discovered when `az/test` stalled ~35 min on PR #37; chatops retrigger
unblocked it within a few minutes. The original implementation used `ScheduleWakeup`
which works only because the Agent SDK's underlying CLI harness happens to honour it —
not a stable contract. `gh pr checks --watch` is correct in any context (laptop CLI,
cluster pod, future pub/sub world).

**v2 direction** (see `project_v2_pubsub_direction.md`): the right primitive is
`subscribe pr.checks.terminal{repo, pr}` over a message bus — no polling, no timeouts.
For now, watch+retrigger is the portable stop-gap.

### Cite specific failing criteria when explaining a fix

When proposing or applying a fix, **always cite the specific criterion name(s)** the
fix is responding to:

- "Fixing `test_coverage_meets_threshold[gcp]`: home.component.ts at 50%, lifting
  with new spec covering the authenticated path."
- "Skipping changes to `.lighthouse/jenkins-x/`: not relevant to
  `test_unit_spec_count_changed_when_app_changed`."

This makes the audit trail searchable, makes the agent's reasoning legible to future
reviewers, and surfaces criteria-gap signals — if you can't name the criterion
driving a change, the change probably shouldn't be made unless explicitly requested.

Same principle in commit messages: include the failing criterion name in the body.
Future-you searching git log for a regression will thank present-you.

### When end2end-* fails on one cluster but passes on the other, run kubectl on the failing cluster BEFORE iterating or retriggering

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

### Prefer blocking `gh pr checks --watch` over MCP polling loops when waiting on terminal events

When waiting for Tekton checks to reach a terminal state, **always use a single
blocking Bash call**:

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

**Do NOT loop-poll** `mcp__leartech-pipeline__list_pr_checks` (or any other MCP
read tool) waiting for state to change. Each MCP call burns:

- 1 agent turn
- ~$0.02–0.03 in tokens
- ~5–15 seconds of wall-clock per call

A polling loop costs $0.20–0.50 per minute of waiting. The blocking `--watch` call
costs **zero** — `gh` sleeps inside a subprocess that the agent isn't billing for.

## How this surfaced

Observed live during PR #40's close-out demo: the agent called
`mcp__leartech-pipeline__list_pr_checks` 7+ times in a row across ~7 turns ($0.18)
while the actual checks had already reached terminal state. The agent was
"watching in the background" but its mental model was MCP-poll, not Bash-block.

## Procedure

**Best (preferred): use the `wait_for_terminal` MCP tool**

    mcp__leartech-pipeline__wait_for_terminal(repo, pr_number, timeout_seconds=900)

This wraps `gh pr checks --watch` inside the MCP server's subprocess — zero
agent-turn cost during the wait. Returns a structured result with
`status: "all_passed" | "some_failed" | "timeout"` plus the final checks state.
This is the v1 stop-gap until pub/sub (v2.0) replaces it entirely.

**Fallback (when running outside the agent loop): blocking Bash**

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

Same primitive, exposed directly. Use when you're at a shell, not driving
through the MCP layer.

**On timeout** (`status: "timeout"` from MCP, exit 124 from Bash) — the pipeline
is wedged. Apply chatops recovery (`chatops-recovery-on-stalled-tekton-checks`
lesson): confirm via one `list_pr_checks` call, post `/test <check>`, then
`wait_for_terminal` again.

**Only after the wait returns terminal** should the agent run
`mcp__leartech-criteria__run_criteria_set` for the gate verdict. The MCP tools
are for *acting on terminal state*, not polling toward it.

## Why this lesson is a stop-gap, not a permanent rule

The whole class of "wait for an external event" problems is the wrong shape for
agents. The right architecture is **pub/sub** — the agent subscribes to
`pr.checks.terminal{repo, pr}` on a message bus and is woken by a notification
when the state actually changes. No polling, no timeouts, zero waiting cost.

See `project_v2_pubsub_direction.md` for the design memo. When v2.0 ships
(NATS / Redis pubsub on cluster, single topic for `pr.checks.terminal`), the
`mcp__leartech-bus__wait_for_pipeline_terminal` MCP tool replaces both this
polling pattern and the `gh pr checks --watch` workaround.

**This calibration lesson becomes obsolete when v2.0 lands.** Until then,
`--watch` is the portable correct answer.

### Always read failure detail before proposing a fix

When the gate fails, **fetch the actual failure detail** before guessing what's wrong.
Concretely:

- For `test_coverage_meets_threshold` failures: read the per-file LCOV breakdown to
  identify *which* uncovered lines are pulling the average down. Don't blindly add
  more tests — add tests that target the uncovered lines.
- For `test_specs_pass` (Playwright) failures: pull the trace.zip / video for the
  failing spec and read the assertion error before editing.
- For `test_unit_tests_pass` failures: read the Karma / pytest stderr — the failure
  message identifies the test + assertion. Compile errors and runtime errors require
  different fixes.
- For pipeline failures: use `~/leartech/Hub/scripts/pr-pipelines.sh <repo> <pr> --failed-only --logs`
  to dump the failing step's stderr to `./pr-logs/<pr>/`.

A guessed fix that doesn't address the root cause wastes a full pipeline cycle
(~10-30 min) and erodes trust. One careful read beats three speculative iterations.
