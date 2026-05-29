"""Tests for `gate.agent.step_failure_diagnosis`.

Phase G.2. Two test families:

1. **Classification matrix** — for each canonical failure shape, a
   representative log fragment from leartech-llm-training-data is fed
   through `classify_step_failure` and the classification + action are
   asserted. Includes edge cases (empty log, unknown step name, OOM
   override of step-specific patterns).

2. **Dispatch precedence** — `summarise_dispatch` returns the action
   the initiative loop should take for a *set* of failures. The
   precedence is fix_code > fix_test > all-rebase > all-retry > otherwise
   escalate; the tests exercise each transition.
"""

from __future__ import annotations

import pytest

from gate.agent.step_failure_diagnosis import (
    ACTION_ESCALATE,
    ACTION_FIX_CODE,
    ACTION_FIX_TEST,
    ACTION_REBASE,
    ACTION_RETRY,
    CLASSIFICATIONS,
    StepFailure,
    classify_step_failure,
    diagnose_failures,
    failures_to_json,
    summarise_dispatch,
)

# ─── Classification matrix ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ('step_name', 'log_tail', 'expected_classification', 'expected_action'),
    [
        # ── git merge conflict (rebase) ──
        (
            'git-clone',
            'Auto-merging app/main.py\n'
            'CONFLICT (content): Merge conflict in app/main.py\n'
            'Automatic merge failed; fix conflicts and run "git commit"',
            'git_merge_conflict',
            ACTION_REBASE,
        ),
        # ── ruff format error (fix_code) ──
        (
            'ruff-format',
            'Would reformat: gate/agent/foo.py\n1 file would be reformatted, 42 files left unchanged',
            'ruff_format_error',
            ACTION_FIX_CODE,
        ),
        # ── ruff lint error (fix_code) ──
        (
            'ruff',
            'ruff check\n'
            'gate/agent/foo.py:12:5: E501 line too long (130 > 120)\n'
            'gate/agent/bar.py:99:1: F401 unused import\n'
            'Found 2 errors.',
            'ruff_lint_error',
            ACTION_FIX_CODE,
        ),
        # ── mypy type error (fix_code) ──
        (
            'mypy',
            'gate/agent/foo.py:14: error: Incompatible types in assignment '
            '(expression has type "int", variable has type "str")  [assignment]\n'
            'Found 1 error in 1 file (checked 12 source files)',
            'mypy_type_error',
            ACTION_FIX_CODE,
        ),
        # ── pytest test failure (fix_test) ──
        (
            'pytest',
            '= FAILURES =\n'
            '_______ test_classify_step_failure_handles_unknown _______\n'
            'AssertionError: assert "unknown" == "ruff_lint_error"\n'
            '= short test summary info =\n'
            'FAILED tests/test_step_failure_diagnosis.py::test_classify_step_failure_handles_unknown',
            'pytest_test_failure',
            ACTION_FIX_TEST,
        ),
        # ── kaniko build failure (escalate) ──
        (
            'build-image',
            'INFO: Building image: leartech/automated-agent:abc\n'
            'error building image: executor failed running [/bin/sh -c uv sync]: exit code: 1\n'
            'COPY failed: stat: no such file or directory',
            'kaniko_build_failure',
            ACTION_ESCALATE,
        ),
        # ── image pull backoff (escalate) ──
        (
            'preview',
            'Warning  Failed  pod/preview-agent-0  Error: ImagePullBackOff\nFailed to pull image: manifest unknown',
            'image_pull_backoff',
            ACTION_ESCALATE,
        ),
        # ── ai-review red finding (fix_code) ──
        (
            'ai-review',
            'review_verdict: red\n'
            'BLOCKING: Auth helper is shelling out to bash instead of using gh client.\n'
            'MUST FIX before merge.',
            'ai_review_red_finding',
            ACTION_FIX_CODE,
        ),
        # ── tekton step OOM (escalate) — overrides step-specific shape ──
        (
            'pytest',
            'pytest collecting...\nKilled\nOOMKilled: memory limit exceeded\nexit code 137',
            'tekton_step_oom',
            ACTION_ESCALATE,
        ),
        # ── tekton step timeout (retry) ──
        (
            'pytest',
            'tests collected ok\nstep exceeded its timeout\nTaskRunTimeout: context deadline exceeded',
            'tekton_step_timeout',
            ACTION_RETRY,
        ),
        # ── preview deploy failure (escalate) ──
        (
            'helm-promote',
            'helm upgrade --install ...\nError: UPGRADE FAILED: release failed: no matches for kind "FlinkDeployment"',
            'preview_deploy_failure',
            ACTION_ESCALATE,
        ),
        # ── security scan finding (escalate) ──
        (
            'image-scan',
            'Total: 5 vulnerabilities found\nCRITICAL: 1\nHIGH:     2\nCVE-2025-12345 in openssl ',
            'security_scan_finding',
            ACTION_ESCALATE,
        ),
    ],
)
def test_classification_matrix(
    step_name: str,
    log_tail: str,
    expected_classification: str,
    expected_action: str,
) -> None:
    """Each canonical pattern routes to the expected classification + action."""
    failure = classify_step_failure(step_name, log_tail, pipelinerun='pr-7-run-abc')
    assert failure.classification == expected_classification, (
        f'step={step_name} matched {failure.classification!r}, want {expected_classification!r}'
    )
    assert failure.action == expected_action
    assert failure.pipelinerun == 'pr-7-run-abc'
    assert failure.step_name == step_name


def test_classification_map_is_complete() -> None:
    """Every classification key has a defined action (and vice versa, no orphan actions)."""
    valid_actions = {ACTION_REBASE, ACTION_FIX_CODE, ACTION_FIX_TEST, ACTION_RETRY, ACTION_ESCALATE}
    assert set(CLASSIFICATIONS.values()) <= valid_actions
    # `unknown` always escalates — the safety net.
    assert CLASSIFICATIONS['unknown'] == ACTION_ESCALATE


# ─── Edge cases ──────────────────────────────────────────────────────────────


def test_empty_log_returns_unknown_escalate() -> None:
    """Pod GC'd → empty log → unknown classification → escalate (no guessing)."""
    failure = classify_step_failure('ruff', '', pipelinerun='run-x')
    assert failure.classification == 'unknown'
    assert failure.action == ACTION_ESCALATE


def test_whitespace_only_log_returns_unknown() -> None:
    """A log that's only whitespace shouldn't accidentally match an empty substring check."""
    failure = classify_step_failure('ruff', '   \n\n\t\n  ', pipelinerun='run-x')
    assert failure.classification == 'unknown'


def test_unknown_step_name_can_still_classify_via_log() -> None:
    """OOM / timeout / image-pull are step-name-agnostic — must match regardless of step name."""
    failure = classify_step_failure('some-novel-step', 'pod was OOMKilled\nexit code 137')
    assert failure.classification == 'tekton_step_oom'
    assert failure.action == ACTION_ESCALATE


def test_unrecognised_failure_returns_unknown_escalate() -> None:
    """A failure that doesn't match any heuristic → unknown → escalate."""
    failure = classify_step_failure('mystery-step', 'something went sideways in a way we have never seen before')
    assert failure.classification == 'unknown'
    assert failure.action == ACTION_ESCALATE


def test_step_name_alone_is_not_sufficient() -> None:
    """A 'ruff' step with no diagnostic content in the log doesn't get classified as ruff failure."""
    failure = classify_step_failure('ruff', 'starting ruff... done.')
    # Should NOT spuriously match ruff_format/lint — both need diagnostic substrings.
    assert failure.classification == 'unknown'


def test_oom_overrides_step_specific_classification() -> None:
    """If a pytest step OOMs, OOM wins — fixing the test wouldn't fix the memory limit."""
    log = '= FAILURES =\nAssertionError\nOOMKilled\nexit code 137'
    failure = classify_step_failure('pytest', log)
    assert failure.classification == 'tekton_step_oom'  # OOM first in _HEURISTICS order


def test_image_pull_backoff_overrides_preview_deploy() -> None:
    """Image pull errors take precedence over preview-deploy classification."""
    log = 'helm upgrade --install ...\nErrImagePull\nmanifest unknown for sha:abc'
    failure = classify_step_failure('preview', log)
    assert failure.classification == 'image_pull_backoff'


# ─── Output shape ────────────────────────────────────────────────────────────


def test_step_failure_is_frozen() -> None:
    """Frozen dataclass — agent can serialise without aliasing concerns."""
    failure = classify_step_failure('ruff', 'gate/foo.py:1:1: E501 oops')
    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError is a dataclasses.FrozenInstanceError
        failure.classification = 'changed'  # type: ignore[misc]


def test_step_failure_to_dict_is_json_serialisable() -> None:
    import json

    failure = classify_step_failure('ruff', 'gate/foo.py:1:1: E501 oops', pipelinerun='run-x')
    payload = failure.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload['classification'] == 'ruff_lint_error'
    assert payload['action'] == ACTION_FIX_CODE
    assert payload['pipelinerun'] == 'run-x'


def test_failures_to_json_round_trips() -> None:
    import json

    a = classify_step_failure('ruff', 'gate/foo.py:1:1: E501 oops')
    b = classify_step_failure('git-clone', 'CONFLICT (content): Merge conflict in foo')
    text = failures_to_json([a, b])
    parsed = json.loads(text)
    assert isinstance(parsed, list)
    assert len(parsed) == 2
    assert parsed[0]['classification'] == 'ruff_lint_error'
    assert parsed[1]['classification'] == 'git_merge_conflict'


# ─── diagnose_failures ───────────────────────────────────────────────────────


def test_diagnose_failures_filters_to_failed_states_only() -> None:
    rows: list[dict[str, object]] = [
        {'step': 'git-clone', 'state': 'Succeeded', 'log_tail': '', 'pipelinerun': 'run-x'},
        {
            'step': 'ruff',
            'state': 'Failed',
            'log_tail': 'gate/foo.py:1:1: E501 oops',
            'pipelinerun': 'run-x',
        },
        {'step': 'pytest', 'state': 'Running', 'log_tail': '', 'pipelinerun': 'run-x'},
    ]
    diagnosed = diagnose_failures(rows)
    assert len(diagnosed) == 1
    assert diagnosed[0].step_name == 'ruff'
    assert diagnosed[0].classification == 'ruff_lint_error'


def test_diagnose_failures_handles_logs_under_either_key() -> None:
    """`log_tail` and `logs` are both accepted (different MCP shapes)."""
    rows: list[dict[str, object]] = [
        {'step': 'ruff', 'state': 'Failed', 'logs': 'gate/foo.py:1:1: E501 oops'},
    ]
    diagnosed = diagnose_failures(rows)
    assert len(diagnosed) == 1
    assert diagnosed[0].classification == 'ruff_lint_error'


def test_diagnose_failures_empty_when_no_failures() -> None:
    rows: list[dict[str, object]] = [
        {'step': 'ruff', 'state': 'Succeeded', 'log_tail': ''},
    ]
    assert diagnose_failures(rows) == []


# ─── summarise_dispatch ──────────────────────────────────────────────────────


def _mk(classification: str, action: str) -> StepFailure:
    return StepFailure(
        pipelinerun='run-x',
        step_name='x',
        log_tail='',
        classification=classification,
        action=action,
    )


def test_dispatch_empty_returns_escalate() -> None:
    """Calling dispatch with no failures is a bug — surface it rather than silently passing."""
    assert summarise_dispatch([]) == ACTION_ESCALATE


def test_dispatch_all_rebase_returns_rebase() -> None:
    failures = [
        _mk('git_merge_conflict', ACTION_REBASE),
        _mk('git_merge_conflict', ACTION_REBASE),
    ]
    assert summarise_dispatch(failures) == ACTION_REBASE


def test_dispatch_any_fix_code_takes_precedence_over_retry_and_rebase() -> None:
    """Code fixes beat transient retries — fix the cause, don't paper over it."""
    failures = [
        _mk('tekton_step_timeout', ACTION_RETRY),
        _mk('ruff_lint_error', ACTION_FIX_CODE),
        _mk('git_merge_conflict', ACTION_REBASE),
    ]
    assert summarise_dispatch(failures) == ACTION_FIX_CODE


def test_dispatch_any_fix_test_when_no_fix_code() -> None:
    failures = [
        _mk('tekton_step_timeout', ACTION_RETRY),
        _mk('pytest_test_failure', ACTION_FIX_TEST),
    ]
    assert summarise_dispatch(failures) == ACTION_FIX_TEST


def test_dispatch_all_retry_returns_retry() -> None:
    failures = [_mk('tekton_step_timeout', ACTION_RETRY)]
    assert summarise_dispatch(failures) == ACTION_RETRY


def test_dispatch_any_escalate_returns_escalate() -> None:
    """An OOM or unknown failure forces escalate even if a retry is also present."""
    failures = [
        _mk('tekton_step_timeout', ACTION_RETRY),
        _mk('tekton_step_oom', ACTION_ESCALATE),
    ]
    assert summarise_dispatch(failures) == ACTION_ESCALATE


def test_dispatch_mixed_rebase_and_retry_escalates() -> None:
    """Rebase + retry isn't a clean set — escalate rather than guess."""
    failures = [
        _mk('git_merge_conflict', ACTION_REBASE),
        _mk('tekton_step_timeout', ACTION_RETRY),
    ]
    assert summarise_dispatch(failures) == ACTION_ESCALATE
