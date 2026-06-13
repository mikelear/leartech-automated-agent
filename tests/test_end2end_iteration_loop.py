"""Tests for the v6p0.5 step-2 iteration-loop watcher.

The watcher (``gate/watcher/iteration_loop.py``) is a pure decision module:
given an :class:`IterationContext` it returns an :class:`IterationDecision`
and, on the respawn branch, a ``feedback_payloads`` list ready to be
attached to the next agent run's initiative.

These tests cover the five scenarios called out in the v6p0.5 step-2
initiative goal:

1. Real end2end failure → ``RESPAWN`` with a failure payload in the list.
2. Preview-infra failure (first occurrence) → ``SKIP_INFRA``; agent NOT
   re-spawned; no payload built.
3. Max iterations reached → ``ESCALATE_MAX_ITERATIONS``; manual-fix label
   + comment body rendered.
4. end2end-ui failure with screenshot URLs → payload carries those URLs
   and the prompt-block surfaces them with explicit fetch guidance.
5. ai-review feedback + end2end failure both present → both end up in the
   payloads list, end2end first.

Plus tests for:

- Idempotency (failures with a key in ``already_handled_keys`` drop out).
- Repeated preview-infra promotion to ``ESCALATE_REPEATED_INFRA``.
- :class:`gate.initiatives.loader.Initiative` accepts the new
  ``feedback_payloads`` field without breaking existing initiatives.
- The agent's startup prompt construction surfaces the formatted block
  before the standard initiative loop instructions (smoke check on the
  format function, since the actual prompt assembly lives in the SDK
  loop and is exercised end-to-end by the watcher's integration tests).
"""

from __future__ import annotations

from gate.tools.end2end_gate import End2EndFailure, End2EndTest
from gate.tools.playwright_artifacts import Artifact
from gate.watcher.iteration_loop import (
    DEFAULT_MAX_ITERATIONS,
    REPEATED_INFRA_THRESHOLD,
    IterationContext,
    IterationDecision,
    PriorAttempt,
    build_feedback_payloads,
    decide_action,
    failure_idempotency_key,
    format_feedback_payloads_for_prompt,
    manual_fix_comment_body,
    manual_fix_label,
    preview_infra_skip_comment_body,
    repeated_infra_escalation_comment_body,
)

# ─── Test fixtures ──────────────────────────────────────────────────────────


def _real_failure(gate: str = 'az/end2end') -> End2EndFailure:
    """Build a representative ``real_failure`` payload — an app-side bug."""
    return End2EndFailure(
        gate=gate,
        classification='real_failure',
        summary='2/3 checks passed',
        failed_tests=(
            End2EndTest(
                name='03-list',
                status='fail',
                message="GET /api/items expected 200, got 500: {'detail': 'internal error'}",
            ),
        ),
        actionable=True,
    )


def _preview_infra_failure(gate: str = 'az/end2end') -> End2EndFailure:
    """Build a representative preview-infra payload — the canonical PR #58 shape."""
    return End2EndFailure(
        gate=gate,
        classification='preview_infra',
        summary='1/4 checks passed',
        failed_tests=(
            End2EndTest(name='01-smoke', status='fail', message='GET /health/live HTTP 000 FAIL'),
            End2EndTest(name='02-auth', status='fail', message='GET /api/auth HTTP 000 FAIL'),
            End2EndTest(name='03-roundtrip', status='fail', message='POST /api/widget HTTP 000 FAIL'),
        ),
        actionable=False,
    )


def _ui_failure_with_artifacts() -> End2EndFailure:
    """Build an end2end-ui payload that carries screenshot + trace URLs."""
    artifacts = (
        Artifact(
            spec_name='01-login',
            kind='screenshot',
            url='https://artifacts.example/login.png',
            cluster='gcp',
        ),
        Artifact(
            spec_name='01-login',
            kind='trace',
            url='https://artifacts.example/login-trace.zip',
            cluster='gcp',
        ),
    )
    return End2EndFailure(
        gate='gcp/end2end-ui',
        classification='real_failure',
        summary='2/3 browser tests passed',
        failed_tests=(
            End2EndTest(
                name='01-login',
                status='fail',
                message='locator(...).click() — element not visible',
                screenshot_url='https://artifacts.example/login.png',
                trace_url='https://artifacts.example/login-trace.zip',
            ),
        ),
        actionable=True,
        artifact_urls=artifacts,
    )


# ─── decide_action ──────────────────────────────────────────────────────────


def test_real_failure_triggers_respawn() -> None:
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_real_failure(),),
        iteration_count=0,
        max_iterations=DEFAULT_MAX_ITERATIONS,
    )
    assert decide_action(ctx) == IterationDecision.RESPAWN


def test_preview_infra_failure_skips_first_occurrence() -> None:
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_preview_infra_failure(),),
        iteration_count=0,
        prior_infra_count=0,
    )
    assert decide_action(ctx) == IterationDecision.SKIP_INFRA


def test_preview_infra_repeated_escalates() -> None:
    """Second consecutive infra hit promotes to a cluster-operator escalation."""
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_preview_infra_failure(),),
        iteration_count=0,
        prior_infra_count=REPEATED_INFRA_THRESHOLD - 1,  # one already, this one tips
    )
    assert decide_action(ctx) == IterationDecision.ESCALATE_REPEATED_INFRA


def test_max_iterations_escalates() -> None:
    """Real failure at the budget ceiling escalates instead of spawning."""
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_real_failure(),),
        iteration_count=DEFAULT_MAX_ITERATIONS,
        max_iterations=DEFAULT_MAX_ITERATIONS,
    )
    assert decide_action(ctx) == IterationDecision.ESCALATE_MAX_ITERATIONS


def test_max_iterations_with_explicit_lower_ceiling() -> None:
    """Per-initiative max_iterations override is respected."""
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_real_failure(),),
        iteration_count=2,
        max_iterations=2,
    )
    assert decide_action(ctx) == IterationDecision.ESCALATE_MAX_ITERATIONS


def test_mixed_real_and_infra_real_wins_spawns() -> None:
    """A real_failure mixed with a preview_infra one still respawns —
    we'd rather fix the real bug; the infra failure clears or re-classifies
    on the next cycle."""
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(_real_failure(), _preview_infra_failure()),
        iteration_count=0,
        prior_infra_count=2,  # would otherwise tip into ESCALATE_REPEATED_INFRA
    )
    assert decide_action(ctx) == IterationDecision.RESPAWN


def test_no_failures_is_noop() -> None:
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(),
    )
    assert decide_action(ctx) == IterationDecision.NOOP


def test_already_handled_failure_is_filtered_out() -> None:
    """If the orchestrator already acted on this failure_id, decide_action
    treats the unhandled set as empty — preventing a double-spawn."""
    f = _real_failure()
    key = failure_idempotency_key(f)
    ctx = IterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        failures=(f,),
        iteration_count=0,
        already_handled_keys=frozenset({key}),
    )
    assert decide_action(ctx) == IterationDecision.NOOP


# ─── build_feedback_payloads ────────────────────────────────────────────────


def test_feedback_payloads_carry_real_failure_detail() -> None:
    payloads = build_feedback_payloads([_real_failure()])
    assert len(payloads) == 1
    p = payloads[0]
    assert p['kind'] == 'end2end_failure'
    assert p['gate'] == 'az/end2end'
    assert p['classification'] == 'real_failure'
    assert p['actionable'] is True
    assert p['failed_tests'][0]['name'] == '03-list'
    assert 'expected 200' in p['failed_tests'][0]['message']


def test_feedback_payloads_carry_screenshot_urls_for_ui_failures() -> None:
    payloads = build_feedback_payloads([_ui_failure_with_artifacts()])
    assert len(payloads) == 1
    p = payloads[0]
    assert p['gate'] == 'gcp/end2end-ui'
    # Per-test screenshot URL surfaces.
    assert p['failed_tests'][0]['screenshot_url'] == 'https://artifacts.example/login.png'
    assert p['failed_tests'][0]['trace_url'] == 'https://artifacts.example/login-trace.zip'
    # Full artifact_urls section is present too.
    assert 'artifact_urls' in p
    assert any(a['kind'] == 'screenshot' for a in p['artifact_urls'])
    assert any(a['kind'] == 'trace' for a in p['artifact_urls'])


def test_feedback_payloads_merge_end2end_and_ai_review() -> None:
    """Both kinds of feedback land in the same list — end2end first."""
    ai_review = {
        'cluster': 'gcp',
        'verdict': 'Needs Work',
        'score': 65,
        'summary': 'Two reviewers flagged the missing input validation.',
        'findings': [
            {'file': 'app/login.py', 'comment': 'Missing CSRF token check'},
        ],
    }
    payloads = build_feedback_payloads(
        [_real_failure()],
        ai_review_findings=[ai_review],
    )
    assert len(payloads) == 2
    assert payloads[0]['kind'] == 'end2end_failure'
    assert payloads[1]['kind'] == 'ai_review_finding'
    assert payloads[1]['verdict'] == 'Needs Work'


def test_feedback_payloads_ai_review_kind_defaulted_when_missing() -> None:
    """If the ai-review path emits a finding without 'kind', we add it."""
    payloads = build_feedback_payloads(
        [],
        ai_review_findings=[{'cluster': 'az', 'verdict': 'Fail', 'score': 30}],
    )
    assert payloads[0]['kind'] == 'ai_review_finding'


def test_feedback_payloads_empty_for_empty_inputs() -> None:
    assert build_feedback_payloads([]) == []
    assert build_feedback_payloads([], ai_review_findings=[]) == []


# ─── format_feedback_payloads_for_prompt ────────────────────────────────────


def test_format_returns_empty_string_for_empty_payloads() -> None:
    """No prior attempt → no prompt block (callers can unconditionally
    concatenate the result)."""
    assert format_feedback_payloads_for_prompt([]) == ''


def test_format_surfaces_end2end_failure_name_and_message() -> None:
    payloads = build_feedback_payloads([_real_failure()])
    block = format_feedback_payloads_for_prompt(payloads)
    assert 'Previous-attempt failure context' in block
    assert 'az/end2end' in block
    assert 'real_failure' in block
    assert '03-list' in block
    assert 'expected 200' in block


def test_format_surfaces_screenshot_urls_with_fetch_guidance() -> None:
    """For UI failures, screenshots are surfaced AND the agent is told how
    to fetch them (Read tool needs a local file; curl downloads first)."""
    payloads = build_feedback_payloads([_ui_failure_with_artifacts()])
    block = format_feedback_payloads_for_prompt(payloads)
    assert 'https://artifacts.example/login.png' in block
    assert 'curl' in block.lower()
    # The "fetch first then Read" guidance must be present somewhere in
    # the block so the agent doesn't try to Read an HTTPS URL directly.
    assert 'read' in block.lower()


def test_format_emits_both_end2end_and_ai_review_sections() -> None:
    payloads = build_feedback_payloads(
        [_real_failure()],
        ai_review_findings=[
            {
                'cluster': 'gcp',
                'verdict': 'Needs Work',
                'score': 65,
                'findings': [
                    {'file': 'app/foo.py', 'comment': 'Missing test coverage'},
                ],
            }
        ],
    )
    block = format_feedback_payloads_for_prompt(payloads)
    assert 'az/end2end' in block
    assert 'AI code review' in block
    assert 'app/foo.py' in block


# ─── failure_idempotency_key ────────────────────────────────────────────────


def test_idempotency_key_is_stable_across_calls() -> None:
    f = _real_failure()
    assert failure_idempotency_key(f) == failure_idempotency_key(f)


def test_idempotency_key_differs_per_gate() -> None:
    a = _real_failure(gate='az/end2end')
    b = _real_failure(gate='gcp/end2end')
    assert failure_idempotency_key(a) != failure_idempotency_key(b)


def test_idempotency_key_incorporates_pipelinerun_and_sha() -> None:
    f = _real_failure()
    assert failure_idempotency_key(f, pipelinerun_name='pr-58-abcd') != failure_idempotency_key(
        f, pipelinerun_name='pr-58-wxyz'
    )
    assert failure_idempotency_key(f, sha='aaaa') != failure_idempotency_key(f, sha='bbbb')


def test_idempotency_key_differs_per_classification() -> None:
    """A real_failure and a preview_infra with the same gate+tests should
    produce different keys — the orchestrator treats them as distinct
    events."""
    real = _real_failure()
    infra = End2EndFailure(
        gate=real.gate,
        classification='preview_infra',
        summary=real.summary,
        failed_tests=real.failed_tests,
    )
    assert failure_idempotency_key(real) != failure_idempotency_key(infra)


# ─── Comment bodies ─────────────────────────────────────────────────────────


def test_manual_fix_label_is_stable() -> None:
    """The label is part of the Lighthouse Keeper merge contract; if it
    changes silently Keeper stops blocking auto-merge."""
    assert manual_fix_label() == 'do-not-merge/manual-fix'


def test_manual_fix_comment_includes_iteration_summary() -> None:
    body = manual_fix_comment_body(
        iteration_count=3,
        max_iterations=3,
        attempts=[
            PriorAttempt(
                run_id='abc123',
                started_at='2026-06-13T15:00:00Z',
                status='failed',
                summary='Tried to fix /health/live; still 500.',
            ),
            PriorAttempt(
                run_id='def456',
                started_at='2026-06-13T15:30:00Z',
                status='failed',
                summary='Tried to fix hydra DNS; still NXDOMAIN.',
            ),
        ],
        last_failure=_real_failure(),
    )
    assert 'manual fix required' in body.lower()
    assert 'abc123' in body and 'def456' in body
    assert '/health/live' in body
    assert '03-list' in body  # the failure name
    assert manual_fix_label() in body  # the label name is cited for human action


def test_manual_fix_comment_handles_no_attempts() -> None:
    """Defensive: if the DB row didn't capture attempts, render something
    still-useful rather than crashing."""
    body = manual_fix_comment_body(
        iteration_count=3,
        max_iterations=3,
        attempts=[],
        last_failure=None,
    )
    assert 'no recorded attempts' in body.lower()


def test_preview_infra_skip_comment_caps_message_list() -> None:
    body = preview_infra_skip_comment_body(_preview_infra_failure())
    assert 'preview_infra' in body.lower()
    assert '/test end2end' in body
    # The canonical PR #58 fixture has 3 failed tests — message list shows
    # all of them (cap is 3).
    assert '01-smoke' in body
    assert '02-auth' in body
    assert '03-roundtrip' in body


def test_preview_infra_skip_comment_truncates_when_over_three() -> None:
    big = End2EndFailure(
        gate='az/end2end',
        classification='preview_infra',
        summary='1/5 checks passed',
        failed_tests=tuple(End2EndTest(name=f'check-{i}', status='fail', message='HTTP 000') for i in range(5)),
    )
    body = preview_infra_skip_comment_body(big)
    assert 'and 2 more' in body  # cap=3 means 2 of the 5 are summarised away


def test_repeated_infra_escalation_includes_count() -> None:
    body = repeated_infra_escalation_comment_body(
        _preview_infra_failure(),
        prior_infra_count=1,
    )
    assert 'Repeated preview-infra' in body
    assert manual_fix_label() in body  # the escalation path applies the same label


# ─── Initiative.feedback_payloads schema ────────────────────────────────────


def test_initiative_accepts_feedback_payloads_field() -> None:
    """The watcher constructs a new initiative with feedback_payloads
    populated; the loader must accept it without barfing on
    extra='forbid'."""
    from gate.initiatives.loader import load_initiative_from_yaml

    yaml_body = """
    name: example
    repo: leartech-auth-ui
    branch: agent/example
    goal: respawned by watcher
    feedback_payloads:
      - kind: end2end_failure
        gate: az/end2end
        classification: real_failure
        summary: 1/2 checks passed
        actionable: true
        failed_tests:
          - name: 01-smoke
            message: HTTP 500
    """
    initiative = load_initiative_from_yaml(yaml_body)
    assert len(initiative.feedback_payloads) == 1
    assert initiative.feedback_payloads[0]['kind'] == 'end2end_failure'
    assert initiative.feedback_payloads[0]['gate'] == 'az/end2end'


def test_initiative_defaults_feedback_payloads_to_empty() -> None:
    """Fresh runs (the common case) carry no feedback_payloads."""
    from gate.initiatives.loader import load_initiative_from_yaml

    yaml_body = """
    name: example
    repo: leartech-auth-ui
    branch: agent/example
    goal: first attempt
    """
    initiative = load_initiative_from_yaml(yaml_body)
    assert initiative.feedback_payloads == []
