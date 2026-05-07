"""Cross-cutting Tekton pipeline criteria — apply to every repo, every PR."""

from __future__ import annotations

import pytest

from gate.tools import PRContext, list_pr_checks
from gate.tools.pipelines import PipelineCheck

pytestmark = pytest.mark.shared

SECURITY_CHECKS = {'security-scan', 'image-scan', 'dynamic-scan'}


def _format_failures(failures: list[PipelineCheck]) -> str:
    return '\n'.join(f'  [{c.cluster}] {c.check} — {c.state} ({c.pipelinerun})' for c in failures)


def test_pr_checks_green(pr_context: PRContext) -> None:
    """Every Tekton check on the PR is SUCCESS across both clusters."""
    checks = list_pr_checks(pr_context.repo, pr_context.number)
    have_checks = bool(checks)
    assert have_checks, f'No pipeline checks found for {pr_context.repo}#{pr_context.number}'

    still_running = bool([c for c in checks if not c.terminal])
    assert not still_running, f'Checks still running:\n{_format_failures([c for c in checks if not c.terminal])}'

    failed_checks = [c for c in checks if c.failed]
    has_failures = bool(failed_checks)
    assert not has_failures, f'Pipeline failures:\n{_format_failures(failed_checks)}'


def test_security_scan_clean(pr_context: PRContext) -> None:
    """All security checks (security-scan + image-scan + dynamic-scan) succeeded on both clusters."""
    checks = list_pr_checks(pr_context.repo, pr_context.number)
    security = [c for c in checks if c.check in SECURITY_CHECKS]
    have_security_checks = bool(security)
    assert have_security_checks, (
        f'No security checks reported for #{pr_context.number} (expected one of {SECURITY_CHECKS})'
    )

    failed_checks = [c for c in security if not c.passed]
    has_failures = bool(failed_checks)
    assert not has_failures, f'Security check failures:\n{_format_failures(failed_checks)}'


# test_ai_review_no_blockers moved to gate/criteria/shared/test_ai_review.py — ai-review is informational
# (no GitHub status check), so the criterion has to read the sticky comment, not the pipelineRun status.
