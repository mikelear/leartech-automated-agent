---
id: ai-video-review-needs-saliency-calibration
title: AI video review fails on subtle UI changes — agent burns hours iterating on the criterion when the criterion is the bug
captured_at: 2026-05-06T12:30:00Z
source:
  type: agent_run
  reference: pr_9_webcoder_ui_new_dialog_video_spiral
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - test_video_visual_review
  - initiative_agent
status: open
slipped_past_criteria: []
proposed_criterion: |
  test_iteration_circuit_breaker — when the agent has iterated on a
  single criterion N times (N=3 default) without reaching SUCCESS, the
  gate should refuse to run that criterion again on the same PR until a
  human marks the criterion as either (a) calibration-needed or
  (b) genuinely-failing. Forces escalation instead of letting the agent
  spiral. Optional companion: per-criterion "iteration cost so far" in
  sticky comment so cost is visible to humans before approval.
---

## The spiral

PR #9 (webcoder-ui add-initiative-new-dialog) burned **6 commits and
~$5+** trying to satisfy `test_video_visual_review[gcp/08-...-the-button-opens-the-dialog]`:

| # | Fix attempt | Result |
|---|---|---|
| 1 | Move `toBeAttached` check after `toBeVisible` | failed |
| 2 | Add `fill('demo-initiative')` to create distinct visual state | failed |
| 3 | Close dialog at end of test for clear open→closed arc | failed |
| 4 | Replace `loginIfNeeded` with direct nav to reduce auth-flow video frames | failed (introduced regression) |
| 5 | Restore dashboard-page guard from regression in #4 | failed |
| 6 | Add `<dialog>::backdrop` CSS for visual contrast | partially worked, broke other test |
| 7 | **Switch to `dialog.show()` + custom CSS backdrop in normal layer** | genuinely insightful — Playwright `.webm` doesn't capture HTML5 top-layer |

Attempts 1-6 were the agent treating the criterion failure as a real
defect to fix. Attempt 7 was the moment the agent uncovered an actual
underlying interaction bug (Playwright's video recorder skips the
`<dialog>` top-layer that `showModal()` puts the modal in). That last
attempt is **genuinely valuable engineering** worth preserving regardless
of whether the rest of the spiral was wasted.

## Where the spiral comes from

The AI video reviewer is a vision-based criterion. It receives Playwright
`.webm` recordings and a frame-by-frame screenshot strip, then asks an
AI to verify the user-visible behaviour. It fails differently from text
criteria:

- Text criteria fail with **specific** messages — "expected 5, got 3" /
  "module not found" / "type 'X' is not assignable to 'Y'". The agent
  has a clear next move.
- Vision criteria fail with **fuzzy** messages — "no dialog open" /
  "screen frozen" / "page content never loaded". These can be true
  (the dialog actually didn't open) OR false (it did, but the visual
  signal was too subtle for the AI). The agent has no way to tell which
  from inside the loop.

When the criterion is the bug — i.e. the AI vision is over-strict or
unable to recognize a valid state — the agent's iteration hurts:

- Each attempt pushes a commit, fires another full pipeline (~10-15 min)
- Each attempt costs $0.50-1.00 in agent turns
- Each attempt grows the prompt context, accelerating byte-budget
  consumption (`org-rate-limit-byte-budget-rewards-many-short-sessions`)
- Six attempts = 60-90 minutes wall-clock + $5+ + non-trivial fraction
  of org/hour byte budget

## What good criteria-system design would do

Three layers of defence:

### 1. Circuit breaker

After N failed iterations on the same criterion (proposed N=3), the gate
escalates to human:

- Stop running that criterion on this PR
- Sticky comment: "Criterion `test_X` has failed 3 iterations. May indicate
  a calibration issue. Cost to date: $Y. Awaiting human review."
- Human resolves with either `/criterion-calibration-needed test_X` (mark
  as calibration issue, agent moves on, lesson auto-captured) or `/criterion-genuine
  test_X` (insist agent keeps trying)

### 2. Per-criterion iteration cost in sticky

Even before circuit-break, surface the cost transparently so humans can
see when a criterion is consuming disproportionate budget:

    Criteria iteration cost so far:
    - test_specs_pass[gcp]: 1 iteration / $0.18
    - test_video_visual_review[gcp/08-...]: 5 iterations / $4.20  ⚠
    - test_security_scan_clean: 0 iterations / $0.00 (out-of-diff)

This makes the spiral *visible* before it's catastrophic.

### 3. Distinguish vision-fuzzy from text-precise criteria

Vision-based criteria need:

- **Lower iteration limit** (1-2 attempts before escalation, not 3)
- **Different feedback shape** — instead of "no dialog open", the AI
  should return *what frames it sampled and what it saw in each*, so
  the agent can determine whether to (a) make the change more salient,
  (b) escalate as criterion bug, or (c) move on
- **Required human acknowledgement** before merge — humans should
  always look at the actual video, not trust the AI verdict, on
  vision-criterion-only failures

## What the agent should have done

Instead of 6 iterations, the calibration-correct flow:

1. Iteration 1: read failure detail, propose fix (the move-toBeAttached
   commit)
2. Iteration 2: read failure detail again. **Still vague — `dialog open`
   not visually confirmed**. *This is the moment to escalate*: post a
   sticky comment "test_video_visual_review may be miscalibrated; the
   spec is genuinely passing (test ✓), but the AI reviewer cannot
   confirm visually. Awaiting human review of the actual video."
3. Don't push more fix commits. Mark the iteration as escalated. Exit.

Total cost: ~$0.40, two iterations, maybe 20 minutes. Six fewer pipeline
runs. One sticky comment for human attention. Massive savings vs the
actual $5+ spent.

## Why this is `criteria_gap`, not `calibration`

The agent's behaviour was reasonable — it *did* read failure details, *did*
propose increasingly thoughtful fixes, *did* eventually find a real bug
(the top-layer/.webm interaction). The defect is structural: the gate
doesn't have a circuit breaker, and vision criteria don't return
debuggable feedback.

A purely calibration-side fix ("tell the agent to give up after 2
attempts") puts the burden on the agent to make a decision it has no
reliable signal for. The structural fix (gate-level circuit breaker +
visible per-criterion cost) takes the decision out of the agent's hands
and surfaces it for human judgement at the right moment.

## Pairs with other lessons

- **`passing-tests-need-coverage-audit-too`**: vision criteria failing
  is the inverse problem — passing tests we don't audit, and failing
  vision-criteria we audit too aggressively. Both stem from treating the
  criterion verdict as ground truth without confidence weighting.
- **`org-rate-limit-byte-budget-rewards-many-short-sessions`**: the
  spiral is what consumes the byte budget. Circuit-breaking the spiral
  also relieves byte pressure. They reinforce each other.

## Trigger to act

Already triggered by PR #9. Earliest concrete next step: when the
`test_iteration_circuit_breaker` criterion is built (probably alongside
the wait-phase factor-out and the dependency-graph primitive — they're
all the same shape: "watch a counter, act on a threshold"), it goes in
the same component.

Until then, **agent-side mitigation**: when the same criterion has been
iterated on 3 times without success, the agent should post a sticky
comment naming the criterion and exit. The system prompt could encode
this as a stop rule.
