"""Unit tests for gate.tools.end2end_gate.

Covers:
- Gate detection (with/without cluster prefix; positive + negative)
- results.json parsing in the presence of surrounding noise
- Per-test extraction (failed-only)
- real_failure vs preview_infra classification
- end2end-ui artefact annotation when matching specs
- Soft-fail orchestration when the injected step_logs raises

Reference shape: the 2026-06-13 PR #58 on leartech-auth-service log, quoted
verbatim in the v6p0.5 step-1 initiative goal.
"""

from __future__ import annotations

import json

from gate.tools.end2end_gate import (
    End2EndTest,
    build_end2end_failure,
    classify_end2end_failure,
    extract_failed_tests,
    fetch_end2end_failure,
    is_end2end_gate,
    is_end2end_ui_gate,
    parse_results_json_from_log,
)
from gate.tools.playwright_artifacts import Artifact

# ─── Fixtures ─────────────────────────────────────────────────────────────────


CANONICAL_RESULTS_JSON = {
    'success': False,
    'summary': '1/4 checks passed',
    'tests': [
        {'name': '00-seed-test-data', 'status': 'pass'},
        {'name': '01-smoke', 'status': 'fail', 'message': 'GET /health/live HTTP 000 FAIL'},
        {'name': '02-auth', 'status': 'fail', 'message': 'GET /api/auth HTTP 000 FAIL'},
        {'name': '03-roundtrip', 'status': 'fail', 'message': 'POST /api/widget HTTP 000 FAIL'},
    ],
}

CANONICAL_LOG_TAIL = f"""
+ ./end2end/01-smoke.sh
- 01-smoke FAIL: GET /health/live HTTP 000 FAIL
+ ./end2end/02-auth.sh
- 02-auth  FAIL: GET /api/auth HTTP 000 FAIL
+ writing results to /tmp/results.json
=== results.json ===
{json.dumps(CANONICAL_RESULTS_JSON, indent=2)}
=== end ===
exit 1
"""

# A real-failure shape: app responded with the wrong code / body.
REAL_FAILURE_RESULTS = {
    'success': False,
    'summary': '2/3 checks passed',
    'tests': [
        {'name': '01-smoke', 'status': 'pass'},
        {'name': '02-create', 'status': 'pass'},
        {
            'name': '03-list',
            'status': 'fail',
            'message': "GET /api/items expected 200, got 500: {'detail': 'internal error'}",
        },
    ],
}

# Mixed: one preview-infra-shaped, one app-shaped → classification = real_failure
MIXED_FAILURE_RESULTS = {
    'success': False,
    'summary': '1/3 checks passed',
    'tests': [
        {'name': '01-smoke', 'status': 'pass'},
        {'name': '02-fast', 'status': 'fail', 'message': 'HTTP 000 — no response'},
        {'name': '03-list', 'status': 'fail', 'message': 'assertion failed: items.length == 5 but got 0'},
    ],
}


# ─── is_end2end_gate / is_end2end_ui_gate ────────────────────────────────────


def test_detects_end2end_with_and_without_cluster_prefix() -> None:
    for name in ('end2end', 'gcp/end2end', 'az/end2end'):
        assert is_end2end_gate(name), name


def test_detects_end2end_ui_with_and_without_cluster_prefix() -> None:
    for name in ('end2end-ui', 'gcp/end2end-ui', 'az/end2end-ui'):
        assert is_end2end_gate(name), name
        assert is_end2end_ui_gate(name), name


def test_rejects_other_checks() -> None:
    for name in ('lint', 'pr', 'gcp/pr', 'az/lint', 'ai-review', 'end2end-foo', 'gcp/end2end-extra'):
        assert not is_end2end_gate(name), name


def test_end2end_is_not_end2end_ui() -> None:
    assert not is_end2end_ui_gate('end2end')
    assert not is_end2end_ui_gate('gcp/end2end')
    assert not is_end2end_ui_gate('az/end2end')


# ─── parse_results_json_from_log ──────────────────────────────────────────────


def test_parses_results_json_surrounded_by_noise() -> None:
    doc = parse_results_json_from_log(CANONICAL_LOG_TAIL)
    assert doc is not None
    assert doc['success'] is False
    assert doc['summary'] == '1/4 checks passed'
    assert len(doc['tests']) == 4


def test_parse_returns_none_on_empty_log() -> None:
    assert parse_results_json_from_log('') is None


def test_parse_returns_none_when_no_recognisable_block() -> None:
    log = """
    some random text
    + ./end2end/01-smoke.sh
    {"unrelated": "object", "without": "tests-key"}
    + done
    """
    assert parse_results_json_from_log(log) is None


def test_parse_ignores_unrelated_json_objects_before_results() -> None:
    """Other JSON blocks (helm release, kubectl metadata) must NOT win the match."""
    log = (
        '{"helm_release": {"name": "preview"}}\n'
        '{"info": "no tests here"}\n' + json.dumps(CANONICAL_RESULTS_JSON, indent=2) + '\n'
    )
    doc = parse_results_json_from_log(log)
    assert doc is not None
    assert doc['summary'] == '1/4 checks passed'


def test_parse_tolerates_nested_objects() -> None:
    """Tests rows that contain nested dicts (per-test metadata) shouldn't break parsing."""
    payload = {
        'success': False,
        'summary': '0/1 checks passed',
        'tests': [
            {
                'name': '01-smoke',
                'status': 'fail',
                'message': 'boom',
                'meta': {'nested': {'deeply': True}},
            }
        ],
    }
    log = 'preamble\n' + json.dumps(payload) + '\nepilogue\n'
    doc = parse_results_json_from_log(log)
    assert doc is not None
    assert doc['tests'][0]['meta']['nested']['deeply'] is True


# ─── extract_failed_tests ─────────────────────────────────────────────────────


def test_extracts_failed_tests_only() -> None:
    failed = extract_failed_tests(CANONICAL_RESULTS_JSON)
    names = [t.name for t in failed]
    assert names == ['01-smoke', '02-auth', '03-roundtrip']
    assert all(t.status == 'fail' for t in failed)
    assert failed[0].message == 'GET /health/live HTTP 000 FAIL'


def test_extract_failed_tests_handles_missing_keys() -> None:
    """Defensive: malformed rows must NOT raise."""
    payload = {
        'tests': [
            {},  # no name, no status
            {'status': 'fail'},  # no name, no message
            'not a dict',  # skipped
            {'name': 'X', 'status': 'FAIL'},  # uppercase status
        ]
    }
    failed = extract_failed_tests(payload)
    # Both the empty-status row and the FAIL row count; the lowercased status check is case-insensitive.
    names = [t.name for t in failed]
    assert '<unknown>' in names
    assert 'X' in names


# ─── classify_end2end_failure ────────────────────────────────────────────────


def test_classifies_canonical_pr58_as_preview_infra() -> None:
    """The PR #58 reference case (all failures are HTTP 000) is preview_infra."""
    assert classify_end2end_failure(CANONICAL_RESULTS_JSON, CANONICAL_LOG_TAIL) == 'preview_infra'


def test_classifies_real_app_failure_as_real_failure() -> None:
    assert classify_end2end_failure(REAL_FAILURE_RESULTS, '') == 'real_failure'


def test_classifies_mixed_failures_as_real_failure() -> None:
    """If ANY failure is non-infra-shaped, the overall verdict is real_failure."""
    assert classify_end2end_failure(MIXED_FAILURE_RESULTS, '') == 'real_failure'


def test_falls_back_to_log_scan_when_no_results_json() -> None:
    """When the harness never dumped results, scan the raw log for infra signals."""
    infra_log = """
    + curl -fsS https://preview-foo.az.leartech.com/health/live
    curl: (6) Could not resolve host: preview-foo.az.leartech.com
    preview-gate timed out after 600s
    """
    assert classify_end2end_failure(None, infra_log) == 'preview_infra'


def test_falls_back_to_real_failure_when_no_infra_signal() -> None:
    assert classify_end2end_failure(None, 'pytest collected 0 tests; everything green') == 'real_failure'


# ─── build_end2end_failure (top-level integration) ───────────────────────────


def test_build_failure_returns_none_for_unrelated_gate() -> None:
    assert build_end2end_failure(gate='lint', log_tail='whatever') is None
    assert build_end2end_failure(gate='gcp/pr', log_tail='whatever') is None


def test_build_failure_pr58_shape() -> None:
    """End-to-end: parse the canonical PR #58 log → preview_infra, 3 failed tests, not actionable."""
    failure = build_end2end_failure(gate='az/end2end', log_tail=CANONICAL_LOG_TAIL)
    assert failure is not None
    assert failure.gate == 'az/end2end'
    assert failure.classification == 'preview_infra'
    assert failure.summary == '1/4 checks passed'
    assert len(failure.failed_tests) == 3
    assert failure.failed_tests[0].name == '01-smoke'
    assert failure.actionable is False  # preview-infra is NOT actionable by the agent
    assert failure.artifact_urls == ()


def test_build_failure_real_failure_is_actionable() -> None:
    log = json.dumps(REAL_FAILURE_RESULTS)
    failure = build_end2end_failure(gate='gcp/end2end', log_tail=log)
    assert failure is not None
    assert failure.classification == 'real_failure'
    assert failure.actionable is True
    assert len(failure.failed_tests) == 1
    assert '03-list' == failure.failed_tests[0].name


def test_build_failure_no_results_json() -> None:
    failure = build_end2end_failure(gate='az/end2end', log_tail='step crashed before dump')
    assert failure is not None
    assert failure.classification == 'real_failure'
    assert failure.summary == 'results.json not found in step log'
    assert failure.failed_tests == ()


def test_build_failure_to_dict_payload_shape() -> None:
    """The dict payload matches the contract documented in end2end_gate.py."""
    failure = build_end2end_failure(gate='az/end2end', log_tail=CANONICAL_LOG_TAIL)
    assert failure is not None
    payload = failure.to_dict()
    assert payload['kind'] == 'end2end_failure'
    assert payload['gate'] == 'az/end2end'
    assert payload['classification'] == 'preview_infra'
    assert payload['summary'] == '1/4 checks passed'
    assert payload['actionable'] is False
    assert isinstance(payload['failed_tests'], list)
    first = payload['failed_tests'][0]
    assert first['name'] == '01-smoke'
    assert first['message'] == 'GET /health/live HTTP 000 FAIL'
    assert first['trace_url'] is None
    assert first['screenshot_url'] is None
    # No artefacts on a non-UI gate.
    assert 'artifact_urls' not in payload


# ─── end2end-ui artefact annotation ──────────────────────────────────────────


def test_end2end_ui_attaches_artifact_urls_when_specs_match() -> None:
    """For end2end-ui, screenshot/trace URLs from Playwright must annotate failed tests."""
    ui_results = {
        'success': False,
        'summary': '1/2 checks passed',
        'tests': [
            {'name': '01-page-loads', 'status': 'pass'},
            {
                'name': '02-login-form',
                'status': 'fail',
                'message': 'expect(page).toHaveURL: /dashboard',
            },
        ],
    }
    log = 'noise\n' + json.dumps(ui_results) + '\nmore noise\n'
    artifacts = (
        Artifact(
            spec_name='02-login-form-login-page-renders-form-or-content',
            kind='screenshot',
            url='https://storage.googleapis.com/x/02-login-form-screenshot.png',
            cluster='gcp',
        ),
        Artifact(
            spec_name='02-login-form-login-page-renders-form-or-content',
            kind='trace',
            url='https://storage.googleapis.com/x/02-login-form-trace.zip',
            cluster='gcp',
        ),
    )
    failure = build_end2end_failure(gate='gcp/end2end-ui', log_tail=log, ui_artifacts=artifacts)
    assert failure is not None
    assert failure.is_ui
    assert failure.classification == 'real_failure'  # app-shaped message, not infra
    assert failure.actionable is True
    assert len(failure.failed_tests) == 1
    t = failure.failed_tests[0]
    assert t.screenshot_url is not None
    assert t.screenshot_url.endswith('02-login-form-screenshot.png')
    assert t.trace_url is not None
    assert t.trace_url.endswith('02-login-form-trace.zip')
    # And artifact_urls is round-tripped in the payload.
    payload = failure.to_dict()
    assert 'artifact_urls' in payload
    assert len(payload['artifact_urls']) == 2


def test_end2end_ui_payload_omits_artifacts_when_none_provided() -> None:
    """No Playwright artefacts → no artifact_urls key in the payload."""
    log = json.dumps(REAL_FAILURE_RESULTS)
    failure = build_end2end_failure(gate='gcp/end2end-ui', log_tail=log)
    assert failure is not None
    assert failure.artifact_urls == ()
    assert 'artifact_urls' not in failure.to_dict()


def test_end2end_gate_ignores_artifacts_for_non_ui_gate() -> None:
    """An end2end (non-UI) gate must NOT surface artifact_urls even when supplied."""
    artifacts = (
        Artifact(
            spec_name='01-smoke',
            kind='screenshot',
            url='https://storage.googleapis.com/x/01-smoke.png',
            cluster='gcp',
        ),
    )
    failure = build_end2end_failure(gate='gcp/end2end', log_tail=CANONICAL_LOG_TAIL, ui_artifacts=artifacts)
    assert failure is not None
    assert failure.artifact_urls == ()


# ─── fetch_end2end_failure (orchestrator) ────────────────────────────────────


def test_fetch_invokes_step_logs_and_builds_payload() -> None:
    calls: list[tuple[str, str, str, int]] = []

    def fake_step_logs(prun: str, step: str, cluster: str, tail: int) -> str:
        calls.append((prun, step, cluster, tail))
        return CANONICAL_LOG_TAIL

    failure = fetch_end2end_failure(
        gate='az/end2end',
        pipelinerun_name='auth-svc-pr58-abc',
        cluster='az',
        step_logs_fn=fake_step_logs,
    )
    assert failure is not None
    assert failure.classification == 'preview_infra'
    assert calls == [('auth-svc-pr58-abc', 'run-tests', 'az', 500)]


def test_fetch_soft_fails_on_step_logs_exception() -> None:
    """Transient kubectl errors must NOT crash the watcher — return None and log."""

    def raises_step_logs(prun: str, step: str, cluster: str, tail: int) -> str:
        raise RuntimeError('kubectl context error: temporary')

    failure = fetch_end2end_failure(
        gate='az/end2end',
        pipelinerun_name='auth-svc-pr58-abc',
        cluster='az',
        step_logs_fn=raises_step_logs,
    )
    assert failure is None


def test_fetch_returns_none_for_unrelated_gate() -> None:
    """Caller filtering didn't catch it → orchestrator still refuses to do work."""

    def must_not_be_called(*_args: object, **_kw: object) -> str:
        raise AssertionError('step_logs_fn must not be invoked for non-end2end gate')

    assert (
        fetch_end2end_failure(
            gate='lint',
            pipelinerun_name='x',
            cluster='az',
            step_logs_fn=must_not_be_called,  # type: ignore[arg-type]
        )
        is None
    )


def test_fetch_passes_ui_artifacts_through() -> None:
    """Artefacts supplied by the caller must end up on the End2EndFailure."""

    def fake_step_logs(prun: str, step: str, cluster: str, tail: int) -> str:
        # Minimal results.json with one matching failed spec.
        payload = {
            'success': False,
            'summary': '0/1 checks passed',
            'tests': [{'name': '01-smoke', 'status': 'fail', 'message': 'expected 200, got 500'}],
        }
        return json.dumps(payload)

    artifacts = (Artifact(spec_name='01-smoke', kind='screenshot', url='https://x/p.png', cluster='gcp'),)
    failure = fetch_end2end_failure(
        gate='gcp/end2end-ui',
        pipelinerun_name='auth-ui-pr30-xyz',
        cluster='gcp',
        step_logs_fn=fake_step_logs,
        ui_artifacts=artifacts,
    )
    assert failure is not None
    assert failure.artifact_urls == artifacts
    assert failure.failed_tests[0].screenshot_url == 'https://x/p.png'


# ─── End2EndTest dataclass invariants ─────────────────────────────────────────


def test_end2end_test_failed_property() -> None:
    assert End2EndTest(name='x', status='fail').failed
    assert End2EndTest(name='x', status='FAIL').failed
    assert not End2EndTest(name='x', status='pass').failed
    assert not End2EndTest(name='x', status='skip').failed
