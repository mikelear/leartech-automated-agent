"""Unit tests for gate.tools.triggers — pure parsers + diff logic."""

from __future__ import annotations

import textwrap

from gate.tools.triggers import (
    Trigger,
    diff_triggers,
    golden_template_for,
    parse_triggers_yaml,
)

# Realistic triggers.yaml shape from leartech-auth-ui.
SAMPLE_TRIGGERS = textwrap.dedent(
    """
    apiVersion: config.lighthouse.jenkins-x.io/v1alpha1
    kind: TriggerConfig
    spec:
      presubmits:
      - name: pr
        context: "pr"
        always_run: true
        optional: false
        source: "pullrequest.yaml"
      - name: lint
        context: "lint"
        always_run: true
        optional: true
        source: "lint.yaml"
      - name: ai-code-review
        context: "ai-review"
        always_run: true
        optional: true
        source: "ai-review/pullrequest.yaml"
      - name: ai-review-feedback
        context: "ai-feedback"
        always_run: false
        optional: true
        trigger: "(?m)^/ai-feedback"
        source: "ai-review/feedback.yaml"
    """
)


def test_parses_all_presubmit_entries() -> None:
    triggers = parse_triggers_yaml(SAMPLE_TRIGGERS)
    assert len(triggers) == 4
    contexts = {t.context for t in triggers}
    assert contexts == {'pr', 'lint', 'ai-review', 'ai-feedback'}


def test_always_run_flag_correctly_parsed() -> None:
    triggers = parse_triggers_yaml(SAMPLE_TRIGGERS)
    by_context = {t.context: t for t in triggers}
    assert by_context['pr'].always_run is True
    assert by_context['ai-review'].always_run is True
    assert by_context['ai-feedback'].always_run is False  # opt-in via /ai-feedback comment


def test_skips_entries_missing_required_fields() -> None:
    """Partial parsing — drop incomplete entries rather than raising."""
    bad = textwrap.dedent(
        """
        apiVersion: config.lighthouse.jenkins-x.io/v1alpha1
        kind: TriggerConfig
        spec:
          presubmits:
          - name: incomplete
          - name: also-incomplete
            context: ""
          - name: good
            context: "good"
        """
    )
    triggers = parse_triggers_yaml(bad)
    assert len(triggers) == 1
    assert triggers[0].context == 'good'


def test_handles_empty_yaml() -> None:
    assert parse_triggers_yaml('') == []
    assert parse_triggers_yaml('---\n') == []


def test_diff_flags_missing_required_from_consumer() -> None:
    consumer = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
        Trigger(name='lint', context='lint', always_run=True, optional=True, source='x'),
    ]
    golden = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
        Trigger(name='lint', context='lint', always_run=True, optional=True, source='x'),
        Trigger(name='ai-code-review', context='ai-review', always_run=True, optional=True, source='x'),
        Trigger(name='security-scan', context='security-scan', always_run=True, optional=True, source='x'),
    ]
    missing, extra = diff_triggers(consumer, golden)
    assert missing == ['ai-review', 'security-scan']
    assert extra == []


def test_diff_ignores_opt_in_golden_triggers() -> None:
    """Triggers that golden ships as `always_run: false` are not required of consumers."""
    consumer = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
    ]
    golden = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
        Trigger(name='ai-feedback', context='ai-feedback', always_run=False, optional=True, source='x'),
    ]
    missing, extra = diff_triggers(consumer, golden)
    # ai-feedback is opt-in (always_run: false) — consumer absence is OK.
    assert missing == []


def test_diff_surfaces_extra_in_consumer() -> None:
    """Consumer-specific triggers (not in golden) surface as `extra` — informational, not a failure."""
    consumer = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
        Trigger(name='custom-check', context='custom-check', always_run=True, optional=True, source='x'),
    ]
    golden = [
        Trigger(name='pr', context='pr', always_run=True, optional=False, source='x'),
    ]
    missing, extra = diff_triggers(consumer, golden)
    assert missing == []
    assert extra == ['custom-check']


def test_golden_template_lookup() -> None:
    assert golden_template_for('leartech-auth-ui') == 'mikelear/leartech-angular-service-template'
    assert golden_template_for('mikelear/leartech-auth-service') == 'mikelear/leartech-go-service-template'
    assert golden_template_for('totally-unknown-repo') is None
