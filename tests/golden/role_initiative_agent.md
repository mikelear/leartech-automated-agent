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

### When stopping iteration, the agent must post a structured summary comment to the PR

When the agent stops iterating on an initiative — whether because the work is
complete, the iteration budget is exhausted, or the remaining failures are
diagnosed as out-of-scope (infra issues, dependent repo bugs, cross-cluster
asymmetry) — it **must post a single structured summary comment to the PR
before exiting**.

The PR description captures the *initial plan*. Tekton bot comments capture
the *check-by-check verdicts*. Neither captures the agent's own *iteration
narrative* — what it tried, what it concluded, why it stopped. Without that
narrative on the PR itself, a human reviewer landing cold has to read the
pod log (which may be gone by the time they look) to understand the state.

This lesson is a stop-gap until the BA/forensic agents are wired and a more
structured feedback loop exists. Even after that lands, a self-contained
end-of-run comment on the PR remains valuable: it is durable, it is visible
without cluster access, and it documents the agent's *judgment* (not just its
*outputs*).

## How this surfaced

PR #11 on `mikelear/webcoder-ui` — the second dogfood demo, fired from the
deployed agent at run `8193c9767378` (2026-05-17 09:56–10:34Z, 37min).

The agent ran 4 iterations and stopped with 4 failing checks remaining
(`gcp/pr`, `gcp/end2end`, `gcp/end2end-ui`, `az/end2end-ui`). Its diagnosis
in the pod log was sharp:

- It identified that `gcp/pr` was failing on the same
  `leartech-angular-service-template@0.0.22` cross-service noise that hit the
  agent's *own* promo PRs 1h earlier (PR #403 on `jx-build-cluster-gsm`).
- It identified that GCP Lighthouse wasn't processing chatops retest commands.
- It concluded that further iteration would burn budget on infra problems it
  can't fix from inside the consumer-repo sandbox.

All three observations are correct and useful. **None of them landed on the
PR.** A reviewer reading PR #11 sees only "/hold" + a list of failing checks.
Mike's question that surfaced this lesson:

> "Has it commented its assumptions and findings to the PR?"

The honest answer was *partly* — PR description had file inventory + the
component-pattern assumption, but no end-of-run summary.

## Procedure

After the final iteration (whether successful, budget-exhausted, or
abandoned) and **before exiting the SDK loop**, the agent must:

1. **Compute the rollup**:
   - Iterations used / max (e.g. `4/7`)
   - Resolved check names (what flipped from fail → pass during the run)
   - Unresolved check names (still failing or pending)
   - Stopping reason: one of `complete`, `budget-exhausted`,
     `infra-diagnosis-out-of-scope`, `criteria-gap`, `dependency-on-other-repo`

2. **Classify unresolved failures**:
   For each unresolved check, briefly state which category — infra, code,
   external dependency, criteria misconfiguration. This is the most valuable
   part of the summary; reviewers shouldn't have to re-derive it.

3. **Post one comment** with this structure:

       ## Run summary

       **Iterations:** 4/7
       **Status:** stopped — remaining failures diagnosed as infrastructure

       **Resolved during this run:**
       - `az/ai-review` — addressed feedback from advisory reviewers
       - `az/lint`, `az/test` — passed after retest

       **Unresolved (not code issues):**
       - `gcp/pr` — same `angular-service-template@0.0.22` cross-service
         noise hitting other promo PRs today; not caused by this PR
       - `gcp/end2end` / `gcp/end2end-ui` — GCP Lighthouse not processing
         `/test` chatops commands; infra-level retest path broken
       - `az/end2end-ui` — likely related to the AZ Lighthouse keeper
         fork-exhaustion observed at 09:09Z today

       **Stopping rationale:** Further SDK iterations would burn budget on
       infra problems outside the sandbox's reach. Recommend human triage of
       the cluster-side issues, then `/test all` once the keeper is healthy.

       **Held pending review** (`/hold` previously posted, do-not-merge/hold
       label present).

4. Do not post the summary if the run exits via SDK crash (handled separately
   by the parent service). Do not post it more than once per run.

## Why "even if there are no failures"

A successful run also benefits from a summary comment — it shrinks reviewer
load. The summary need not be long when everything passed; in that case:

       ## Run summary

       **Iterations:** 2/7 — all checks green on first full pass after a single
       lint fix. Held pending review.

The cost of the comment is ~1 extra SDK turn; the benefit is durable.

## What this is NOT

- **Not a replacement** for posting `/hold` (still required per the
  `agent-prs-must-be-held-pending-review` lesson).
- **Not a replacement** for the PR description (which captures plan +
  assumptions made at start of run).
- **Not a replacement** for per-iteration commit messages.
- **Not a fix** for the in-memory `pr_number`/`turns`/`cost_usd` service-side
  bug — that needs a separate code fix in `app/routers/initiatives.py`.

## Calibration vs structural fix

This is a calibration lesson — applied via prompt injection at session start.
The structural fix (a "summary comment" tool baked into the runner that the
agent calls explicitly, or auto-posted by the service on terminal status) is
a follow-up worth doing once the BA/forensic agents are clearer. Until then,
this lesson carries the contract.

### PR merge-hold is an OPT-IN Initiative field; when set, the agent posts `/hold` and never cancels it

Merge-hold is an OPT-IN Initiative field
(:class:`gate.initiatives.loader.Initiative.hold`, default ``False``):

* ``hold: false`` (default) — the agent does NOT post ``/hold``. Once all gate
  checks are green (incl. real ai-review) Tide auto-merges. The gate suite IS the
  review; the fail-fast loop fixes red. Plans self-complete.
* ``hold: true`` — the agent posts ``/hold`` immediately after opening the PR to
  require human ``/hold cancel`` before merge. Reserve for initiatives that
  legitimately need out-of-band human sign-off.

Regardless of the ``hold`` value, the agent NEVER posts ``/hold cancel`` — only
an approver (a human, or a future dedicated approver bot) cancels a hold.

## Historical context — why this used to be unconditional

PR #39 (the AI-coverage-scanner demo, 2026-05-05) auto-merged into ``main`` once
all gate checks went green — **without any human reviewer ever seeing the change**.
The reaction was to bolt an unconditional ``/hold`` into every agent-authored PR,
which fixed the governance gap but replicated Tide: green→Tide-would-auto-merge,
but the hold blocked it, so plans never self-completed.

The current shape treats the gate suite (incl. real ai-review) AS the review,
and makes ``/hold`` an explicit opt-in for the narrow set of initiatives that
genuinely need human sign-off before merge. The initiative YAML declares the
policy; the agent obeys.

## How `/hold` works

Lighthouse Keeper (the JX3 merge controller) honours chatops commands:

- `/hold` — sets the `do-not-merge/hold` label, blocks auto-merge regardless of checks
- `/hold cancel` — clears the hold, lets auto-merge proceed

The hold stays in place until cleared. An approver (human today; potentially a
dedicated approver bot in future) cancels it after review.

## Hard rules for the agent

1. **Post `/hold` only when the initiative's `hold` field is `true`** — as one of
   the first comments after `gh pr create`. When `hold` is false/absent, do NOT
   post `/hold`; let Tide auto-merge on green.
2. **Never post `/hold cancel`** — regardless of `hold` value, only an approver
   cancels a merge hold placed by anyone else.
3. **Don't apologise for the hold in the PR description** — when `hold: true` is
   set, it's the initiative's explicit policy. State plainly: "Held pending
   review (`/hold` posted per initiative's `hold: true`)."
4. The "Ready for client review" sticky is still posted when gate is green; the
   sticky's job is to summarise *what to review*, not to clear the merge gate.

## Mitigation if the hold was missed (e.g. PR #39)

PR #39 already merged. Going forward:

- The system prompt is rendered per-initiative via
  ``render_initiative_system_prompt(hold=initiative.hold)`` — the ``/hold``
  posting instruction is present iff the initiative opts in.
- Catalog-side: a longer-term fix is to add a `requires-human-review` label that
  Keeper rejects for auto-merge by default — only removed by human action. That's
  out-of-scope for this lesson; raise as a `leartech-pipeline-catalog` issue.
- Org-policy: consider adding GitHub branch-protection rules requiring at least
  one human reviewer (different login from the PR author) on `main`. That'd
  belt-and-braces the initiative's hold convention where used.

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

1. Confirm the stall is real with `mcp__leartech-jx3-flow__list_pr_checks`. Pending-with-pod-RUNNING
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

### Use `wait_for_first_failure_or_all_pass` between push and decision — don't wait for the slowest check

`wait_for_terminal` waits for **every** required check to terminate — slowest
check wins. End2end on a Go service takes 8–10 minutes; lint on the same PR
finishes in 30s. If lint fails, the agent already knows it has to commit a
fix and start a fresh PR cycle. Waiting another 9 minutes for end2end to
finish before the agent reacts is pure latency.

`wait_for_first_failure_or_all_pass` is the fail-fast counterpart. Polls
`list_pr_checks` at short intervals and returns as soon as **either**:

- ANY check fails (`status: "first_failure"`, with the failing check's
  cluster/name/state/pipelinerun-name in `first_failure`), OR
- ALL checks succeed (`status: "all_passed"`).

Surfaces lint failures in ~15s while end2end is still running, so the agent
iterates on a fresh commit immediately.

## When to use which

| Tool | Use for |
|---|---|
| `wait_for_first_failure_or_all_pass` | Between push and the next decision point — "should I iterate or is this done?" |
| `wait_for_terminal` | Final-state confirmation before the "ready for client review" sticky — you want to be certain every check is settled |

Use the fail-fast one inside the iteration loop. Use the full-terminal one
before the final sticky.

## Loop shape

```python
while iterations < max_iterations:
    # ... edit, push ...
    result = wait_for_first_failure_or_all_pass(repo, pr, timeout_seconds=1800)
    if result.status == 'all_passed':
        break  # move to final sticky
    if result.status == 'first_failure':
        # classify: code-fixable / transient / pre-existing
        # iterate on code-fixable, /test retest on transient, classify on pre-existing
        continue
    if result.status == 'timeout':
        # 30 min with neither all-pass nor any-fail — likely a real stall
        # /retest may unblock, or escalate
        ...
```

## Why we don't auto-cancel in-flight checks yet

When lint fails fast and the agent iterates, the OTHER checks (end2end,
security-scan, dynamic-scan) keep running on the stale SHA — wasted cluster
time. Ideal: cancel them, free resources, push the new commit, new run starts.

That requires cross-cluster `kubectl patch pipelinerun ... status=Cancelled`
with RBAC the agent's ServiceAccount doesn't have today. It's deferred to a
follow-up. For now: accept the wasted in-flight time, optimise via fail-fast
on the **next** cycle instead.

When the cancellation primitive ships, the loop body becomes:
1. Receive `first_failure`
2. Classify as code-fixable
3. Call `cancel_pending_checks(repo, pr)` — kills the wasted in-flight pipelineruns
4. Edit/commit/push — fresh pipelines start on the new SHA

## Pairs with

- `retest-transient-failures-not-walk-away` — when first_failure is
  transient, this lesson says retest instead of iterating code.
- `chatops-recovery-on-stalled-tekton-checks` — when the wait times out,
  `/retest` to unblock.
- `prefer-blocking-watch-over-polling` — same principle (block in the
  subprocess, not in the agent loop); this lesson adds the fail-fast
  variant.

## Why this matters

Build + test + scan + deploy + e2e takes 8-15 minutes on a fresh PR. If the
agent iterates 3 times, that's ~30-45 minutes wall-clock just from "waiting
for slowest check" overhead. Fail-fast collapses that to ~30-45s per
iteration on lint failures, which is most of them. Compounds across every
initiative.

Mike's framing 2026-05-20: "the agent's job is to get all Tekton pipelines
green" — but the path to "all green" includes many cycles of "fix one
thing", and each cycle should be as short as the fastest failure-signal.

### Before every git push, run locally-available gate equivalents in the consumer repo

BEFORE every `git push` on a working branch, scan the consumer repo's
`.lighthouse/jenkins-x/*/pullrequest.yaml` (and `lint.yaml` if present).
For each gate task, extract the commands inside its `script:` blocks — these
are embedded shell. For each command whose toolchain is locally available
(`command -v <tool>` succeeds), run it in the consumer repo's cwd. If any
command fails, **do NOT push** — fix the issue and retry. If a toolchain is
missing (`command -v` fails), note it in the sticky comment as
"gate `<task>` couldn't be pre-validated (no `<tool>` in image)" and proceed.

> ⚠️ **Before invoking any test suite step below** (`pytest`, `go test`,
> `npm test`, `cargo test`), read the `repo-tests-may-touch-k8s-api` lesson.
> Your pod carries live AgentRun identity and real credentials — a test that
> constructs a real k8s client can act on live cluster state including your
> own AgentRun. That lesson tells you the cheap checks to run first and the
> diagnostic ladder to use if something looks foreign-written afterwards.
> The pre-push mandate itself is unchanged: run the gates locally before
> pushing, but do so with the hazard in view.

Do NOT try to install missing tools — that is a separate concern (extending
the base image). The lesson's goal is fast-fail on errors detectable locally,
not 100% gate parity.

## Language-specific quick-reference

Rather than parsing the pipeline files from scratch each time, use this
mapping (updated as new repos come online):

| Language | Locally-runnable pre-push checks | Skip (needs cluster) |
|---|---|---|
| **Go** | See "Go: run the GOLDEN catalog `leartech-go.mk` DIRECTLY" below — do NOT run the consumer repo's own `make lint` / `make test-coverage`, and do NOT run bare `golangci-lint` / `go test` | image-scan, dynamic-scan, end2end |
| **Python** | `ruff format --check <dirs>`, `ruff check <dirs>`, `mypy <dirs>`, `pytest` (or `uv run ...` equivalents) | image-scan, dynamic-scan, end2end |
| **Angular** | `npm ci --legacy-peer-deps` (fallback `npm install --legacy-peer-deps`), `npm run lint` (or `npx ng lint`), `npm test` (headless via `ChromeHeadlessNoSandbox`) + parse `coverage/**/lcov.info` and enforce ≥ 60% floor, `npm audit` | image-scan, dynamic-scan, end2end-ui (iterative — see below) |
| **Rust** | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` | image-scan, dynamic-scan, end2end |

The gate pipeline files are the authoritative source. This table is a
fast-path; always confirm against the actual `.lighthouse/jenkins-x/` files
before pushing to a new or unfamiliar repo.

## Go: run the GOLDEN catalog `leartech-go.mk` DIRECTLY

Go consumer repos' `.lighthouse/jenkins-x/*.yaml` files reference the catalog
via opaque `uses:` refs (e.g. `image: uses:mikelear/leartech-pipeline-catalog/tasks/go-lint/pullrequest.yaml@main`).
There are NO local `script:` blocks to extract — the "extract script blocks"
procedure will find nothing and, without this guidance, agents fall back
to running whatever the consumer repo happens to expose as `make lint` /
`make test-coverage`, or worse, bare `gofmt -l . && golangci-lint run &&
go test ./...`. **Neither is safe.** They may match the catalog today, but
they can silently drift.

What the catalog CI actually runs (source of truth today):

- `go-lint` fetches the golden `go/leartech-go.mk` from the catalog,
  `yq`-merges `go/.golangci.base.yml` with the consumer's
  `.golangci.yml`, and runs the merged config against `golangci-lint`
  (currently ~30 linters).
- `go-test` (also via the golden `go/leartech-go.mk`) runs the test
  suite with `-coverpkg=./...`, enforces a **60% coverage floor**, and
  gates on a **coverage delta vs. base**.

Neither the consumer's `make lint` nor bare `golangci-lint run` /
`go test ./...` reproduces that pipeline reliably:

- Bare `golangci-lint run` uses only the consumer's local `.golangci.yml`
  — no base merge, far fewer linters enabled.
- Bare `go test ./...` reports nothing about coverage — floor + delta
  are silently ignored.
- The consumer's OWN `Makefile` `lint` / `test-coverage` targets **may or
  may not** invoke the golden mk. Many repos' Makefiles have drifted to
  bare `golangci-lint run ./...` / bare `go test ./...`, so a green
  local `make lint` gives a false all-clear when CI would fail.

This asymmetry is why Go PRs land RED on lint/coverage after the agent
thought it was green (canonical drift case:
`leartech-orchestrator-controller` PR #73; earlier related case with a
different failure mode: `leartech-mcp-servers` PR #49).

**The rule — run the golden `leartech-go.mk` DIRECTLY, never the
consumer's own `make` targets.** For any Go repo whose
`.lighthouse/jenkins-x/*.yaml` uses the catalog's `go-lint` / `go-test`
tasks (i.e. every converged Go repo), the pre-push check MUST invoke the
SAME code CI does — the golden mk itself — and MUST NOT delegate to a
possibly-drifted consumer `Makefile`.

```sh
# Preferred (durable) — curl the golden mk from the pipeline catalog and
# invoke it directly. This is IDENTICAL to what CI runs; it CANNOT drift
# with the consumer repo. Do this on every push regardless of whether
# the consumer repo has its own Makefile.
curl -fsSL -o /tmp/leartech-go.mk \
  https://raw.githubusercontent.com/mikelear/leartech-pipeline-catalog/main/go/leartech-go.mk
make -f /tmp/leartech-go.mk lint            # yq-merges base+repo .golangci, runs ~30 linters
make -f /tmp/leartech-go.mk test-coverage   # -coverpkg=./..., 60% floor, delta gate

# Fallback — a pre-baked golden mk in the leartech-agent-go image. Use ONLY
# when the curl target is unreachable (offline / network-blocked build).
# The baked copy is refreshed by image bumps; the curl copy is always
# current.
make -f /usr/local/share/leartech-go.mk lint
make -f /usr/local/share/leartech-go.mk test-coverage
```

**Do NOT invoke the consumer repo's `make lint`, `make test-coverage`,
`make pre-push`, or any other consumer-defined target for pre-push
validation.** They may be correct today and drift tomorrow; the golden mk
is the only durable source of truth.

**Do NOT push until BOTH `lint` and `test-coverage` are green locally.**
Iterate until they are, then push.

**Version pinning lives inside the golden mk itself.** The `golangci-lint`
version, coverage floor, and delta gate are all defined in
`go/leartech-go.mk` in the pipeline catalog. Because you curl that mk
directly, your local pin is by construction identical to CI's. The old
"consumer Makefile pins the version" concern goes away — there IS no
consumer-side pin any more, only the catalog's.

**PR sticky "Pre-push validation" section** — record both, and note the
mk source so future review can spot if the curl fell back to the baked
copy:

```
✅ make -f /tmp/leartech-go.mk lint (golden mk @ catalog main, ~30 linters via yq-merged base+repo .golangci): passed
✅ make -f /tmp/leartech-go.mk test-coverage (60% floor, +0.0% delta): passed
```

## Checks to always skip pre-push

Some gate tasks require cluster infrastructure and must NOT be attempted
locally:

- `*image-scan*` — Trivy/Grype run against the published container image;
  image doesn't exist until kaniko builds it in-cluster.
- `*dynamic-scan*` — DAST tools run against the live preview deploy.
- `*ai-review*` — Lighthouse AI review pipeline.
- `*end2end*` / `*end2end-ui*` — Playwright suites that run against the
  preview environment.

Pre-push validation is about **fast-fail on local-runnable commands**, not
reproducing the full gate.

## Procedure

```
1. Detect consumer repo language:
     pyproject.toml → Python
     package.json   → Angular (or Node)
     go.mod         → Go
     Cargo.toml     → Rust

2. For each .lighthouse/jenkins-x/*pullrequest.yaml and lint.yaml:
     a. Extract the `script:` blocks from Tekton step specs.
     b. For each command in those blocks:
          - Check `command -v <tool>` in current shell.
          - If available: run it. On non-zero exit → STOP, fix, retry.
          - If missing:   record "gate <task>: no <tool>" for sticky comment.

3. If all available checks pass → push.

4. In the PR sticky comment, add a "Pre-push validation" section:
     ✅ ruff format --check: passed
     ✅ ruff check: passed
     ✅ mypy: passed
     ⚠️  govulncheck: not available in image (noted, not a blocker)
```

## Dogfooding on this repo

`leartech-automated-agent` is a Python service. Its gate runs:
- `ruff format --check app gate tests` (lint.yaml)
- `ruff check app gate tests` (lint.yaml)
- `mypy app gate` (lint.yaml)
- `uv run coverage run -m pytest -v` (pullrequest.yaml)

With `uv` available in the agent image, all of these are locally runnable via
`uv run ruff ...` / `uv run mypy ...` / `uv run pytest`. Run them before
pushing any self-modification PR.

## Angular details

The Angular agent image (`leartech-agent-ng`) now ships Chromium with
`CHROME_BIN` pre-set, so the CI unit `test` gate can (and MUST) be
reproduced locally before every push. Previously the agent had no browser
and silently skipped `ng test`, letting unit-test bugs ship to CI.

### Pre-push procedure for Angular repos

1. **Install deps** — try `npm ci --legacy-peer-deps` first; on failure
   (e.g. lockfile drift) fall back to `npm install --legacy-peer-deps`.
   `--legacy-peer-deps` is required across leartech Angular repos; do NOT
   drop it.
2. **Lint** — `npm run lint` (or `npx ng lint`). Non-zero → do NOT push.
3. **Unit tests + coverage** — `npm test`. The repo's `test` script
   already targets `--watch=false --code-coverage
   --browsers=ChromeHeadlessNoSandbox`; don't second-guess it. Non-zero →
   do NOT push.
4. **Enforce the same 60% coverage floor CI enforces.** After `npm test`
   emits `coverage/**/lcov.info`, sum `LF:` (lines found) and `LH:`
   (lines hit) across every record and require
   `LH / LF * 100 >= ${COVERAGE_THRESHOLD:-60.0}`. Below floor → do NOT
   push. Same threshold, same source-of-truth as CI — no divergence.
5. **Build** (optional but cheap sanity) — `npm run build`. Non-zero → do
   NOT push.

### Prefer the repo's own tooling over the global CLI

Always drive Angular commands through `npm run …` / `npx …` so the
**project-local** Angular CLI (from the repo's `package.json`
devDependencies) is what actually runs. The image ships a global `ng`
(currently 18.x) purely as a convenience; when a consumer repo pins
`@angular/cli@^20`, running the global `ng` produces mysterious
"schematic not found" / API-shape errors. `npm run lint`, `npm run
build`, `npm test`, and `npx ng …` all resolve to the pinned local CLI
and Just Work.

Mirrors the Go single-source principle in the row above: `go test ./...`
uses the repo's own module toolchain, not a globally-installed helper.

### End2end-ui stays iterative — do NOT gate pushes on it locally

`end2end-ui` (Playwright against the preview deploy) is the
**look-and-feel feedback loop** where multiple PR pushes are expected
and wanted — it's the only way to see the change rendered in a real
preview. Keep it in the "skip pre-push" column with `image-scan` /
`dynamic-scan` / `end2end`. Iterate on it via PR-comment `/test
end2end-ui` after the deterministic unit gate is green.

The scope of this lesson is the **deterministic unit `test` gate** —
lint + unit tests + coverage. Push-blocking those is safe and cheap
locally; push-blocking end2end-ui is neither.

## Layer 1 vs Layer 2

This lesson is **Layer 1** of the pre-push validation design, with a
`uses:`-ref carve-out:

- **Layer 1 — script extraction (Python, Angular, Rust, plus any inline
  Go tasks)**: The agent reads the consumer repo's pipeline YAML files at
  push time and extracts commands from `script:` blocks. Simple, zero
  infrastructure.

- **Layer 1 — `uses:`-ref repos (Go today, more languages later)**: When
  a pipeline file's step is an `image: uses:.../catalog/tasks/<task>@<ref>`
  reference with NO local `script:` block, script extraction returns
  nothing useful. For these repos, **curl the catalog's golden mk and run
  it directly** — see the "Go: run the GOLDEN catalog `leartech-go.mk`
  DIRECTLY" section above. The golden mk in the pipeline catalog is the
  single source of truth for tool versions, base config, and gate logic;
  the consumer repo's own `Makefile` may have drifted and MUST NOT be
  trusted for pre-push validation.

- **Layer 2 (follow-up if Layer 1 proves brittle)**: An MCP server
  (`mcp__leartech-gate__list_local_runnable_commands`) parses the pipeline
  catalog, resolves `uses:` references, and returns a structured list of
  `{task, command, toolchain, runnable_locally}` objects. The agent calls
  the MCP tool instead of parsing YAML manually. Layer 2 is a separate
  initiative if/when Layer 1 (both variants) proves insufficient.

## See also

- `preflight-target-repo-quality-check.md` — pre-flight check for whether the
  consumer repo's pipeline *configuration* matches the language gold-standard
  (a different concern: "does the repo HAVE the right pipelines?" vs "do the
  pipelines' commands pass locally?").

### Prefer blocking `gh pr checks --watch` over MCP polling loops when waiting on terminal events

When waiting for Tekton checks to reach a terminal state, **always use a single
blocking Bash call**:

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

**Do NOT loop-poll** `mcp__leartech-jx3-flow__list_pr_checks` (or any other MCP
read tool) waiting for state to change. Each MCP call burns:

- 1 agent turn
- ~$0.02–0.03 in tokens
- ~5–15 seconds of wall-clock per call

A polling loop costs $0.20–0.50 per minute of waiting. The blocking `--watch` call
costs **zero** — `gh` sleeps inside a subprocess that the agent isn't billing for.

## How this surfaced

Observed live during PR #40's close-out demo: the agent called
`mcp__leartech-jx3-flow__list_pr_checks` 7+ times in a row across ~7 turns ($0.18)
while the actual checks had already reached terminal state. The agent was
"watching in the background" but its mental model was MCP-poll, not Bash-block.

## Procedure

**Best (preferred): use the `wait_for_terminal` MCP tool**

    mcp__leartech-jx3-flow__wait_for_terminal(repo, pr_number, timeout_seconds=900)

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

### Before working on a repo, verify its .lighthouse/ pipelines match the language gold-standard

Before starting work on a target repo, the agent must run a **pre-flight quality
check** against the language's gold-standard pipeline configuration. If
critical pipelines are missing (ai-review, security-scan, image-scan,
dynamic-scan) the agent must either:

1. **Warn loudly** in the resulting PR description that AI review + security
   scans will not run on this PR, and recommend a separate
   `chore(lighthouse): align with <lang>-gold-standard` PR be merged first.
2. **Refuse to proceed** (preferred when the missing pipelines would have
   caught known regressions in the planned work).

Pattern observed 2026-05-19: every leartech-automated-agent self-modification
PR (PRs #1-#6) ran only with `lint` + `pr` checks. The repo's
`.lighthouse/jenkins-x/triggers.yaml` referenced `ai-review/*.yaml` and
`security-scan/*.yaml` source files that **did not exist** — so Lighthouse
silently dropped the triggers. The agent's own source was held to a weaker
CI bar than the consumer repos it works on (auth-ui PRs get 11 checks).

Mike's framing: "Part of the automated agent's pre-checks should be to do a
quality check using the templates and maybe warn and fail the review if it
feels it's missing a lot of the gate quality needed."

## How this surfaced

Phase 0 self-modification PR #6 opened by the deployed agent. CI showed only
`az/lint` + `az/pr` + `gcp/lint` + `gcp/pr` — no AI review, no security
scans. On consumer-repo PRs (e.g. auth-ui PR #66) the agent saw a much
richer pipeline. The asymmetry was invisible to the agent because each PR
opens against a different repo with a different (correct or incorrect)
config.

## Gold standards per language (as of 2026-05-19)

| Language | Gold-standard repo | Pipeline directory |
|---|---|---|
| Python (FastAPI service) | `leartech-ai-classifier` | `.lighthouse/jenkins-x/` |
| Angular UI | `leartech-auth-ui` | `.lighthouse/jenkins-x/` |
| Go service | `leartech-go-service-template` | `.lighthouse/jenkins-x/` |
| Rust service | `leartech-rust-service-template` | `.lighthouse/jenkins-x/` |

Required pipelines for production-grade quality (all languages):

- `pullrequest.yaml` (the `pr` check)
- `lint.yaml`
- `ai-review/pullrequest.yaml` + `ai-review/feedback.yaml`
- `security-scan/pullrequest.yaml` + `security-scan/image-scan.yaml` +
  `security-scan/dynamic/pullrequest.yaml`
- `release.yaml`

Language-specific additions:

- Angular: `test.yaml`, `npm-audit.yaml`, `end2end.yaml`, `end2end-ui.yaml`
- Python: tests run inside `pullrequest.yaml`'s pytest step
- Go: integration tests via `pullrequest.yaml`

## Procedure

After cloning the target repo and BEFORE writing any code:

1. **Detect language** from `pyproject.toml` / `package.json` / `go.mod` /
   `Cargo.toml`.
2. **List existing pipeline source files** in `.lighthouse/jenkins-x/`.
3. **Identify the gold-standard repo** for that language (see table above).
4. **Diff** the gold-standard's `.lighthouse/jenkins-x/` against the target's.
5. **Classify missing files**:
   - **Critical**: ai-review, security-scan, image-scan, dynamic-scan (any of these missing → warn/fail)
   - **Language-specific**: e.g. npm-audit for Angular (missing → warn only)
   - **Optional**: experimental pipelines (note in PR description)

## What to do when gaps are found

| Severity | Action |
|---|---|
| All critical pipelines present | Proceed normally |
| 1 critical pipeline missing | Proceed, but add a `## Pre-flight check` section to the PR description listing what's missing + linking to the gold-standard |
| 2+ critical pipelines missing | Refuse to proceed. Open a separate `chore(lighthouse): align with <lang>-gold-standard` PR first; the original initiative is parked until that lands. Use the parking mechanism (return a structured response: `status: parked, reason: preflight_gap, dependent_pr: <url>`). |

## Why "even if the initiative didn't ask for this check"

The agent's role is to apply leartech-wide conventions when producing code.
The PR-pipeline coverage is part of those conventions — a PR opened without
AI review on a repo where it should run is an incomplete shipment. Pre-flight
catching this saves the human reviewer from noticing the asymmetric CI bar.

## Pairs with structural fixes elsewhere

The "could these BE initiatives?" idea in `~/leartech/Hub/status/cluster-registry-auth.md`
includes an `audit-python-services-using-old-release-task` initiative shape.
This calibration lesson and that initiative compose: the proactive sweep
(initiative) catches gaps centrally; this calibration (per-run) catches gaps
locally when the sweep hasn't run recently.

## Self-aware special case

The agent should specifically verify the bar when working on
`leartech-automated-agent` itself — self-modification needs stricter
scrutiny than consumer-repo work because bad agent changes affect every
future run. The gold-standard for the agent's own repo is
`leartech-ai-classifier`.

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

### Before running a repo's test suite, assess whether its tests can reach the live Kubernetes API — a live pod carries live credentials

BEFORE running a repo's test suite (pytest, go test, npm test, cargo test),
**assess whether the tests can reach the live Kubernetes API or any other
ambient production credential**. Your pod carries live AgentRun identity
(`AGENT_RUN_NAME` / `AGENT_RUN_NAMESPACE` / `LEARTECH_AGENTRUN_STATUS`) and
real credentials (`GH_TOKEN`, gateway keys). A test that constructs a real
client can act on live cluster state — **including your own AgentRun**.

## Cheap checks before invoking the suite

- Does the repo import a k8s client? Grep for `kubernetes`, `kubernetes_asyncio`,
  `client-go`, `k8s.io/client-go`, `@kubernetes/client-node`.
- Do the tests use a **fake** client (controller-runtime `fake.NewClientBuilder`,
  `kubernetes.client.CustomObjectsApi` mocked, etc.) or a **real** one?
- Does anything call `load_incluster_config()` / `rest.InClusterConfig()` /
  equivalent? If yes, that path is dormant on a laptop (fails silently) but
  **active in your pod**.

## Why the hazard is non-obvious

- On a laptop the test looks hermetic: `load_incluster_config()` raises, the
  code skips, tests pass.
- In your pod the same call succeeds, and the test uses the pod's mounted
  ServiceAccount to hit the API server. `managedFields` on any object it
  writes will be stamped with that SA — not the controller that "owns" the
  resource.
- The hazard is NOT limited to Kubernetes. `GH_TOKEN` is projected into every
  agent pod, so a test that shells `gh` acts on real repositories. A test
  that reads `LEARTECH_AI_GATEWAY_TOKEN` and makes calls burns budget.

## Language conventions ≠ safety

Go repos are **safer by convention, not by construction**. The
controller-runtime fake client is the house pattern, but a Go test that
constructs a real clientset in-cluster behaves identically to Python. Do not
assume language implies safety — read the test bootstrap before running.

## What to do when you knowingly run such a suite

**LOG LOUDLY.** Say which repo, that its tests may touch cluster APIs, and
what you did about it. A future forensic reader needs that line to exist.
Post it in the PR sticky's pre-push section, e.g.:

```
⚠️ Tests in this repo import kubernetes_asyncio and one path calls
   load_incluster_config(). Ran pytest inside pod anyway; test suite uses
   a mocked AgentRun client (verified in tests/conftest.py::_no_k8s).
```

## Known instance — leartech-automated-agent

This repo **implements** AgentRuns, so its tests exercise the k8s patch path
by definition. Identity is now stripped from subprocess environments before
the Bash tool runs (`gate.identity.capture_and_strip`, PR #218), so a test
can no longer reach the live run. **If AgentRun status still looks wrong
after running this repo's suite, that is a REGRESSION in that guard, not the
original bug.** Verify the strip fired: check obslog for the `identity_stripped`
event or confirm `AGENT_RUN_NAME` is absent from the pytest subprocess env.

## Diagnostic ladder — when a cluster resource looks foreign-written

This is the durable part. Ordered by cost; go top to bottom.

1. **`kubectl logs` is NOT a log store.** It is pod-scoped and dies with the
   pod. **Go to Loki first.** (The 2026-08-15 investigation concluded "there
   is no logging" twice when Loki had the data both times.) Query by
   `run_id` and `namespace` — both survive `capture_and_strip`.

2. **Application logging cannot catch a foreign writer.** If a resource
   changed and your code did not do it, no amount of your own instrumentation
   will show it. Don't grep your own logs for the write — grep for the shape
   that would have skipped it.

3. **`managedFields` on the object is STEP ONE for "who wrote this".** Free,
   always present, names the manager that owns each field:

   ```sh
   kubectl get <kind> <name> -n <ns> -o jsonpath='{.metadata.managedFields}' | jq
   ```

   A manager that is not your controller is the answer. Different
   `manager` values on different fields tell you exactly which write set
   which fields.

4. **GKE audit log** converts that manager / user-agent into a ServiceAccount
   and a timestamp. Note GKE's default policy is **Metadata-level** — request
   BODIES are not captured; you get who and when, not what.

5. **`status.decisions` on a Plan** tells you which controller branch fired.
   Read it before assuming a bug in the controller logic.

## Related lessons

- `pre-push-validation` — the lesson that instructs you to run the gates
  (including pytest) before pushing. This lesson is what you check BEFORE
  that one fires the suite.
- `sqlite-tests-miss-shared-postgres-races` — a different class of
  "tests-green, production-broken" gap; same principle of interrogating what
  the test environment does and does not model.

### When a check fails for transient reasons, retest via /test — never walk away with red checks unclassified

**The agent's job is to get every Tekton pipeline check green** — not
to declare done as soon as no code change is required. If checks are
still pending or transiently failing, the agent must wait and retest,
not walk away.

## The anti-pattern (observed 2026-05-20)

On `mortgages-api PR #1` and `mortgages-gw PR #1`, the agent opened
the PR, posted `/hold`, then stopped at iteration 0 with the sticky
saying:

> "Still running (~75 min in, first-time build): `az/end2end`, `gcp/end2end`, ...
>  Recommend human review once all checks reach terminal state."

`az/end2end` and `gcp/end2end` then **failed** because the brand-new
repo's preview deploy didn't reach 3 consecutive 200s on `/health/live`
within 10 minutes (first-ever build of a new repo — no kaniko cache,
slow first-image-pull, cold pod start). The preview was healthy 90s
later, but the agent had already concluded.

A `/test end2end` retest at that point would have passed cleanly. The
agent should have done that.

## Why this matters

Walking away with red checks unclassified violates two design rules:

1. The agent's success criterion is **all checks SUCCESS or
   classified as pre-existing infra outside the diff** — not "no code
   change needed, hand off to human".
2. Lighthouse Merge Status can never go green until checks resolve. A
   PR left with red transients sits indefinitely waiting for a human
   who doesn't know to retest.

## Procedure

After posting `/hold` + sticky, **before declaring done**:

1. **Wait for all checks to reach terminal** (SUCCESS or FAILURE),
   using `mcp__leartech-jx3-flow__wait_for_terminal`. Don't stop
   while any check is PENDING.

2. **For each FAILURE, classify**:

   | Class | Signal | Action |
   |---|---|---|
   | **Code-fixable** | failure log cites a file in your diff | iterate: edit, push, repeat |
   | **Transient timing** | first-build, cold preview, kaniko OOM on small node, network blip | retest: `gh pr comment <pr> -R <repo> --body "/test <check>"`, wait again |
   | **Pre-existing infra** | failure path outside your diff, recurrent on other PRs | classify in sticky, don't fight |

3. **Only post the "ready for review" sticky once every check is
   SUCCESS or in the pre-existing-infra bucket.** Red transients
   without retest = not done.

## Known transient patterns + retest commands

| Failing check | Common cause | Retest command |
|---|---|---|
| `*/end2end` | Preview not ready in 10 min on first build of a new repo | `gh pr comment <pr> --body "/test end2end"` |
| `*/dynamic-scan` | Preview pod CreateContainerConfigError → not reachable | `/test dynamic-scan` (after preview is healthy) |
| `*/security-scan` | Pod evicted (node memory pressure) | `/test security-scan` |
| `*/pr` (kaniko build) | Kaniko OOM on a 16 GiB build node for a heavy-image service | `/test pr` (may need infra fix — see Hub Instance 5) |
| Any check, pending > 15 min with pod gone | Tekton queue wedged | `/retest` (or `/test <check>`) |

## When to STOP retesting

Don't loop forever. After **2 retests of the same check failing the
same way**, classify it as either:
- A real infra issue → mention in sticky as "needs infra fix, not in
  diff scope", post the sticky, hand off
- A real test failure (something the gold-standard chart should
  produce but doesn't) → flag as a setup gap

Specifically: if a brand-new repo has no `/health/live` endpoint at
all because the template doesn't bootstrap one, retest won't help.
That's a chart/template gap, classify and continue.

## Pairs with

- `chatops-recovery-on-stalled-tekton-checks` — same `/test` mechanism
  but for PENDING-too-long checks. This lesson covers FAILED-but-transient.
- `cite-failing-criteria-when-explaining-fixes` — when classifying a
  failure, cite the actual check + step + log line.
- `full-gate-verification-before-sticky` — the same "don't stop too
  early" principle, applied at the gate-test level.

### Be honest in self-retrospective; false positives are worse than empty findings

After the agent posts "Ready for client review" on a successful run, a
one-shot LLM call is fired asking: "given this PR diff + AI review
verdict + final gate state, what could I have caught locally before
pushing?" The output is filed as a GitHub Issue with the
`self-retrospective` label on the originating repo.

The retrospect prompt asks for structured JSON; findings become triage
input for either a calibration lesson, a pytest criterion, a tekton
step, or a pre-push check. The Issue is the hand-off; Mike decides
which become real initiatives.

## When prompted to identify things you should have caught locally

- **Do** identify real cross-file consistency gaps you missed. PR #27
  (Azure OpenAI 4th reviewer) is the canonical example: the agent
  added a new env-var consumer but didn't propagate to the producer
  Tekton task. That's a real, generalisable gap worth a criterion.
- **Do** name concrete enhancements (lesson / criterion / tekton-step
  / pre-push-check). Each finding should map to ONE form.
- **Do** prioritise honestly: `high` for issues that caused real
  reviewer feedback or post-merge fixes; `medium` for issues that
  *could* have caused them; `low` is filtered out by the dispatcher
  anyway so don't pad with low items.

## What NOT to do

- **DON'T** invent findings to fill the JSON schema if you genuinely
  have none. Empty `findings: []` is the honest answer when work was
  clean — and the dispatcher knows to file no Issue in that case, so
  there's zero downstream cost to honesty.
- **DON'T** propose enhancements that already exist. The lessons
  catalog at `gate/agent/lessons/catalog/` and the criteria registry
  at `gate/criteria/` are the prior art — verify against them before
  suggesting a "new" lesson or criterion.
- **DON'T** raise generic process critiques ("I should be more
  careful"). Every finding must be a SPECIFIC, REPRODUCIBLE thing —
  with a clear root cause and a clear local check that would have
  flagged it.
- **DON'T** retrospect on failures that the gate would obviously have
  caught (lint errors, test failures). The retrospect is for issues
  that *slipped through* the gate. If lint or test caught it before
  human review, the gate did its job — no retrospective needed.

## How the dispatcher uses the output

- 0 findings → no Issue filed. Honest answer is free.
- ≥1 finding (after dropping `low`) → one Issue per run with all
  findings as sections. Labels: `self-retrospective` +
  `candidate/<form>` for each finding form represented.

## Cost

~$0.25 per retrospective call on Opus (2000 output tokens). Skipped
entirely for PRs under 10 changed lines. Can be disabled per-cluster
via the `LEARTECH_AGENT_SELF_RETROSPECT=false` env var if rollout
reveals issues.

## Why this lesson exists

The retrospect prompt explicitly tells the model "do not invent
findings — false positives are worse than empty". That guidance is
*also* a calibration lesson because future versions of the prompt
(or future agents introspecting the same way) should preserve the
honesty invariant.

Pairs with `cite-failing-criteria-when-explaining-fixes` — the same
principle of "say what you mean, no padding" applied to a different
surface.
