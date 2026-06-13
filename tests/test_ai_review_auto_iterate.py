"""Tests for v6p0.6 step-4 auto-iterate-on-ai-review-red-findings.

These cover the six scenarios called out in the initiative goal:

1. All green verdict → no action
2. Yellow-only verdict → no action
3. Red findings + aggregate 95 → iterate with structured findings
4. Red findings + aggregate 70 → escalate (Class A real objection)
5. Same verdict re-posted → no double-iterate (idempotency)
6. New verdict supersedes → re-evaluate (different findings → new key)

Plus structural tests on the parser, payload shape, and prompt-block
rendering — those are the seams that integrate with the rest of the
watcher (v6p0.5 step 2 + structured artefact parsers from v6p0.6 step 1).
"""

from __future__ import annotations

from gate.tools.ai_review import (
    AIReviewFinding,
    AIReviewVerdict,
    parse_ai_review_comment,
    parse_ai_review_findings,
)
from gate.watcher.ai_review_iteration import (
    SCORE_CONFIDENCE_THRESHOLD,
    AIReviewDecision,
    AIReviewIterationContext,
    build_ai_review_failure_payload,
    build_ai_review_failure_payloads,
    decide_ai_review_action,
    format_ai_review_failure_payload,
    verdict_idempotency_key,
)
from gate.watcher.iteration_loop import (
    build_feedback_payloads,
    format_feedback_payloads_for_prompt,
)

# ─── Sample verdict comment bodies ──────────────────────────────────────────

# Excellent / passing. No Issues Found section at all.
ALL_GREEN_BODY = """## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]`

**Scores:** 95 | 95 (avg: 95) | **3 reviewers**

> All reviewers passed. This PR is eligible for auto-merge.
"""

# Yellow-only — Needs Work but no red findings.
YELLOW_ONLY_BODY = """## :warning: AI Code Review: **88/100 — Good** `[gcp]`

**Scores:** 95 | 80 (avg: 88) | **2 reviewers**

### Issues Found

- :yellow_circle: [claude] `cmd/server/main.go:67` File operation without error handling
- :blue_circle: [deepseek] `OWNERS:5` Unnecessary trailing whitespace at end of file

### Suggestions

- [claude] Consider using embed package
"""

# Red findings present, score 95 (≥86) → iterate.
RED_HIGH_SCORE_BODY = """## :warning: AI Code Review: **95/100 — Good** `[gcp]`

**Scores:** 95 | 95 (avg: 95) | **3 reviewers**
**:red_circle: Critical issues found — auto-fail regardless of score**

### Issues Found

- :red_circle: [claude] `cmd/server/main.go:67` Missing error handling on file operation
- :yellow_circle: [deepseek] `OWNERS:5` Trailing whitespace at end of file

### Suggestions

- [claude] Use embed package
"""

# Red findings present, score 70 (<86) → escalate.
RED_LOW_SCORE_BODY = """## :warning: AI Code Review: **70/100 — Needs Work** `[az]`

**Scores:** 70 | 70 (avg: 70) | **2 reviewers**

### Issues Found

- :red_circle: [claude] `Dockerfile:27` Hardcoded secret 'auth' used as user password
- :red_circle: [deepseek] `cmd/server/main.go:56` Error from mongoStore.Close(ctx) is silently discarded
"""

# Same shape as RED_HIGH_SCORE_BODY but with a single additional yellow that
# does not alter the red set. Should produce the SAME idempotency key as
# RED_HIGH_SCORE_BODY if it represents the same findings — but here we adjust
# one of the red findings' fix_hint, which MUST produce a different key.
RED_HIGH_SCORE_SUPERSEDED_BODY = """## :warning: AI Code Review: **95/100 — Good** `[gcp]`

**Scores:** 95 | 95 (avg: 95) | **3 reviewers**

### Issues Found

- :red_circle: [claude] `cmd/server/main.go:67` Missing error handling on file operation AND on listener close
- :yellow_circle: [deepseek] `OWNERS:5` Trailing whitespace at end of file
"""


# ─── Parser tests ────────────────────────────────────────────────────────────


def test_parse_ai_review_findings_extracts_red_yellow_and_blue() -> None:
    body = """## :warning: AI Code Review: **88/100 — Good** `[az]`
### Issues Found

- :red_circle: [claude] `a.go:1` red-text
- :yellow_circle: [deepseek] `b.go:2` yellow-text
- :blue_circle: [claude] `c.go:3` blue-text
"""
    findings = parse_ai_review_findings(body)
    assert len(findings) == 3
    assert findings[0].severity == 'red'
    assert findings[0].reviewer == 'claude'
    assert findings[0].location == 'a.go:1'
    assert findings[0].fix_hint == 'red-text'
    assert findings[1].severity == 'yellow'
    assert findings[2].severity == 'blue'


def test_parse_ai_review_findings_skips_section_outside_issues_found() -> None:
    """Bullets in Suggestions or feedback panels MUST NOT be parsed as findings."""
    body = """## :warning: AI Code Review: **88/100 — Good** `[gcp]`
### Issues Found

- :red_circle: [claude] `a.go:1` real-issue

### Suggestions

- :red_circle: [claude] `b.go:9` looks-like-an-issue-but-isnt-one
"""
    findings = parse_ai_review_findings(body)
    assert len(findings) == 1
    assert findings[0].location == 'a.go:1'


def test_parse_ai_review_findings_empty_when_no_issues_section() -> None:
    body = """## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]`

> All reviewers passed.
"""
    assert parse_ai_review_findings(body) == []


def test_parse_ai_review_comment_populates_findings() -> None:
    verdict = parse_ai_review_comment(RED_HIGH_SCORE_BODY)
    assert verdict is not None
    assert verdict.score == 95
    assert len(verdict.findings) == 2
    assert len(verdict.red_findings) == 1
    assert verdict.red_findings[0].location == 'cmd/server/main.go:67'


def test_parse_ai_review_comment_handles_no_findings_section() -> None:
    verdict = parse_ai_review_comment(ALL_GREEN_BODY)
    assert verdict is not None
    assert verdict.findings == ()
    assert verdict.red_findings == ()


def test_ai_review_finding_to_dict_round_trip() -> None:
    f = AIReviewFinding(
        severity='red',
        reviewer='claude',
        location='cmd/main.go:1',
        fix_hint='fix it',
    )
    assert f.to_dict() == {
        'severity': 'red',
        'reviewer': 'claude',
        'location': 'cmd/main.go:1',
        'fix_hint': 'fix it',
    }


# ─── decide_ai_review_action ─────────────────────────────────────────────────


def _verdict_all_green() -> AIReviewVerdict:
    parsed = parse_ai_review_comment(ALL_GREEN_BODY)
    assert parsed is not None
    return parsed


def _verdict_yellow_only() -> AIReviewVerdict:
    parsed = parse_ai_review_comment(YELLOW_ONLY_BODY)
    assert parsed is not None
    return parsed


def _verdict_red_high_score() -> AIReviewVerdict:
    parsed = parse_ai_review_comment(RED_HIGH_SCORE_BODY)
    assert parsed is not None
    return parsed


def _verdict_red_low_score() -> AIReviewVerdict:
    parsed = parse_ai_review_comment(RED_LOW_SCORE_BODY)
    assert parsed is not None
    return parsed


def test_all_green_verdict_is_noop() -> None:
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(_verdict_all_green(),),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.NOOP


def test_yellow_only_verdict_is_noop() -> None:
    """Yellow findings alone don't trigger iteration — the agent only spawns
    on red. This is the "Needs Work but nothing critical" path."""
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(_verdict_yellow_only(),),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.NOOP


def test_red_findings_high_score_triggers_respawn() -> None:
    """Aggregate score ≥86 + red findings → iterate. Reviewers trust their
    own verdict; the red findings are actionable."""
    verdict = _verdict_red_high_score()
    assert verdict.score >= SCORE_CONFIDENCE_THRESHOLD
    assert verdict.red_findings, 'fixture should have at least one red finding'
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(verdict,),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.RESPAWN


def test_red_findings_low_score_triggers_escalate() -> None:
    """Aggregate score <86 + red findings → escalate per Class A pattern.

    Memory ``feedback_both_ai_review_fail_means_stop``: when the reviewers
    don't agree (score below the 86 confidence cutoff), the agent's
    substantive disagreement is real — stop, ask a human, don't iterate.
    """
    verdict = _verdict_red_low_score()
    assert verdict.score < SCORE_CONFIDENCE_THRESHOLD
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(verdict,),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.ESCALATE_LOW_CONFIDENCE


def test_mixed_clusters_one_low_score_triggers_escalate() -> None:
    """If ANY cluster's verdict is low-score-with-red, escalate. The low signal
    dominates — we can't trust the cross-cluster picture if either side disagrees
    with itself."""
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(_verdict_red_high_score(), _verdict_red_low_score()),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.ESCALATE_LOW_CONFIDENCE


def test_no_verdicts_is_noop() -> None:
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.NOOP


def test_already_handled_verdict_is_noop() -> None:
    """Same verdict twice → no double-iterate."""
    verdict = _verdict_red_high_score()
    key = verdict_idempotency_key(verdict)
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(verdict,),
        already_handled_keys=frozenset({key}),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.NOOP


def test_new_verdict_supersedes_evaluates_again() -> None:
    """A re-posted verdict with different findings (or different score)
    must produce a different idempotency key — the watcher re-evaluates."""
    original = _verdict_red_high_score()
    superseded = parse_ai_review_comment(RED_HIGH_SCORE_SUPERSEDED_BODY)
    assert superseded is not None
    original_key = verdict_idempotency_key(original)
    superseded_key = verdict_idempotency_key(superseded)
    assert original_key != superseded_key
    # The orchestrator has already handled the ORIGINAL — but the SUPERSEDED
    # one's key is fresh, so the decision is RESPAWN, not NOOP.
    ctx = AIReviewIterationContext(
        repo='mikelear/leartech-auth-service',
        pr_number=58,
        verdicts=(superseded,),
        already_handled_keys=frozenset({original_key}),
    )
    assert decide_ai_review_action(ctx) == AIReviewDecision.RESPAWN


# ─── Idempotency key tests ───────────────────────────────────────────────────


def test_idempotency_key_is_stable_across_parses() -> None:
    """Same body parsed twice → same key. Determinism is the whole point."""
    a = parse_ai_review_comment(RED_HIGH_SCORE_BODY)
    b = parse_ai_review_comment(RED_HIGH_SCORE_BODY)
    assert a is not None and b is not None
    assert verdict_idempotency_key(a) == verdict_idempotency_key(b)


def test_idempotency_key_differs_per_cluster() -> None:
    """Same findings on different clusters → distinct keys.

    Each cluster's verdict is acted on independently — if AZ says one thing
    and GCP says the same thing, the orchestrator may want to act on both
    (or neither) depending on which cluster's keys are already in the
    handled set.
    """
    gcp_verdict = AIReviewVerdict(
        cluster='gcp',
        emoji='warning',
        score=95,
        verdict='Good',
        auto_merge_eligible=False,
        findings=(AIReviewFinding(severity='red', reviewer='claude', location='a.go:1', fix_hint='x'),),
    )
    az_verdict = AIReviewVerdict(
        cluster='az',
        emoji='warning',
        score=95,
        verdict='Good',
        auto_merge_eligible=False,
        findings=(AIReviewFinding(severity='red', reviewer='claude', location='a.go:1', fix_hint='x'),),
    )
    assert verdict_idempotency_key(gcp_verdict) != verdict_idempotency_key(az_verdict)


def test_idempotency_key_differs_per_score() -> None:
    """Same findings, different score → distinct keys (verdict superseded)."""
    base_findings = (AIReviewFinding(severity='red', reviewer='claude', location='a.go:1', fix_hint='x'),)
    a = AIReviewVerdict(
        cluster='gcp', emoji='warning', score=88, verdict='Good', auto_merge_eligible=False, findings=base_findings
    )
    b = AIReviewVerdict(
        cluster='gcp', emoji='warning', score=95, verdict='Good', auto_merge_eligible=False, findings=base_findings
    )
    assert verdict_idempotency_key(a) != verdict_idempotency_key(b)


def test_idempotency_key_invariant_of_finding_order() -> None:
    """The reviewer panel may post findings in different orders run-to-run;
    the key MUST treat ``[A, B]`` and ``[B, A]`` as the same observation."""
    a = AIReviewFinding(severity='red', reviewer='claude', location='a.go:1', fix_hint='x')
    b = AIReviewFinding(severity='red', reviewer='deepseek', location='b.go:2', fix_hint='y')
    v_ab = AIReviewVerdict(
        cluster='gcp', emoji='warning', score=90, verdict='Good', auto_merge_eligible=False, findings=(a, b)
    )
    v_ba = AIReviewVerdict(
        cluster='gcp', emoji='warning', score=90, verdict='Good', auto_merge_eligible=False, findings=(b, a)
    )
    assert verdict_idempotency_key(v_ab) == verdict_idempotency_key(v_ba)


# ─── Payload construction tests ──────────────────────────────────────────────


def test_build_ai_review_failure_payload_shape() -> None:
    verdict = _verdict_red_high_score()
    payload = build_ai_review_failure_payload(verdict)
    assert payload['kind'] == 'ai_review_failure'
    assert payload['cluster'] == 'gcp'
    assert payload['score'] == 95
    assert payload['verdict'] == 'Good'
    assert len(payload['red_findings']) == 1
    assert payload['red_findings'][0]['location'] == 'cmd/server/main.go:67'
    # all_findings carries yellow + blue as well so the agent has context.
    assert len(payload['all_findings']) == 2


def test_build_ai_review_failure_payloads_skips_no_red_verdicts() -> None:
    """No-red verdicts should not produce payloads — the iteration loop is
    keyed on actionable red signal."""
    payloads = build_ai_review_failure_payloads(
        [_verdict_all_green(), _verdict_yellow_only(), _verdict_red_high_score()],
    )
    assert len(payloads) == 1
    assert payloads[0]['cluster'] == 'gcp'


def test_build_feedback_payloads_includes_ai_review_failures() -> None:
    """The watcher constructs feedback_payloads via the existing
    build_feedback_payloads — verify the new ``ai_review_failures`` kwarg
    routes into the output list with kind='ai_review_failure'."""
    verdict = _verdict_red_high_score()
    failure_payload = build_ai_review_failure_payload(verdict)
    out = build_feedback_payloads([], ai_review_failures=[failure_payload])
    assert len(out) == 1
    assert out[0]['kind'] == 'ai_review_failure'


def test_build_feedback_payloads_orders_end2end_then_ai_review_failures() -> None:
    """end2end first (most recent), ai_review_failure next, ai_review_finding last.

    Pure human-readability ordering; the agent reads all blocks unconditionally,
    but a reviewer scanning the prompt should see substantive failures first."""
    from gate.tools.end2end_gate import End2EndFailure, End2EndTest

    end2end = End2EndFailure(
        gate='az/end2end',
        classification='real_failure',
        summary='2/3 checks passed',
        failed_tests=(End2EndTest(name='03-list', status='fail', message='HTTP 500'),),
        actionable=True,
    )
    failure_payload = build_ai_review_failure_payload(_verdict_red_high_score())
    out = build_feedback_payloads(
        [end2end],
        ai_review_failures=[failure_payload],
        ai_review_findings=[{'cluster': 'gcp', 'verdict': 'Needs Work', 'score': 65}],
    )
    assert out[0]['kind'] == 'end2end_failure'
    assert out[1]['kind'] == 'ai_review_failure'
    assert out[2]['kind'] == 'ai_review_finding'


# ─── Prompt-block rendering tests ────────────────────────────────────────────


def test_format_ai_review_failure_payload_surfaces_red_findings() -> None:
    payload = build_ai_review_failure_payload(_verdict_red_high_score())
    lines = format_ai_review_failure_payload(payload)
    block = '\n'.join(lines)
    assert 'AI code review (gcp)' in block
    assert 'Good (95/100)' in block
    assert 'cmd/server/main.go:67' in block
    assert 'Missing error handling' in block
    # Red header is mandatory because the agent MUST address those.
    assert 'Red findings' in block


def test_format_feedback_payloads_renders_ai_review_failure_block() -> None:
    """Integration: ``ai_review_failure`` payloads flow through
    ``format_feedback_payloads_for_prompt`` and surface in the prompt block."""
    payload = build_ai_review_failure_payload(_verdict_red_high_score())
    block = format_feedback_payloads_for_prompt([payload])
    assert 'Previous-attempt failure context' in block
    assert 'AI code review (gcp)' in block
    assert 'cmd/server/main.go:67' in block
    assert 'Red findings' in block


def test_format_payload_omits_red_section_when_no_red() -> None:
    """A payload with no red findings (defensive — shouldn't be produced by
    the builder, but the renderer must still cope) renders the header and
    summary without the Red findings sub-section."""
    payload = {
        'kind': 'ai_review_failure',
        'cluster': 'az',
        'verdict': 'Good',
        'score': 95,
        'emoji': 'warning',
        'summary': 'No red findings present (defensive case).',
        'red_findings': [],
        'all_findings': [],
    }
    lines = format_ai_review_failure_payload(payload)
    block = '\n'.join(lines)
    assert 'AI code review (az)' in block
    assert 'Red findings' not in block
