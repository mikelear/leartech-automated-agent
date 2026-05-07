---
id: full-gate-verification-before-sticky
title: Always run full-gate (no mark filter) before posting "ready for client review"
captured_at: 2026-05-04T22:30:00Z
source:
  type: agent_run
  reference: pr_38_iteration_2
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/initiative_prompt.py
  - gate/agent/lessons/catalog/full-gate-verification-before-sticky.md
slipped_past_criteria:
  - test_pr_checks_green
proposed_criterion: |
  test_full_gate_clean_before_sticky — runs every shared/* criterion regardless
  of initiative gate_marks, asserts no PR-touching files appear in any failing
  check's logs.
---

When an initiative declares `gate_marks: [unit]` (or any subset), the criteria runner
filters to that tier. That's correct for *iteration speed* — you don't need the full
30-minute Tekton suite to confirm your change compiles.

**But before posting "ready for client review", you MUST run the gate one more time
with no mark filter.** Reason: lint, dynamic-scan, end2end checks live OUTSIDE the unit
tier and a failure there may still be caused by your change.

## How this slipped through on PR #38

The login-component-spec initiative had `gate_marks: [unit]`. The agent's gate run
honestly reported 6 passed / 0 failed / 1 skipped on unit criteria. **What it missed**:
`gcp/lint` and `az/lint` both failed because of an unused `fixture` variable in the
spec the agent itself wrote — `'fixture' is assigned a value but never used` at
`login.component.spec.ts:75:9`.

The agent's claim that the lint failures "pre-date this branch" was wrong. The 30
warnings did pre-date; the 1 error didn't. Reading file paths in the lint log would
have caught this in seconds.

## The procedure

Before declaring done:

1. Run full-gate: `mcp__leartech-criteria__run_criteria_set` **with no `mark` argument**.
2. For each failing catalog check, pull the logs:

       ~/leartech/Hub/scripts/pr-pipelines.sh <repo> <pr> --failed-only --logs

   Logs land at `./pr-logs/<pr>/<cluster>-<check>-<step>.log`.
3. **For each log, grep for any file path that appears in your PR's diff.** If found,
   the failure is yours — fix it. The 30-warning vs 1-error pattern is real: most
   pre-existing warnings don't matter, but any error-level finding pointing at a file
   you touched is your responsibility.
4. Only post the sticky if every catalog failure has been verified to reference files
   *outside* your PR's diff.

This is what `test_pr_checks_green` would have caught if the marker filter hadn't
hidden it. The proposed permanent fix is a `test_full_gate_clean_before_sticky`
criterion that always runs regardless of `gate_marks` — the system-prompt step is
the immediate guardrail until that lands.
