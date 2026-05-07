"""Unit tests for gate.tools.coverage parsers."""

from __future__ import annotations

from gate.tools.coverage import parse_coverage_comment, parse_coverage_threshold_from_yaml

GCP_OK = """<!-- leartech-coverage-gcp -->
### :white_check_mark: Coverage: 100.0% (threshold 60.0%) `[gcp]` — PASS

11 of 11 lines hit.
"""

AZ_FAIL = """<!-- leartech-coverage-az -->
### :x: Coverage: 42.5% (threshold 60.0%) `[az]` — FAIL

42 of 99 lines hit.
"""

YAML_WITH_THRESHOLD = """
spec:
  pipelineSpec:
    tasks:
    - name: ng-test
      taskSpec:
        stepTemplate:
          env:
          - name: COVERAGE_THRESHOLD
            value: "80.0"
"""

YAML_NO_THRESHOLD = """
spec:
  pipelineSpec:
    tasks:
    - name: build
      taskSpec: {}
"""


def test_parses_passing_coverage() -> None:
    r = parse_coverage_comment(GCP_OK)
    assert r is not None
    assert r.cluster == 'gcp'
    assert r.coverage_pct == 100.0
    assert r.threshold_pct == 60.0
    assert r.verdict == 'PASS'
    assert r.passed


def test_parses_failing_coverage() -> None:
    r = parse_coverage_comment(AZ_FAIL)
    assert r is not None
    assert r.cluster == 'az'
    assert r.coverage_pct == 42.5
    assert r.threshold_pct == 60.0
    assert r.verdict == 'FAIL'
    assert not r.passed


def test_returns_none_for_non_coverage_comment() -> None:
    assert parse_coverage_comment('## AI Code Review: 100/100') is None
    assert parse_coverage_comment('') is None


def test_parses_threshold_from_yaml() -> None:
    assert parse_coverage_threshold_from_yaml(YAML_WITH_THRESHOLD) == 80.0


def test_returns_none_when_no_threshold_in_yaml() -> None:
    assert parse_coverage_threshold_from_yaml(YAML_NO_THRESHOLD) is None
