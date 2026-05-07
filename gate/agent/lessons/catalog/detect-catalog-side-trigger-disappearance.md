---
id: detect-catalog-side-trigger-disappearance
title: A trigger configured but producing no LighthouseJob → catalog-side regression, treat as failure not skip
captured_at: 2026-05-04T23:15:00Z
source:
  type: ci_failure
  reference: leartech-pipeline-catalog#f4d9038
  observer: mike@leartech
  latency_to_capture: 3d
category: criteria_gap
applies_to:
  - initiative_agent
  - review_agent
status: encoded
encoded_in:
  - gate/tools/triggers.py
  - gate/criteria/shared/test_trigger_completeness.py
encoded_at: 2026-05-04T23:45:00Z
slipped_past_criteria:
  - test_ai_review_not_blocking
  - test_ai_review_passing_on_every_cluster
  - test_pr_checks_green
proposed_criterion: |
  test_no_silently_disabled_triggers — for each trigger declared in
  `.lighthouse/jenkins-x/triggers.yaml` of the consumer repo, assert that either
  (a) a corresponding GitHub status check appears in `statusCheckRollup`, OR
  (b) the trigger has `always_run: false` (opt-in only, e.g. `/ai-feedback`).
  Failure mode caught: catalog regression silently disables a trigger across
  every consumer; current criteria treat this as "skipped — nothing to assert"
  rather than the catalog-side failure it actually is.
---

The `ai-code-review` trigger is configured in every leartech consumer repo's
`triggers.yaml` with `always_run: true`. Since 2026-05-01 (commit `f4d9038` in
`mikelear/leartech-pipeline-catalog`, which added a classifier pre-check step), the
LighthouseJob is created but never materialises into a PipelineRun — the controller
reconciles in a loop for ~30 min and the job is garbage-collected. **Effect: AI
review has been silently disabled across every leartech repo for 3+ days.**

Our gate detected the symptom *but classified it wrong*:
- `test_ai_review_not_blocking` → SKIPPED ("No AI review comments posted yet")
- `test_ai_review_passing_on_every_cluster` → SKIPPED (same reason)
- `test_pr_checks_green` → didn't see ai-review at all (it's not in the rollup
  because no PipelineRun → no GitHub check → no rollup entry)

A skip looks like "this criterion is N/A right now". A skip is the **wrong verdict**
when the trigger is configured to always_run. The right verdict is **FAIL**: the
catalog-side machinery that should produce the artefact is broken.

**Recursive irony**: AI review is the system that should have flagged a broken AI
review pipeline. With it silently disabled, the regression was invisible to itself.

## Diagnostic procedure (when this lesson fires)

1. Read the consumer repo's `triggers.yaml`. List every `always_run: true` trigger.
2. Compare against the PR's `statusCheckRollup` (`gh pr view <pr> --json statusCheckRollup`).
3. For each declared `always_run: true` trigger MISSING from the rollup, check:

       kubectl get lighthousejob -l "lighthouse.jenkins-x.io/refs.repo=<repo>,lighthouse.jenkins-x.io/refs.pull=<pr>" \
         -o jsonpath='{range .items[?(@.spec.job=="<job>")]}{.metadata.name}{"\\t"}{.status.state}{"\\n"}{end}'

   If the LighthouseJob exists but has been reconciling without producing a
   PipelineRun for >5 min, the catalog source for that trigger has a problem.

## Permanent fix

Implement `test_no_silently_disabled_triggers` as a `shared/` criterion. Reads
the consumer's `triggers.yaml`, asserts the rollup includes every `always_run: true`
context. **Don't run any agent runs against this repo until this criterion exists.**
