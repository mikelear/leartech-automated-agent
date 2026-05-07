---
id: per-repo-criteria-must-be-shareable-across-template-consumers
title: per_repo/auth_ui/* criteria are silently auth-ui-only — block the AI coverage scanner from firing on any other consumer
captured_at: 2026-05-05T11:55:00Z
source:
  type: agent_run
  reference: pr_7_webcoder_ui_about_page_dogfood
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - test_ui_changes_have_playwright_coverage
  - test_video_visual_review
  - test_unit
  - test_coverage
  - test_playwright
status: encoded
encoded_in:
  - gate/criteria/per_repo/_angular_service_template/  # moved from per_repo/auth_ui/
  - gate/criteria/per_repo/_angular_service_template/conftest.py  # new template-membership autouse skip
  - gate/tools/triggers.py  # added angular_template_consumers() + go_service_template_consumers() helpers
encoded_at: 2026-05-05T12:30:00Z
slipped_past_criteria:
  - test_ui_changes_have_playwright_coverage
proposed_criterion: |
  Refactor `gate/criteria/per_repo/auth_ui/*` into a shared base that runs against
  any leartech-angular-service-template-derived repo. Either:
  (a) Move the coverage / playwright / unit criteria up to `gate/criteria/shared/`
      with a guard fixture that skips ONLY when the repo isn't a known
      angular-service-template consumer (read from a registry / repo-type.yaml).
  (b) Keep `per_repo/<repo>/` for genuinely repo-specific criteria, but extract
      a `per_repo/_template/angular_service_template/` directory that any
      angular-template repo's per_repo dir imports from.
---

## How this surfaced

PR #7 on `mikelear/webcoder-ui` (the dogfood demo). The agent committed the new
About component + route but no Playwright spec, deliberately to trigger the AI
coverage scanner. Catalog's `gcp/end2end-ui` passed vacuously (existed specs,
nothing testing /about). The agent then ran our gate to detect the coverage gap.

**Result: 4 passed / 2 failed / 20 SKIPPED.** The 20 skips include
`test_ui_changes_have_playwright_coverage`, blocked by the `_only_for_auth_ui`
autouse fixture in `gate/criteria/per_repo/auth_ui/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _only_for_auth_ui(pr_context):
    if not pr_context.repo.endswith('/leartech-auth-ui'):
        pytest.skip(...)
```

So when running against webcoder-ui, every per_repo/auth_ui criterion skipped,
including the AI coverage scanner. **The criterion that was supposed to detect
the missing About spec never even ran.**

## Why we wrote it this way

When we built `per_repo/auth_ui/*` during the v1 cycle, we hardcoded:
- Path conventions assuming `~/leartech/leartech-auth-ui/end2end-ui/`
- Reference-spec lookup paths
- Coverage-comment regex matching auth-ui-specific patterns

The autouse fixture was defensive: "skip these so they don't false-fail on a
different repo whose conventions differ". That defensiveness now blocks the
scenario we want — applying the same criteria to webcoder-ui (which uses the
*same* template, same conventions, same end2end-ui directory shape).

## What the refactor should produce

A model where criteria targeting "any angular-service-template-derived consumer"
fire automatically on every such consumer. webcoder-ui, leartech-auth-ui,
next-generation-lending-website, future-lending-ui — all derive from
`leartech-angular-service-template`. They share:

- `end2end-ui/*.spec.ts` directory convention
- `data-testid` selector convention
- `tasks/end2end-ui/pullrequest.yaml` catalog wiring
- `tasks/ng-test/pullrequest.yaml` LCOV sticky comment shape
- `app.routes.ts` (standalone) or `app-routing.module.ts` (NgModule) routing
  pattern

A repo-type registry (eventually `qa-architecture`'s `repo-type.yaml`) lets the
gate look up "what template is this repo derived from" and run the matching
criteria set.

For v1.5 (before qa-architecture lands), the simplest fix:

1. Move `per_repo/auth_ui/test_unit.py` → `per_repo/_angular_service_template/test_unit.py`
2. Same for `test_coverage.py`, `test_playwright.py`,
   `test_playwright_coverage.py`, `test_video_review.py`
3. The autouse fixture changes from "skip unless this is auth-ui" to "skip
   unless `pr_context.repo` is in `KNOWN_ANGULAR_TEMPLATE_REPOS`" — same hardcoded
   list pattern as `gate/tools/triggers.py::GOLDEN_TEMPLATE_FOR`
4. `per_repo/auth_ui/` keeps only auth-ui-genuinely-specific criteria (e.g.
   anything reading auth-ui-only sticky comment markers), if any

## Pairs with another lesson

This pairs with the just-captured
`playwright-coverage-criterion-must-check-assertion-strength` lesson — both
surfaced from the same PR #7 run. One is "criterion fires but verifies surface
not strength"; this one is "criterion doesn't fire at all on a second consumer".
**The dogfood demo earned its keep on its first run.**

## Trigger to build this fix

Immediate — without it, every webcoder-ui agent run will skip the AI coverage
scanner. The whole reason for picking webcoder-ui as the dogfood target was to
prove the scanner works across consumers. It doesn't yet, structurally.

Until the refactor lands, webcoder-ui-targeted initiatives can't validate the
AI coverage scanner. (Auth-ui-targeted ones still work, since the criterion
fires correctly there.)
