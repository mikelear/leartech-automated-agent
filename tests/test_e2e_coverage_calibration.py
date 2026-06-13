"""Unit tests for gate.tools.e2e_coverage — pre-PR self-review verdict.

Covers the matrix from the v6p0.5 step-3 initiative goal:

    * New endpoint added + no e2e change      → halt
    * New endpoint added + e2e extended       → proceed
    * Pure refactor + no e2e change           → proceed
    * UI new screen + e2e-ui extended         → proceed
    * UI new screen + only e2e (backend) ext  → halt (UI needs UI tests)
    * PR comment `/skip-e2e-check`            → bypass honoured

Plus a few additional cases that pin the calibration-prompt contract:

    * Go router additions detected as backend endpoints
    * Click CLI commands flagged as new behaviour
    * Bypass author = agent itself → NOT honoured (must be human)
    * Playwright spec added (without scripts/e2e-ui.sh change) counts as
      UI coverage
    * Verdict JSON serialisation is stable

The initiative prompt also asserts the prompt now contains the E2E hard-rule
block — pinned in :func:`test_initiative_prompt_includes_e2e_hard_rule`.
"""

from __future__ import annotations

import textwrap

from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT
from gate.tools.e2e_coverage import (
    AGENT_LOGINS,
    SkipDirective,
    detect_backend_e2e_extension,
    detect_new_backend_endpoints,
    detect_ui_e2e_extension,
    evaluate_e2e_coverage,
    find_skip_directive,
)

# ─── Fixtures ────────────────────────────────────────────────────────────────


NEW_PY_ENDPOINT_DIFF = textwrap.dedent(
    """
    diff --git a/app/routers/plans.py b/app/routers/plans.py
    index 1111111..2222222 100644
    --- a/app/routers/plans.py
    +++ b/app/routers/plans.py
    @@ -10,3 +10,8 @@ async def list_plans():
         return {"plans": []}
    +
    +
    +@router.post("/plans")
    +async def create_plan(payload: dict) -> dict:
    +    return {"created": True}
    """
).strip()


NEW_PY_ENDPOINT_PLUS_E2E_DIFF = (
    NEW_PY_ENDPOINT_DIFF
    + '\n'
    + textwrap.dedent(
        """
    diff --git a/scripts/e2e.sh b/scripts/e2e.sh
    index aaaa..bbbb 100644
    --- a/scripts/e2e.sh
    +++ b/scripts/e2e.sh
    @@ -80,3 +80,7 @@ assert_status "POST /initiatives/_validate (422)" "422" "${got}"
    +
    +# New: POST /plans round-trip.
    +got=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BASE_URL}/plans" -d '{}')
    +assert_status "POST /plans (200)" "200" "${got}"
    """
    ).strip()
)


PURE_REFACTOR_DIFF = textwrap.dedent(
    """
    diff --git a/app/state.py b/app/state.py
    index 1111111..2222222 100644
    --- a/app/state.py
    +++ b/app/state.py
    @@ -10,3 +10,3 @@ def transition_to(state: str) -> None:
    -    # old comment
    +    # new comment, same behaviour
         _emit(state)
    """
).strip()


NEW_UI_COMPONENT_DIFF = textwrap.dedent(
    """
    diff --git a/src/app/profile/profile.component.ts b/src/app/profile/profile.component.ts
    new file mode 100644
    index 0000000..aaaa
    --- /dev/null
    +++ b/src/app/profile/profile.component.ts
    @@ -0,0 +1,7 @@
    +import { Component } from '@angular/core';
    +
    +@Component({
    +  selector: 'app-profile',
    +  templateUrl: './profile.component.html',
    +})
    +export class ProfileComponent {}
    diff --git a/src/app/app-routing.module.ts b/src/app/app-routing.module.ts
    index 1111..2222 100644
    --- a/src/app/app-routing.module.ts
    +++ b/src/app/app-routing.module.ts
    @@ -5,3 +5,4 @@ const routes: Routes = [
       { path: 'home', component: HomeComponent },
    +  { path: 'profile', component: ProfileComponent },
     ];
    """
).strip()


NEW_UI_PLUS_E2E_UI_DIFF = (
    NEW_UI_COMPONENT_DIFF
    + '\n'
    + textwrap.dedent(
        """
    diff --git a/scripts/e2e-ui.sh b/scripts/e2e-ui.sh
    index aaaa..bbbb 100644
    --- a/scripts/e2e-ui.sh
    +++ b/scripts/e2e-ui.sh
    @@ -10,3 +10,5 @@ npx playwright test
    +
    +# New: profile route smoke.
    +npx playwright test --grep profile
    """
    ).strip()
)


NEW_UI_PLUS_BACKEND_ONLY_E2E_DIFF = (
    NEW_UI_COMPONENT_DIFF
    + '\n'
    + textwrap.dedent(
        """
    diff --git a/scripts/e2e.sh b/scripts/e2e.sh
    index aaaa..bbbb 100644
    --- a/scripts/e2e.sh
    +++ b/scripts/e2e.sh
    @@ -10,3 +10,4 @@ assert_status "/health/live" "200" "${got}"
    +# Unrelated backend probe — does NOT cover the new UI surface.
    +echo "backend e2e extended"
    """
    ).strip()
)


NEW_PLAYWRIGHT_SPEC_DIFF = (
    NEW_UI_COMPONENT_DIFF
    + '\n'
    + textwrap.dedent(
        """
    diff --git a/end2end-ui/profile.spec.ts b/end2end-ui/profile.spec.ts
    new file mode 100644
    index 0000000..cccc
    --- /dev/null
    +++ b/end2end-ui/profile.spec.ts
    @@ -0,0 +1,6 @@
    +import { test, expect } from '@playwright/test';
    +
    +test('profile route renders', async ({ page }) => {
    +  await page.goto('/profile');
    +  await expect(page.getByTestId('profile-page')).toBeVisible();
    +});
    """
    ).strip()
)


GO_ROUTER_DIFF = textwrap.dedent(
    """
    diff --git a/cmd/server/main.go b/cmd/server/main.go
    index 1111..2222 100644
    --- a/cmd/server/main.go
    +++ b/cmd/server/main.go
    @@ -25,3 +25,4 @@ func main() {
         router := gin.Default()
         router.GET("/health/live", livenessHandler)
    +    router.POST("/quotes", quotesHandler)
         router.Run()
    """
).strip()


CLICK_CLI_DIFF = textwrap.dedent(
    """
    diff --git a/app/agent_cli/commands.py b/app/agent_cli/commands.py
    index 1111..2222 100644
    --- a/app/agent_cli/commands.py
    +++ b/app/agent_cli/commands.py
    @@ -50,3 +50,6 @@ def existing_command():
         pass
    +
    +@cli.command("plan")
    +def plan_command():
    +    pass
    """
).strip()


# ─── Backend endpoint detection ──────────────────────────────────────────────


def test_detects_new_python_decorator_endpoint() -> None:
    hits = detect_new_backend_endpoints(NEW_PY_ENDPOINT_DIFF)
    assert len(hits) == 1
    assert '@router.post("/plans")' in hits[0]


def test_detects_new_go_router_endpoint() -> None:
    hits = detect_new_backend_endpoints(GO_ROUTER_DIFF)
    assert any('router.POST' in h for h in hits)


def test_detects_new_click_cli_command() -> None:
    hits = detect_new_backend_endpoints(CLICK_CLI_DIFF)
    assert any('@cli.command' in h for h in hits)


def test_ignores_endpoints_added_under_tests() -> None:
    test_diff = textwrap.dedent(
        """
        diff --git a/tests/test_routes.py b/tests/test_routes.py
        new file mode 100644
        index 0000..1111
        --- /dev/null
        +++ b/tests/test_routes.py
        @@ -0,0 +1,5 @@
        +from fastapi import APIRouter
        +router = APIRouter()
        +@router.get("/spam")
        +def spam():
        +    return {}
        """
    ).strip()
    assert detect_new_backend_endpoints(test_diff) == []


def test_pure_refactor_yields_no_backend_endpoints() -> None:
    assert detect_new_backend_endpoints(PURE_REFACTOR_DIFF) == []


# ─── Coverage-file detection ─────────────────────────────────────────────────


def test_detects_e2e_sh_extension() -> None:
    assert detect_backend_e2e_extension(NEW_PY_ENDPOINT_PLUS_E2E_DIFF) is True


def test_does_not_flag_e2e_when_unmodified() -> None:
    assert detect_backend_e2e_extension(NEW_PY_ENDPOINT_DIFF) is False


def test_detects_e2e_ui_sh_extension() -> None:
    assert detect_ui_e2e_extension(NEW_UI_PLUS_E2E_UI_DIFF) is True


def test_detects_playwright_spec_extension() -> None:
    """A new end2end-ui/*.spec.ts is acceptable UI coverage even without e2e-ui.sh changes."""
    assert detect_ui_e2e_extension(NEW_PLAYWRIGHT_SPEC_DIFF) is True


def test_backend_only_change_is_not_ui_coverage() -> None:
    assert detect_ui_e2e_extension(NEW_PY_ENDPOINT_PLUS_E2E_DIFF) is False


# ─── /skip-e2e-check directive ───────────────────────────────────────────────


def test_finds_skip_directive_from_human() -> None:
    comments = [
        {'id': 42, 'body': 'looks good', 'user': {'login': 'mikelear'}},
        {'id': 43, 'body': '/skip-e2e-check infra-only PR', 'user': {'login': 'mikelear'}},
    ]
    directive = find_skip_directive(comments)
    assert directive is not None
    assert directive.actor == 'mikelear'
    assert directive.comment_id == 43
    assert directive.reason == 'infra-only PR'


def test_ignores_skip_directive_from_agent_login() -> None:
    """The agent must never bypass its own check — only humans."""
    for bot in AGENT_LOGINS:
        comments = [{'id': 1, 'body': '/skip-e2e-check', 'user': {'login': bot}}]
        assert find_skip_directive(comments) is None, bot


def test_skip_directive_only_matches_exact_token() -> None:
    """``/skip-e2e-checkers`` is not a valid bypass."""
    comments = [{'id': 1, 'body': '/skip-e2e-checkers', 'user': {'login': 'mikelear'}}]
    assert find_skip_directive(comments) is None


def test_skip_directive_with_no_reason() -> None:
    comments = [{'id': 7, 'body': '/skip-e2e-check', 'user': {'login': 'mikelear'}}]
    directive = find_skip_directive(comments)
    assert directive is not None
    assert directive.reason is None


def test_skip_directive_picks_most_recent_when_multiple() -> None:
    comments = [
        {'id': 1, 'body': '/skip-e2e-check first', 'user': {'login': 'mikelear'}},
        {'id': 2, 'body': '/skip-e2e-check second', 'user': {'login': 'mikelear'}},
    ]
    directive = find_skip_directive(comments)
    assert directive is not None
    assert directive.comment_id == 2
    assert directive.reason == 'second'


# ─── Verdict — the matrix from the goal ──────────────────────────────────────


def test_new_endpoint_no_e2e_halts() -> None:
    verdict = evaluate_e2e_coverage(diff=NEW_PY_ENDPOINT_DIFF)
    assert verdict.action == 'halt'
    assert verdict.new_backend_endpoints
    assert verdict.backend_covered is False
    assert any('scripts/e2e.sh' in r for r in verdict.reasons)


def test_new_endpoint_with_e2e_proceeds() -> None:
    verdict = evaluate_e2e_coverage(diff=NEW_PY_ENDPOINT_PLUS_E2E_DIFF)
    assert verdict.action == 'proceed'
    assert verdict.new_backend_endpoints
    assert verdict.backend_covered is True
    assert any('Backend coverage extended' in r for r in verdict.reasons)


def test_pure_refactor_proceeds() -> None:
    verdict = evaluate_e2e_coverage(diff=PURE_REFACTOR_DIFF)
    assert verdict.action == 'proceed'
    assert not verdict.new_backend_endpoints
    assert verdict.new_ui_surface.is_empty
    assert any('No new endpoints' in r for r in verdict.reasons)


def test_ui_new_screen_with_e2e_ui_proceeds() -> None:
    verdict = evaluate_e2e_coverage(diff=NEW_UI_PLUS_E2E_UI_DIFF)
    assert verdict.action == 'proceed'
    assert not verdict.new_ui_surface.is_empty
    assert verdict.ui_covered is True


def test_ui_new_screen_with_only_backend_e2e_halts() -> None:
    """UI surface NEEDS UI tests — backend e2e extension alone is not enough."""
    verdict = evaluate_e2e_coverage(diff=NEW_UI_PLUS_BACKEND_ONLY_E2E_DIFF)
    assert verdict.action == 'halt'
    assert verdict.ui_covered is False
    assert verdict.backend_covered is True
    assert any('UI surface' in r for r in verdict.reasons)


def test_ui_new_screen_with_playwright_spec_proceeds() -> None:
    """Adding a Playwright spec under end2end-ui/ is acceptable UI coverage."""
    verdict = evaluate_e2e_coverage(diff=NEW_PLAYWRIGHT_SPEC_DIFF)
    assert verdict.action == 'proceed'
    assert verdict.ui_covered is True


def test_skip_directive_bypasses_halt() -> None:
    """Even though the diff would normally halt, /skip-e2e-check bypasses."""
    comments = [
        {'id': 99, 'body': '/skip-e2e-check infrastructure only', 'user': {'login': 'mikelear'}},
    ]
    verdict = evaluate_e2e_coverage(diff=NEW_PY_ENDPOINT_DIFF, comments=comments)
    assert verdict.action == 'proceed'
    assert verdict.bypass is not None
    assert verdict.bypass.actor == 'mikelear'
    assert verdict.bypass.comment_id == 99
    assert verdict.bypass.reason == 'infrastructure only'
    # Reasons trail must cite the bypass for audit.
    assert any('/skip-e2e-check' in r for r in verdict.reasons)


def test_skip_directive_by_agent_does_not_bypass() -> None:
    """Confirms the audit invariant: an agent-posted /skip-e2e-check is ignored."""
    comments = [
        {'id': 1, 'body': '/skip-e2e-check', 'user': {'login': 'leartech-automated-agent'}},
    ]
    verdict = evaluate_e2e_coverage(diff=NEW_PY_ENDPOINT_DIFF, comments=comments)
    assert verdict.action == 'halt'
    assert verdict.bypass is None


def test_verdict_to_dict_is_serialisable() -> None:
    import json

    verdict = evaluate_e2e_coverage(
        diff=NEW_PY_ENDPOINT_DIFF,
        comments=[{'id': 1, 'body': '/skip-e2e-check', 'user': {'login': 'mikelear'}}],
    )
    payload = verdict.to_dict()
    # Must round-trip through json without exploding.
    json.dumps(payload)
    assert payload['kind'] == 'e2e_coverage_verdict'
    assert payload['action'] == 'proceed'
    assert payload['bypass']['actor'] == 'mikelear'


# ─── Calibration prompt wiring ───────────────────────────────────────────────


def test_initiative_prompt_includes_e2e_hard_rule() -> None:
    """The system prompt must carry the E2E coverage hard-rule block.

    If a future refactor splits the prompt across files or drops the section
    heading, this assertion catches it. Pick a load-bearing phrase that's
    unlikely to be reworded without intent.
    """
    assert 'E2E coverage is non-negotiable' in INITIATIVE_SYSTEM_PROMPT
    assert 'scripts/e2e.sh' in INITIATIVE_SYSTEM_PROMPT
    assert 'scripts/e2e-ui.sh' in INITIATIVE_SYSTEM_PROMPT
    assert '/skip-e2e-check' in INITIATIVE_SYSTEM_PROMPT


def test_initiative_prompt_includes_pr_description_template() -> None:
    """The prompt must require the three-section PR description."""
    assert '## E2E coverage added' in INITIATIVE_SYSTEM_PROMPT
    assert '## Summary' in INITIATIVE_SYSTEM_PROMPT
    assert '## Test plan' in INITIATIVE_SYSTEM_PROMPT
    # The bypass must be flagged as human-only in the prompt.
    assert 'humans bypass' in INITIATIVE_SYSTEM_PROMPT or 'humans cancel' in INITIATIVE_SYSTEM_PROMPT


# ─── Test-file filtering — fixtures embedded in unit tests must not halt ─────


def test_ui_fixtures_inside_python_unit_tests_do_not_trigger_halt() -> None:
    """The diff that ADDS a unit-test fixture containing Angular sample code must NOT halt.

    This is the self-dogfood case: the PR that introduces
    ``tests/test_e2e_coverage_calibration.py`` itself embeds
    ``selector: 'app-profile'`` and ``path: 'profile'`` inside string fixtures.
    Without test-file filtering, those substrings would be picked up as
    "new UI surface" by the underlying ui_surface_diff parser. With filtering
    they're correctly ignored.
    """
    diff_adding_unit_test = textwrap.dedent(
        '''
        diff --git a/tests/test_my_module.py b/tests/test_my_module.py
        new file mode 100644
        index 0000..1111
        --- /dev/null
        +++ b/tests/test_my_module.py
        @@ -0,0 +1,10 @@
        +import textwrap
        +
        +FIXTURE = textwrap.dedent("""
        +    @router.post("/quotes")
        +    selector: 'app-fixture'
        +    { path: 'fixture', component: FixtureComponent }
        +""")
        +
        +def test_thing():
        +    assert FIXTURE
        '''
    ).strip()
    verdict = evaluate_e2e_coverage(diff=diff_adding_unit_test)
    assert verdict.action == 'proceed'
    assert verdict.new_backend_endpoints == ()
    assert verdict.new_ui_surface.is_empty


# ─── Smoke: SkipDirective shape stays stable ─────────────────────────────────


def test_skip_directive_is_dataclass_with_expected_fields() -> None:
    d = SkipDirective(actor='x', comment_id=1, reason='y')
    assert d.actor == 'x'
    assert d.comment_id == 1
    assert d.reason == 'y'
