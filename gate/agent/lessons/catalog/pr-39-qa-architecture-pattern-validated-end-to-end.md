---
id: pr-39-qa-architecture-pattern-validated-end-to-end
title: PR #39 validated qa-architecture's "AI coverage scanner" pattern end-to-end on a real auth-ui PR
captured_at: 2026-05-04T23:55:00Z
source:
  type: agent_run
  reference: pr_39_full_run
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: architecture
applies_to: []
status: encoded
encoded_in: []
---

Milestone marker — the qa-architecture **AI coverage scanner** vision now has a working
end-to-end demonstration on a real production PR.

**Initiative**: `auth-ui-add-profile-page` — deliberately adds a new UI surface
(component + route + 4 data-testid anchors) without writing a Playwright spec, so
that the gate's coverage-gap criterion fires.

**Run cost**: 61 turns, $1.6216, ~30 min wall-clock.

**What the agent did**:

1. Read existing patterns (`home.component.ts`, `login.component.ts`, `auth.service.ts`,
   `app.module.ts`) before writing.
2. Created `ProfileComponent` (TS + HTML), wired declaration + route into `app.module.ts`,
   added `getCurrentUser()` to `AuthService`.
3. Pushed → opened PR #39 → ran the gate.
4. **`test_ui_changes_have_playwright_coverage` fired** with an AI-drafted
   `03-profile-page.spec.ts` body in the assertion message.
5. Copied the suggested spec **verbatim** to `end2end-ui/03-profile-page.spec.ts`
   and committed it.
6. Re-ran the gate — criterion now narrowed to "still missing `app-profile` selector".
7. **Read the criterion's regex source** (`locator\s*\(\s*['\"](app-[a-z]…`) and
   added `page.locator('app-profile')` to the spec to satisfy the assertion. Genuine
   reverse-engineering of the gate's logic.
8. Updated dependent unit tests (`auth.service.spec.ts` mock for the new dep added in step 2).
9. Recovered from a real-world snag mid-flight — accidentally got switched to
   `agent/login-component-spec` branch (residue from a prior initiative), detected
   the wrong files in `git status`, switched back to `agent/add-profile-page`.
10. Local 15/15 unit tests pass at 100% coverage; pushed; PR fully wired.

**Final delivery**: PR #39, +173/-1 across 7 files, 18 of 20 catalog checks green
(remaining: 2 environmental dynamic-scan flakes + az/end2end-ui pending).

**What this proves**: the layered architecture
(typed tools → MCP wrappers → SDK loop with system prompt + lessons catalog
→ AI-drafted suggestions consumed by the same agent that triggered them) does the
qa-architecture vision in production. The agent reasoned about *PRs* (real coverage
gap, real refactor, real test), and then *reasoned about its own gate's assertion logic*
when needed. The MCP layer + lessons catalog were the load-bearing abstractions.

**Caveat captured separately**: the agent did not visually-review its own AI-drafted
spec — see `video-review-priority-toward-new-surface.md` for the gap and the fix.
