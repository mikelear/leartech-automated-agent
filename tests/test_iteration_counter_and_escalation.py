"""Tests for the v6p0.6 step-3 per-gate iteration counter + same-error escalation.

Covers the six scenarios called out in the initiative goal:

1. Single gate fails twice with the same fingerprint → escalate +
   manual-fix label applied.
2. Single gate fails twice with different fingerprints → continue
   iterating (agent IS making progress).
3. Five gates each fail once → continues (under cross-gate cap).
4. Five+ total iterations → cross-gate escalate.
5. Green transition → counter resets.
6. Manual override via PR comment ``/retry-all`` → counter resets.

Plus structural tests:

- Fingerprint stability across calls and dedup of trace_url / screenshot_url
  noise (UI failures with run-specific URLs must produce stable fps).
- Fingerprint differs for end2end_failure vs gate_failure shapes.
- Env-var overrides of the per-gate threshold + cross-gate cap.
- ``AttemptHistory`` round-trips through ``to_dict`` / ``from_dict`` for
  catalog-DB persistence.
- ``escalation_comment_body`` renders the cited gate + history summary
  for both escalation reasons.

The module under test is pure (no GitHub / K8s / DB) — tests drive it
directly, no fixtures or mocks required.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gate.watcher.escalation import (
    DEFAULT_CROSS_GATE_CAP,
    DEFAULT_SAME_ERROR_THRESHOLD,
    ENV_CROSS_GATE_CAP,
    ENV_SAME_ERROR_THRESHOLD,
    MANUAL_RETRY_COMMAND,
    AttemptHistory,
    EscalationReason,
    compute_fingerprint,
    cross_gate_cap,
    escalation_comment_body,
    escalation_label,
    is_manual_retry_command,
    mark_gate_passed,
    record_attempt,
    reset_all,
    same_error_threshold,
    should_escalate,
)

# ─── Fixtures ───────────────────────────────────────────────────────────────


def _end2end_payload(
    *,
    gate: str = 'az/end2end',
    classification: str = 'real_failure',
    test_name: str = '03-list',
    message: str = "GET /api/items expected 200, got 500: {'detail': 'internal error'}",
) -> dict[str, Any]:
    """An end2end_failure payload in the v6p0.5 shape."""
    return {
        'kind': 'end2end_failure',
        'gate': gate,
        'classification': classification,
        'summary': '2/3 checks passed',
        'failed_tests': [
            {'name': test_name, 'message': message, 'trace_url': None, 'screenshot_url': None},
        ],
        'actionable': True,
    }


def _gate_failure_payload(
    *,
    gate: str = 'az/security-scan',
    rule: str = 'CVE-2024-9999',
    severity: str = 'critical',
    location: str = 'lib/x:1',
    message: str = 'demo CVE',
) -> dict[str, Any]:
    """A gate_failure payload in the v6p0.6 step-1 shape."""
    return {
        'kind': 'gate_failure',
        'gate': gate,
        'artefact_type': 'sarif',
        'findings': [
            {'severity': severity, 'rule': rule, 'location': location, 'message': message},
        ],
        'actionable': True,
        'top_severity': severity,
    }


# ─── Fingerprint stability + shape coverage ─────────────────────────────────


def test_fingerprint_is_stable_across_calls() -> None:
    payload = _end2end_payload()
    assert compute_fingerprint(payload) == compute_fingerprint(payload)


def test_fingerprint_ignores_trace_and_screenshot_urls() -> None:
    """UI failures carry per-run artifact URLs (signed S3 links etc.).
    Two cycles producing the same logical failure must produce the same
    fingerprint regardless of those URLs."""
    a = _end2end_payload()
    a['failed_tests'][0]['screenshot_url'] = 'https://art.example/run-1/login.png'
    a['failed_tests'][0]['trace_url'] = 'https://art.example/run-1/trace.zip'
    b = _end2end_payload()
    b['failed_tests'][0]['screenshot_url'] = 'https://art.example/run-2/login.png'
    b['failed_tests'][0]['trace_url'] = 'https://art.example/run-2/trace.zip'
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_fingerprint_differs_for_different_test_messages() -> None:
    a = _end2end_payload(message='HTTP 500 internal')
    b = _end2end_payload(message='HTTP 502 bad gateway')
    assert compute_fingerprint(a) != compute_fingerprint(b)


def test_fingerprint_classification_changes_fp() -> None:
    """real_failure with the same test list ≠ preview_infra fingerprint."""
    real = _end2end_payload(classification='real_failure')
    infra = _end2end_payload(classification='preview_infra')
    assert compute_fingerprint(real) != compute_fingerprint(infra)


def test_fingerprint_gate_failure_shape() -> None:
    a = _gate_failure_payload()
    b = _gate_failure_payload()  # identical
    assert compute_fingerprint(a) == compute_fingerprint(b)
    c = _gate_failure_payload(rule='CVE-2024-0001')
    assert compute_fingerprint(a) != compute_fingerprint(c)


def test_fingerprint_gate_failure_findings_order_invariant() -> None:
    """Same finding set in different order produces the same fingerprint."""
    a = _gate_failure_payload()
    a['findings'].append({'severity': 'high', 'rule': 'CVE-2', 'location': 'a:1', 'message': 'x'})
    b = _gate_failure_payload()
    # Insert the same extra finding at the start instead of the end.
    b['findings'].insert(0, {'severity': 'high', 'rule': 'CVE-2', 'location': 'a:1', 'message': 'x'})
    assert compute_fingerprint(a) == compute_fingerprint(b)


def test_fingerprint_empty_findings_falls_back_to_raw_log_tail() -> None:
    """A gate_failure with no structured findings hashes its raw_log_tail."""
    a: dict[str, Any] = {
        'kind': 'gate_failure',
        'gate': 'az/lint',
        'artefact_type': 'sarif',
        'findings': [],
        'raw_log_tail': 'error: undefined symbol\n  at line 12\nE  RuntimeError\n',
    }
    b = dict(a)
    b['raw_log_tail'] = a['raw_log_tail']
    assert compute_fingerprint(a) == compute_fingerprint(b)
    c = dict(a)
    c['raw_log_tail'] = 'completely different error message'
    assert compute_fingerprint(a) != compute_fingerprint(c)


def test_fingerprint_string_input_treated_as_log_tail() -> None:
    """Passing a raw log string (no dict envelope) hashes the first lines."""
    a = 'error: thing broke\n  at x\n  at y\n'
    b = 'error: thing broke\n  at x\n  at y\n'
    assert compute_fingerprint(a) == compute_fingerprint(b)
    c = 'different error entirely\n  at z\n'
    assert compute_fingerprint(a) != compute_fingerprint(c)


def test_fingerprint_none_or_empty_does_not_crash() -> None:
    """Defensive: an empty payload still gets a deterministic fingerprint."""
    assert compute_fingerprint(None) == compute_fingerprint(None)
    assert compute_fingerprint({}) == compute_fingerprint({})
    assert compute_fingerprint('') == compute_fingerprint('')


# ─── record_attempt / mark_gate_passed / reset_all ──────────────────────────


def test_record_attempt_appends_to_per_gate_list() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='abc')
    record_attempt(h, gate='az/end2end', fingerprint='def')
    assert h.gate_attempts['az/end2end'] == ['abc', 'def']


def test_record_attempt_does_not_dedup_same_fingerprint() -> None:
    """Caller is responsible for once-per-cycle; module records what it's told."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='abc')
    record_attempt(h, gate='az/end2end', fingerprint='abc')
    assert h.gate_attempts['az/end2end'] == ['abc', 'abc']


def test_mark_gate_passed_clears_only_one_gate() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='abc')
    record_attempt(h, gate='gcp/lint', fingerprint='xyz')
    mark_gate_passed(h, gate='az/end2end')
    assert 'az/end2end' not in h.gate_attempts
    assert h.gate_attempts['gcp/lint'] == ['xyz']


def test_mark_gate_passed_is_idempotent() -> None:
    """No-op when the gate wasn't in the history."""
    h = AttemptHistory()
    mark_gate_passed(h, gate='az/end2end')  # must not raise
    assert h.gate_attempts == {}


def test_reset_all_clears_every_gate() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='a', fingerprint='1')
    record_attempt(h, gate='b', fingerprint='2')
    reset_all(h)
    assert h.gate_attempts == {}


# ─── should_escalate: the canonical six scenarios ───────────────────────────


def test_scenario_1_same_fingerprint_twice_escalates() -> None:
    """Initiative goal scenario 1: single gate fails twice with same
    fingerprint → escalate."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='same-fp')
    # First attempt — no escalation yet.
    assert should_escalate(h, gate='az/end2end') is None
    record_attempt(h, gate='az/end2end', fingerprint='same-fp')
    assert should_escalate(h, gate='az/end2end') == EscalationReason.SAME_ERROR_REPEATED


def test_scenario_2_different_fingerprints_keeps_iterating() -> None:
    """Initiative goal scenario 2: single gate fails twice with DIFFERENT
    fingerprints → continue (agent IS making progress)."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp-a')
    record_attempt(h, gate='az/end2end', fingerprint='fp-b')
    assert should_escalate(h, gate='az/end2end') is None


def test_scenario_3_five_gates_each_failing_once_continues() -> None:
    """Initiative goal scenario 3: under the cross-gate cap, multiple
    gates each failing once must NOT escalate."""
    h = AttemptHistory()
    for i, gate in enumerate(['az/end2end', 'gcp/end2end', 'az/lint', 'gcp/lint']):
        record_attempt(h, gate=gate, fingerprint=f'fp-{i}')
    # 4 attempts < default cap of 5 — keep iterating.
    assert h.total_attempts == 4
    for gate in h.gate_attempts:
        assert should_escalate(h, gate=gate) is None


def test_scenario_4_cross_gate_cap_escalates() -> None:
    """Initiative goal scenario 4: 5+ total iterations summed across gates
    escalates cross-gate even when each individual gate fingerprint differs."""
    h = AttemptHistory()
    for i in range(DEFAULT_CROSS_GATE_CAP):
        # Different gate each time so same-error doesn't fire first.
        record_attempt(h, gate=f'gate-{i}', fingerprint=f'fp-{i}')
    assert h.total_attempts == DEFAULT_CROSS_GATE_CAP
    # Probing any of the cited gates returns the cross-gate verdict —
    # we don't have a same-error trigger because every fp is unique.
    last_gate = f'gate-{DEFAULT_CROSS_GATE_CAP - 1}'
    assert should_escalate(h, gate=last_gate) == EscalationReason.CROSS_GATE_BUDGET_EXHAUSTED


def test_scenario_5_green_transition_resets_gate() -> None:
    """Initiative goal scenario 5: when a gate flips fail→pass, its
    counter clears so a subsequent fail starts fresh."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp-same')
    record_attempt(h, gate='az/end2end', fingerprint='fp-same')
    # Would escalate now.
    assert should_escalate(h, gate='az/end2end') == EscalationReason.SAME_ERROR_REPEATED
    # Gate went green → counter clears.
    mark_gate_passed(h, gate='az/end2end')
    # Same failure shape comes back — but it's the first since reset, so
    # no escalation.
    record_attempt(h, gate='az/end2end', fingerprint='fp-same')
    assert should_escalate(h, gate='az/end2end') is None


def test_scenario_6_manual_override_clears_counters() -> None:
    """Initiative goal scenario 6: /retry-all PR comment clears all counters."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp')
    record_attempt(h, gate='az/end2end', fingerprint='fp')
    record_attempt(h, gate='gcp/lint', fingerprint='other')
    assert is_manual_retry_command(MANUAL_RETRY_COMMAND)
    assert is_manual_retry_command('/retry-all')
    # Case + whitespace insensitive.
    assert is_manual_retry_command('  /Retry-All  ')
    # Multi-line comment with the command on its own line.
    assert is_manual_retry_command('Fixed the cluster issue.\n/retry-all\nthanks!')
    # Resetting clears every gate.
    reset_all(h)
    assert h.gate_attempts == {}
    assert h.total_attempts == 0


def test_manual_retry_command_negatives() -> None:
    """The detector must NOT fire on unrelated comments."""
    assert not is_manual_retry_command(None)
    assert not is_manual_retry_command('')
    assert not is_manual_retry_command('Looks good to me, /lgtm')
    assert not is_manual_retry_command('please /retest')
    # Inline (not on its own line) should NOT fire — operators put the
    # command on its own line by convention; in-prose mentions are noise.
    assert not is_manual_retry_command('I think /retry-all might help')


# ─── Escalation precedence + cross-gate interaction ─────────────────────────


def test_same_error_takes_precedence_over_cross_gate() -> None:
    """When BOTH conditions trip simultaneously, same-error wins — it's
    the more actionable signal (cite the specific recurring fingerprint)."""
    h = AttemptHistory()
    # Build up to the cross-gate cap...
    for i in range(DEFAULT_CROSS_GATE_CAP - 1):
        record_attempt(h, gate=f'gate-{i}', fingerprint=f'fp-{i}')
    # Now a gate fails twice with the same fingerprint, hitting BOTH
    # thresholds at once.
    record_attempt(h, gate='az/end2end', fingerprint='same')
    record_attempt(h, gate='az/end2end', fingerprint='same')
    assert h.total_attempts >= DEFAULT_CROSS_GATE_CAP
    assert should_escalate(h, gate='az/end2end') == EscalationReason.SAME_ERROR_REPEATED


def test_same_error_threshold_override_via_argument() -> None:
    """Caller-supplied threshold raises the bar — 3 same fps required."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='same')
    record_attempt(h, gate='az/end2end', fingerprint='same')
    # Default threshold (2) would escalate — caller raises to 3.
    assert should_escalate(h, gate='az/end2end', threshold=3) is None
    record_attempt(h, gate='az/end2end', fingerprint='same')
    assert should_escalate(h, gate='az/end2end', threshold=3) == EscalationReason.SAME_ERROR_REPEATED


def test_cross_gate_cap_override_via_argument() -> None:
    h = AttemptHistory()
    for i in range(3):
        record_attempt(h, gate=f'g{i}', fingerprint=f'fp{i}')
    # Lower cap to 3 → already at the ceiling.
    assert should_escalate(h, gate='g0', cap=3) == EscalationReason.CROSS_GATE_BUDGET_EXHAUSTED
    # Generous cap → keep going.
    assert should_escalate(h, gate='g0', cap=100) is None


def test_same_error_requires_consecutive_identical_fps() -> None:
    """Three attempts with fp-A, fp-B, fp-A should NOT escalate — they're
    not consecutive (the agent changed something, then the bug came back,
    but not because the same fix failed twice)."""
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp-A')
    record_attempt(h, gate='az/end2end', fingerprint='fp-B')
    record_attempt(h, gate='az/end2end', fingerprint='fp-A')
    assert should_escalate(h, gate='az/end2end') is None


# ─── Env var overrides ──────────────────────────────────────────────────────


def test_env_var_overrides_same_error_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SAME_ERROR_THRESHOLD, '4')
    assert same_error_threshold() == 4


def test_env_var_overrides_cross_gate_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_CROSS_GATE_CAP, '10')
    assert cross_gate_cap() == 10


def test_env_var_invalid_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SAME_ERROR_THRESHOLD, 'not-an-int')
    assert same_error_threshold() == DEFAULT_SAME_ERROR_THRESHOLD


def test_env_var_zero_or_negative_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 and negative values are nonsensical — fall back to defaults so an
    operator typo doesn't silently disable escalation."""
    monkeypatch.setenv(ENV_SAME_ERROR_THRESHOLD, '0')
    assert same_error_threshold() == DEFAULT_SAME_ERROR_THRESHOLD
    monkeypatch.setenv(ENV_CROSS_GATE_CAP, '-1')
    assert cross_gate_cap() == DEFAULT_CROSS_GATE_CAP


def test_env_var_raised_threshold_changes_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raised threshold is honoured by should_escalate() without an
    explicit kwarg — proves the env var flows through."""
    monkeypatch.setenv(ENV_SAME_ERROR_THRESHOLD, '3')
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='same')
    record_attempt(h, gate='az/end2end', fingerprint='same')
    assert should_escalate(h, gate='az/end2end') is None
    record_attempt(h, gate='az/end2end', fingerprint='same')
    assert should_escalate(h, gate='az/end2end') == EscalationReason.SAME_ERROR_REPEATED


# ─── Persistence round-trip ─────────────────────────────────────────────────


def test_attempt_history_round_trips_via_to_dict() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp1')
    record_attempt(h, gate='az/end2end', fingerprint='fp2')
    record_attempt(h, gate='gcp/lint', fingerprint='fp3')
    serialised = json.dumps(h.to_dict())
    restored = AttemptHistory.from_dict(json.loads(serialised))
    assert restored.gate_attempts == h.gate_attempts


def test_attempt_history_from_dict_handles_none() -> None:
    """First-event hydration: there's no prior state, from_dict(None)
    returns an empty history rather than raising."""
    h = AttemptHistory.from_dict(None)
    assert h.gate_attempts == {}


def test_attempt_history_from_dict_tolerates_bad_shapes() -> None:
    """Defensive: a corrupt store row mustn't crash the watcher."""
    h = AttemptHistory.from_dict({'gate_attempts': 'not-a-dict'})
    assert h.gate_attempts == {}
    h2 = AttemptHistory.from_dict({'gate_attempts': {'az/end2end': 'not-a-list'}})
    assert 'az/end2end' not in h2.gate_attempts


# ─── total_attempts property ────────────────────────────────────────────────


def test_total_attempts_sums_across_gates() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='a', fingerprint='1')
    record_attempt(h, gate='a', fingerprint='2')
    record_attempt(h, gate='b', fingerprint='3')
    assert h.total_attempts == 3


# ─── Comment + label rendering ──────────────────────────────────────────────


def test_escalation_label_matches_iteration_loop_label() -> None:
    """The label must be uniform across every escalation path so
    Lighthouse Keeper's auto-merge hold is consistent."""
    from gate.watcher.iteration_loop import manual_fix_label

    assert escalation_label() == manual_fix_label()
    assert escalation_label() == 'do-not-merge/manual-fix'


def test_escalation_comment_same_error_cites_gate_and_fingerprint() -> None:
    h = AttemptHistory()
    record_attempt(h, gate='az/end2end', fingerprint='fp-abc')
    record_attempt(h, gate='az/end2end', fingerprint='fp-abc')
    body = escalation_comment_body(
        EscalationReason.SAME_ERROR_REPEATED,
        h,
        gate='az/end2end',
    )
    assert 'az/end2end' in body
    assert 'fp-abc' in body
    assert escalation_label() in body
    assert MANUAL_RETRY_COMMAND in body


def test_escalation_comment_cross_gate_includes_total() -> None:
    h = AttemptHistory()
    for i in range(DEFAULT_CROSS_GATE_CAP):
        record_attempt(h, gate=f'g{i}', fingerprint=f'fp{i}')
    body = escalation_comment_body(
        EscalationReason.CROSS_GATE_BUDGET_EXHAUSTED,
        h,
    )
    assert str(DEFAULT_CROSS_GATE_CAP) in body
    assert escalation_label() in body
    # Each gate cited at least once.
    for i in range(DEFAULT_CROSS_GATE_CAP):
        assert f'g{i}' in body


def test_escalation_comment_handles_empty_history() -> None:
    """Defensive: if escalation_comment_body is called with no history
    rows (shouldn't happen but isn't a crash-worthy invariant), render
    something useful instead of crashing."""
    body = escalation_comment_body(
        EscalationReason.SAME_ERROR_REPEATED,
        AttemptHistory(),
        gate='az/end2end',
    )
    assert 'no recorded attempts' in body.lower()


# ─── Walk-through (the "fake same end2end failure twice" example) ───────────


def test_walkthrough_same_end2end_failure_twice() -> None:
    """End-to-end narrative: an end2end gate fails twice with the
    canonical PR #58-shape failure. Cycle 1 records the attempt and
    does NOT escalate (one observation isn't a pattern). Cycle 2
    records the IDENTICAL attempt and trips same-error escalation.

    This is the worked example the PR description references.
    """
    history = AttemptHistory()

    # Cycle 1 — agent's first attempt. Watcher fetches end2end output,
    # builds the payload, computes its fingerprint, records it.
    failure_payload_cycle_1 = _end2end_payload(
        gate='az/end2end',
        test_name='03-list',
        message='GET /api/items expected 200, got 500',
    )
    fp_1 = compute_fingerprint(failure_payload_cycle_1)
    record_attempt(history, gate='az/end2end', fingerprint=fp_1)
    # First observation — keep iterating, spawn the agent again.
    assert should_escalate(history, gate='az/end2end') is None

    # Cycle 2 — agent's second attempt fails THE SAME WAY (HTTP 500 on
    # /api/items). Different run, different pipelinerun_name, but the
    # failed-test name + message are identical → same fingerprint.
    failure_payload_cycle_2 = _end2end_payload(
        gate='az/end2end',
        test_name='03-list',
        message='GET /api/items expected 200, got 500',
    )
    fp_2 = compute_fingerprint(failure_payload_cycle_2)
    assert fp_1 == fp_2  # stable across cycles
    record_attempt(history, gate='az/end2end', fingerprint=fp_2)

    # Now we escalate — the agent's last fix didn't move the needle.
    reason = should_escalate(history, gate='az/end2end')
    assert reason == EscalationReason.SAME_ERROR_REPEATED

    # The orchestrator now applies the label and posts the comment.
    body = escalation_comment_body(reason, history, gate='az/end2end')
    assert 'az/end2end' in body
    assert fp_1 in body
    assert escalation_label() in body
