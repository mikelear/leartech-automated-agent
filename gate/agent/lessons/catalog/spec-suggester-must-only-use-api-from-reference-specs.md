---
id: spec-suggester-must-only-use-api-from-reference-specs
title: spec_suggester must only use API methods that appear verbatim in reference specs (no inferred / mirrored APIs)
captured_at: 2026-05-05T01:10:00Z
source:
  type: agent_run
  reference: pr_40_close_out_demo
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - spec_suggester
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/spec-suggester-must-only-use-api-from-reference-specs.md
slipped_past_criteria:
  - test_uses_data_testid_selectors
proposed_criterion: |
  test_suggested_spec_uses_only_reference_api_methods — (a v1.5 enhancement) parse
  the reference specs to extract the set of method calls used, then verify any
  spec_suggester output uses only methods from that set OR from a known-good
  Playwright API allowlist.
---

When `spec_suggester` drafts a Playwright spec, it must only call methods that
appear *verbatim* in the reference specs, OR are documented assertions in a known
Playwright API allowlist. **Do not infer or mirror methods from assertion shapes.**

## How this surfaced

PR #40's AI-drafted About spec contained:

    const aboutRendered = await aboutComponent.isAttached().catch(() => false);

`locator.isAttached()` **does not exist** in Playwright 1.59.1 (auth-ui's installed
version) — and as far as the public API docs show, it has never existed as a
Locator instance method in any version.

What DOES exist: `await expect(locator).toBeAttached()` — an assertion. The
suggester apparently mirrored the assertion form into a hypothetical method form.
The reference specs (01-page-loads.spec.ts, 02-login-form.spec.ts) use
`.isVisible()` exclusively for boolean checks; nothing in them suggests
`.isAttached()` is a thing.

## Why the existing prompt missed it

`SPEC_SUGGESTER_SYSTEM_PROMPT` says "matching the existing specs' idioms exactly:
same imports, same describe/test structure, same selector style…". It says nothing
explicit about *not inventing methods that aren't in the references*. Claude has
broad Playwright knowledge from training, and "Toolkit X has a method Y" is a
plausible hallucination when assertion `toBeY()` exists.

## The fix

Add to `SPEC_SUGGESTER_SYSTEM_PROMPT`:

> **API discipline**: Only call methods that appear *verbatim* in the reference
> specs OR are valid Playwright assertions (`expect(locator).toBeX()` shapes).
> Do not invent or infer methods that don't appear in the references — even if
> the corresponding assertion exists. If you need a check the references don't
> demonstrate, prefer the assertion form (`expect(locator).toBeAttached()`) over
> a hypothetical method form (`locator.isAttached()`).

## Future criterion

`test_suggested_spec_uses_only_reference_api_methods` (v1.5): parse reference
specs, extract method-call set, verify suggester output is a subset (plus a
known-good allowlist for asserts). Closes the loop programmatically rather than
relying on prompt discipline alone.

## Generic principle

When you ask an LLM to "match the patterns in these examples," it will reliably
match the *shape* but may decorate with plausible-sounding API calls from its
training data. The instruction must be explicit: *only use what's literally in
the examples, or document a precise allowlist*. Otherwise hallucinations leak in.
