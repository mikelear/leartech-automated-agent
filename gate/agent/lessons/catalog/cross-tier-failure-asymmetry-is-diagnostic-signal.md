---
id: cross-tier-failure-asymmetry-is-diagnostic-signal
title: end2end-ui failing while end2end passes narrows the diagnostic surface — agent today doesn't use this signal
captured_at: 2026-05-05T17:30:00Z
source:
  type: agent_run
  reference: pr_8_webcoder_ui_initiatives_tab
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - initiative_agent
status: open
slipped_past_criteria: []
proposed_criterion: |
  test_cross_tier_diagnosis_acknowledged — when end2end-ui fails AND end2end
  (service) passes (or vice versa), the agent's investigation message must
  acknowledge the asymmetry's narrowing implication ("failure scope is
  UI-only" / "failure scope is service-only"). Approximated by: a sticky
  comment or commit message reference to the cross-tier signal. More
  rigorous: a structured field in the agent's verdict output that names the
  asymmetry pattern and its narrowed search space.
---

## The signal

Catalog runs end2end pipelines on both the service tier (Go API) and the
UI tier (Playwright against the rendered SPA). When they disagree, the
disagreement itself carries diagnostic information:

| `end2end-ui` | `end2end` (service) | What this tells us |
|---|---|---|
| ❌ | ✓ | Failure scope is **UI-only**: rendering, change detection, OnPush, UI-side contract drift, auth-session UX, Hydra cookie state |
| ✓ | ❌ | Failure scope is **service-only**: API contract, persistence, RBAC, downstream call shape — UI's tests don't reach this layer |
| ❌ | ❌ | Failure scope is **shared**: env-level (cluster, Hydra config), shared library, or contract drift visible from both sides |
| ✓ | ✓ | (the only "all clear" — but see `passing-tests-need-coverage-audit-too`) |

The diagonal mismatches are the most diagnostically useful — they cut the
search space roughly in half before any logs are read.

## How this surfaced — PR #8 worked example

PR #8 (`mikelear/webcoder-ui#8`, `agent/add-initiatives-tab`) had:

- `gcp/end2end` — green ✓
- `gcp/end2end-ui` — failed on test 18 ("each row shows iteration counter and triggered-by"), 17/18 passing

The agent's diagnosis was correct — it pinned the failure to a transient
GCP Hydra memory-DSN session race and retriggered. **But it reached that
conclusion via a slower path**: tests #1 and #2 use identical navigation
and pass + reading the playwright.config.ts comment about Hydra. About 5
turns of investigation to reach a 1-step conclusion if the cross-tier
signal had been the diagnostic frame.

If the agent had been calibrated on the asymmetry pattern, the first
question would have been:

> "end2end-ui failed but end2end (service) passed. The bug is by definition
> in the UI-only domain — rendering, change detection, auth-session, or
> UI-side contract drift. Which of those?"

That's a 1-step diagnosis instead of a 5-step deduction. Faster, more
explicit, and creates an artifact in the agent's output that explains
WHY the diagnosis is constrained.

## Why a *criteria_gap*, not a *calibration*

Initial instinct: "calibrate the agent to use cross-tier signals". But
that puts the burden on every agent inferring the pattern correctly each
time. Better: **the gate enforces that the agent's verdict acknowledges
the asymmetry when it holds.** Structural enforcement, not behavioural
hope.

The criterion would be: when the gate detects the asymmetry pattern, it
checks whether the agent's investigation output (sticky comment or commit
message) names the pattern explicitly. If not, the criterion fails — the
agent has to either (a) document the asymmetry, or (b) explain why it
wasn't relevant (e.g., the failure was in a tier where the asymmetry
didn't actually constrain the search).

## Pairs with the existing tier composition

The catalog already runs both tiers. The signal exists; it's just unused.
This criterion harvests information from data already being collected,
zero new infrastructure needed.

## Trigger to build

Earliest signal: a future PR where the agent burns 10+ turns chasing a
failure across the wrong tier (e.g., reading service logs to debug a
UI-only race). That waste is what the criterion would catch.

Until then, the cost of NOT having it is small (the agent sometimes takes
slow paths but reaches the right conclusion). It becomes load-bearing when
agent token cost matters more than agent careful-thinking — i.e., at
scale, post-Phase-2 cluster deploy.

## Connection to coverage philosophy

This lesson AND `passing-tests-need-coverage-audit-too` are both about the
same meta-principle: **the gate today treats independent criteria as
independent**. But criteria carry collective signal that's lost when they
fire independently. Both lessons propose extracting that collective signal
as a separate criterion class — "*relational* criteria" that fire on
patterns across multiple tier results, not on any one result alone.

If we ever ship a third relational lesson, this is the architectural
direction worth pinning.
