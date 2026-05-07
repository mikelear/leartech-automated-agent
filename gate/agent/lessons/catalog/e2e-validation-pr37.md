---
id: e2e-validation-pr37
title: First end-to-end initiative run produced a real PR autonomously (PR #37)
captured_at: 2026-05-04T11:35:00Z
source:
  type: agent_run
  reference: pr_37_full_run
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: architecture
applies_to: []
status: encoded
encoded_in: []
---

Milestone marker — the v1 architecture demonstrated the full loop end-to-end.

**Initiative**: `auth-ui-home-component-spec` (fills auth-ui modernisation backlog item #4 — real component specs).

**Run cost**: 46 turns, $1.3786, ~35 min wall-clock (mostly Tekton wait).

**What the agent did across 2 iterations**:

1. **Iter 1**: read patterns from existing specs, wrote `home.component.spec.ts` with
   3 tests, opened PR #37, ran the gate. Failed because Tekton hadn't fired yet.
2. **Iter 2** (~5 min later): Re-ran the gate, saw the *real* failure
   (`test_coverage_meets_threshold` 72% < 80% threshold). Pulled the LCOV detail,
   identified `home.component.ts` at 50% with the `ngOnInit` authenticated branch +
   JWT decode never exercised. Refactored from 3 to 7 tests across 3 nested
   describes with a `setup()` factory and `FAKE_JWT` helper. Verified locally
   (100% line coverage). Pushed. When `az/test` stalled at PENDING for 35 min,
   triggered `/test test` chatops retest unprompted. Gate green: 6 passed / 0
   failed / 1 skipped. Posted "Ready for client review" sticky.

**Final delivery**: PR #37, +110/-0, single new spec file, no app code changes.
Reviewer-grade engineering produced autonomously from a YAML initiative.

**Why this matters**: the layered architecture (typed tools → MCP wrappers → Agent
SDK loop with system prompt) does what we hoped — Claude reasoned about *PRs*, not
about CI plumbing. The MCP layer turned out to be the load-bearing abstraction.
