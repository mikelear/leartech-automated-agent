---
id: passing-tests-need-coverage-audit-too
title: gate audits failing tests but accepts passing tests at face value — coverage gaps hide behind green ticks
captured_at: 2026-05-05T17:30:00Z
source:
  type: manual_review
  reference: pr_8_webcoder_ui_initiatives_tab
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - test_ui_changes_have_playwright_coverage
  - test_specs_pass
  - initiative_agent
status: open
slipped_past_criteria: []
proposed_criterion: |
  test_passing_tests_exercise_diff — for every test that passes on a PR's
  diff, verify the test actually traverses code added or modified by that
  diff. Fails if NO passing test references any line in the diff (the
  passing tests are passing on unchanged code paths and tell us nothing about
  the change). Approximated by: greppable references to new symbols / files
  in passing test files; a more rigorous version would use coverage data per
  test.
---

## The asymmetry

Today the gate + agent treat passing and failing tests asymmetrically:

| State | Treatment |
|---|---|
| Test fails | Agent reads logs, debugs, fixes spec or code, iterates |
| Test passes | Agent moves on |

This is wrong. **A passing test only tells you "what was tested didn't fail"
— it doesn't tell you the change was tested.** A spec can pass without
exercising the new code path, and the agent will treat the green tick as
proof of safety.

## The gap

We audit:
- That new specs reference new surface (`test_ui_changes_have_playwright_coverage`)
- That assertions are meaningful (proposed in
  `playwright-coverage-criterion-must-check-assertion-strength`)
- That tests pass

We do NOT audit:
- Whether the *passing tests on this PR* actually exercise the diff. A PR
  could land code that is structurally untested by every passing test — the
  catalog tests would still go green because pre-existing tests don't touch
  the new code.

This is the inverse of the failure-investigation flow. The agent's failure
loop is rigorous (read logs, find root cause, fix). The pass loop is
trivial (assume green = safe). The asymmetry is a coverage hole.

## Why a *criteria_gap*, not a *calibration*

Calibration would say "tell the agent to audit passing tests too" — but
that's the wrong layer. The agent shouldn't have to inspect coverage
manually; the gate should refuse to accept PRs whose passing tests don't
actually exercise the diff. **It's a missing pytest criterion, not an agent
behaviour fix.**

## Pairs with `playwright-coverage-criterion-must-check-assertion-strength`

That lesson covers: each NEW spec must have meaningful assertions.
This lesson covers: at least ONE passing spec (new or pre-existing) must
exercise the diff.

Both are needed. A new spec with strong assertions could still miss the
diff (lesson 1 fails). And a diff with no spec coverage at all would be
caught by neither criterion individually but caught by both jointly.

## Trigger to build

Earliest signal: a PR lands where the agent's change introduces a bug that
slips through to staging — investigation shows all tests passed but none
of them exercised the changed lines. That's the load-bearing case.

Until then, the existing reference-coverage + (proposed) assertion-strength
criteria cover ~85% of the surface. The remaining 15% (passing-but-vacuous
coverage) becomes load-bearing once initiatives are running autonomously
at scale and a single bad PR has higher cost.

## Why this matters more for autonomous agents than humans

A human reviewer reading green ticks usually still spot-checks whether
"those tests actually exercise this change". An autonomous agent treats
green as terminal — it has no instinct to second-guess a passing tier. The
gate has to compensate for this with structural enforcement.
