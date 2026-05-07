---
id: playwright-coverage-criterion-must-check-assertion-strength
title: test_ui_changes_have_playwright_coverage verifies surface reference but not assertion meaningfulness — vacuous specs would pass
captured_at: 2026-05-05T11:50:00Z
source:
  type: manual_review
  reference: pr_7_webcoder_ui_about_page_dogfood
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - test_ui_changes_have_playwright_coverage
  - spec_suggester
status: open
slipped_past_criteria:
  - test_ui_changes_have_playwright_coverage
  - test_specs_pass
proposed_criterion: |
  test_playwright_assertions_meaningful — for each new spec added by the PR,
  verify each test() block contains at least one substantive `expect()` call
  against an element from the newly-added UI surface (component selector,
  data-testid, or post-navigation page state). Reject tests whose only
  assertions are tautological (e.g. `expect(page.url()).toContain('/path')`
  after `page.goto('/path')`) or absent (just `goto()` + waitFor).
---

`test_ui_changes_have_playwright_coverage` (`gate/criteria/per_repo/auth_ui/test_playwright_coverage.py`)
checks that every newly-added UI surface element (component selector, route,
data-testid) is **referenced** by some spec in `end2end-ui/`. It does NOT check
that the spec actually **asserts something meaningful** about that element.

A spec like this would satisfy the criterion AND pass the catalog's `end2end-ui`
task while testing nothing:

```typescript
test('about page exists', async ({ page }) => {
  await page.goto('/about');
  expect(page.url()).toContain('/about');  // tautology — page.goto resolved
});
```

The criterion sees `getByTestId(...)` references / `app-about` selector mentions
/ `goto('/about')` calls — but the *meaningful test* of "did the about page
actually render" requires an assertion against the rendered DOM, not just the
URL.

## How this surfaced

PR #7 on `mikelear/webcoder-ui` (the auth-ui dogfood replica). After the agent
opened the PR with the component + route but BEFORE it had run our gate to
detect the coverage gap:

- Catalog's `gcp/end2end-ui` passed (ran the 5 existing webcoder-ui specs that
  don't reference `/about`)
- Mike observed: "unless playwright just worked without been changed which
  shows a weakness in the test"

Right call — `gcp/end2end-ui` green here was *vacuous*. The catalog test the
existing specs; nothing was actually validating the new feature. Our gate was
designed to catch this exact case via `test_ui_changes_have_playwright_coverage`,
but only on the *surface-reference* level. A motivated-but-lazy spec drafter
(human or AI) could satisfy the surface criterion with weak assertions and ship
a spec that tests nothing.

## What "meaningful assertion" should mean

Heuristics for the proposed criterion:

1. **At least one `expect(…)` call per `test()` block** that asserts on an
   element from the newly-added surface (data-testid, selector, or post-render
   state).
2. **No tautologies**: `expect(page.url()).toContain(/path/)` immediately after
   `page.goto('/path')` is a no-op assertion (page.goto resolves with the
   navigated URL by definition).
3. **Reject `.skip` and `.only`** in newly-added tests (already covered by
   `test_no_skipped_or_focused_tests`, but worth re-asserting in this layer).
4. **Soft-warn on `.toBeVisible()` without context** — bare visibility checks
   on a component element are weak; better to assert on text content, attribute
   values, or count of rendered children.

## Why this is a *criteria_gap*, not a *calibration*

This isn't an agent behaviour fix (the agent's drafting pattern, where it
mostly produces good assertions thanks to the spec_suggester's reference-spec
priming, is fine). It's a **gate gap**: even if the agent draft is perfect,
the gate framework today doesn't *enforce* meaningful assertions. A bad-faith
spec or a regression-via-edit would slip through. The fix is a new pytest
criterion that examines each new spec for assertion strength.

## Pairs with the existing spec_suggester calibration

`spec-suggester-must-only-use-api-from-reference-specs.md` calibrates the
*generation* side. This proposed criterion calibrates the *gate* side —
mutually-reinforcing. Even if the suggester is poorly calibrated one day, the
gate catches weak output.

## Trigger to build this

Earliest signal: when an agent (or human) commits a Playwright spec that
satisfies `test_ui_changes_have_playwright_coverage` but turns out to have
shipped a bug to staging — the spec was *referencing* the surface but not
*testing* it. Until then, the existing reference-coverage criterion is doing
80% of the job. The 20% gap (assertion strength) becomes load-bearing once
agent-drafted specs are landing autonomously at scale.
