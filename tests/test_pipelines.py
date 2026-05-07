"""Unit tests for gate.tools.pipelines — exercises the JSON parsing + classification."""

from __future__ import annotations

import pytest

from gate.tools.pipelines import (
    PipelineCheck,
    parse_pipelines_json,
    parse_status_check_rollup,
    parse_target_url,
)

# Realistic gh `statusCheckRollup` shape captured from a real auth-ui PR.
REAL_ROLLUP = [
    {
        '__typename': 'StatusContext',
        'context': 'az/dynamic-scan',
        'state': 'FAILURE',
        'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/rtech-auth-ui-pr-25-dynamic-scan-pgmvl',
    },
    {
        '__typename': 'StatusContext',
        'context': 'gcp/lint',
        'state': 'SUCCESS',
        'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/lear-leartech-auth-ui-pr-25-lint-abc',
    },
    # Entry with no recognisable targetUrl — should be skipped.
    {
        '__typename': 'StatusContext',
        'context': 'random/external',
        'state': 'SUCCESS',
        'targetUrl': 'https://example.com/some-other-system',
    },
]

# Real shape of pr-pipelines.sh --json output: clean JSON array followed by failing-step
# detail lines. Pinning this so the parser stays tolerant of the trailing junk.
JSON_WITH_TRAILING_FAILURE_DETAIL = """[
  {"cluster": "az", "check": "end2end", "state": "FAILURE", "pipelinerun": "r-leartech-auth-ui-pr-35-end2end-9z5sx"},
  {"cluster": "gcp", "check": "lint", "state": "SUCCESS", "pipelinerun": "rtech-auth-ui-pr-35-lint-abc"}
]
✗ az/end2end  failing-step=step-run-end2end  exit=1  (r-leartech-auth-ui-pr-35-end2end-9z5sx)
✗ gcp/end2end-ui  failing-step=step-end2end-ui  exit=1  (eartech-auth-ui-pr-35-end2end-ui-9j5dp)
"""


def test_pipeline_check_passed_classifies_success() -> None:
    check = PipelineCheck(cluster='gcp', check='lint', state='SUCCESS', pipelinerun='pr-1')
    assert check.passed
    assert not check.failed
    assert check.terminal


def test_pipeline_check_failed_classifies_failure() -> None:
    for state in ('FAILURE', 'ERROR'):
        check = PipelineCheck(cluster='az', check='test', state=state, pipelinerun='pr-2')
        assert check.failed, f'{state} should classify as failed'
        assert check.terminal
        assert not check.passed


def test_pipeline_check_pending_is_non_terminal() -> None:
    for state in ('PENDING', 'IN_PROGRESS'):
        check = PipelineCheck(cluster='gcp', check='ai-review', state=state, pipelinerun='pr-3')
        assert not check.terminal
        assert not check.passed
        assert not check.failed


def test_parser_tolerates_trailing_failure_detail_lines() -> None:
    rows = parse_pipelines_json(JSON_WITH_TRAILING_FAILURE_DETAIL)
    assert len(rows) == 2
    assert rows[0]['cluster'] == 'az'
    assert rows[0]['check'] == 'end2end'
    assert rows[0]['state'] == 'FAILURE'
    assert rows[1]['cluster'] == 'gcp'
    assert rows[1]['state'] == 'SUCCESS'


def test_parser_handles_empty_input() -> None:
    assert parse_pipelines_json('') == []
    assert parse_pipelines_json('   \n  ') == []


def test_parser_rejects_non_array_root() -> None:
    with pytest.raises(ValueError, match='expected JSON array'):
        parse_pipelines_json('{"cluster": "gcp"}\n')


def test_parse_target_url_gcp() -> None:
    result = parse_target_url('https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/foo-bar')
    assert result == ('gcp', 'foo-bar')


def test_parse_target_url_az() -> None:
    result = parse_target_url('https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/foo-bar')
    assert result == ('az', 'foo-bar')


def test_parse_target_url_unknown_subdomain_returns_none() -> None:
    assert parse_target_url('https://tekton-dashboard-jx.staging.leartech.com/#/...') is None


def test_parse_target_url_non_tekton_url_returns_none() -> None:
    assert parse_target_url('https://example.com/some-system') is None
    assert parse_target_url('') is None


def test_parse_status_check_rollup_extracts_known_clusters() -> None:
    checks = parse_status_check_rollup(REAL_ROLLUP)
    # Two recognisable clusters; the example.com entry is skipped.
    assert len(checks) == 2
    az = next(c for c in checks if c.cluster == 'az')
    assert az.check == 'dynamic-scan'  # cluster prefix stripped
    assert az.state == 'FAILURE'
    assert az.pipelinerun == 'rtech-auth-ui-pr-25-dynamic-scan-pgmvl'
    gcp = next(c for c in checks if c.cluster == 'gcp')
    assert gcp.check == 'lint'
    assert gcp.passed
    assert gcp.terminal


def test_parse_status_check_rollup_handles_empty() -> None:
    assert parse_status_check_rollup([]) == []
