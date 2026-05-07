---
id: agent-applies-spec-convention-when-gate-silent
title: When adding UI surface, the agent must add a Playwright spec — convention, not gate-dependent
captured_at: 2026-05-05T12:05:00Z
source:
  type: agent_run
  reference: pr_7_webcoder_ui_about_page_dogfood
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/initiative_prompt.py
  - gate/agent/lessons/catalog/agent-applies-spec-convention-when-gate-silent.md
encoded_at: 2026-05-05T12:05:00Z
---

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
