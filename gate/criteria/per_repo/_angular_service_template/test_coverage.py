"""Coverage criteria for leartech-auth-ui (Tier 1 — sticky-comment driven, per cluster).

Reads the catalog's `tasks/ng-test/pullrequest.yaml` sticky comments rather than fetching
LCOV directly — the comment is the only artifact reachable without GCS auth, and it
already carries (coverage%, threshold%, PASS/FAIL) per cluster.
"""

from __future__ import annotations

import pytest

from gate.tools import CoverageReport, PRContext, read_coverage_from_pr_comments

pytestmark = pytest.mark.unit


@pytest.fixture(scope='session')
def coverage_reports(pr_context: PRContext) -> list[CoverageReport]:
    reports = read_coverage_from_pr_comments(pr_context.repo, pr_context.number)
    if not reports:
        pytest.skip('No leartech-coverage-* sticky comments posted yet (test check still pending or repo opts out)')
    return reports


@pytest.mark.parametrize('cluster', ['gcp', 'az'])
def test_coverage_status_passes(coverage_reports: list[CoverageReport], cluster: str) -> None:
    """The sticky comment for `cluster` reports PASS (coverage ≥ threshold per the catalog's own decision)."""
    for r in coverage_reports:
        if r.cluster == cluster:
            assert r.passed, (
                f'Coverage on [{cluster}]: {r.coverage_pct}% vs threshold {r.threshold_pct}% — verdict {r.verdict}'
            )
            return
    pytest.skip(f'No leartech-coverage-{cluster} sticky comment posted (cluster may not have run yet)')


@pytest.mark.parametrize('cluster', ['gcp', 'az'])
def test_coverage_meets_threshold(coverage_reports: list[CoverageReport], cluster: str) -> None:
    """Numeric coverage% ≥ threshold% per cluster's sticky comment.

    Belt-and-braces against `test_coverage_status_passes` — that one trusts the catalog's
    PASS/FAIL decision; this one re-asserts against the raw numbers in case the catalog's
    threshold logic ever changes.
    """
    for r in coverage_reports:
        if r.cluster == cluster:
            assert r.coverage_pct >= r.threshold_pct, (
                f'Coverage on [{cluster}]: {r.coverage_pct}% < threshold {r.threshold_pct}%'
            )
            return
    pytest.skip(f'No leartech-coverage-{cluster} sticky comment posted')
