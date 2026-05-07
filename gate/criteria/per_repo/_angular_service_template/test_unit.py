"""Unit-tier criteria for leartech-auth-ui (Tier 1 of the four-tier model)."""

from __future__ import annotations

import re

import pytest

from gate.tools import PRContext, fetch_pr_diff, list_pr_checks
from gate.tools.pipelines import PipelineCheck

pytestmark = pytest.mark.unit

# Anti-patterns: focused or skipped tests that should never reach main.
_FORBIDDEN_PATTERNS = [
    (re.compile(r'\bit\.only\s*\('), 'it.only(...)'),
    (re.compile(r'\bfit\s*\('), 'fit(...)'),
    (re.compile(r'\bfdescribe\s*\('), 'fdescribe(...)'),
    (re.compile(r'\bdescribe\.only\s*\('), 'describe.only(...)'),
    (re.compile(r'\bit\.skip\s*\('), 'it.skip(...)'),
    (re.compile(r'\bxit\s*\('), 'xit(...)'),
    (re.compile(r'\bxdescribe\s*\('), 'xdescribe(...)'),
]


def _format_failures(failures: list[PipelineCheck]) -> str:
    return '\n'.join(f'  [{c.cluster}] {c.check} — {c.state} ({c.pipelinerun})' for c in failures)


def test_unit_tests_pass(pr_context: PRContext) -> None:
    """The Tekton `test` check (ng-test for Angular) is SUCCESS on every cluster that ran it."""
    checks = list_pr_checks(pr_context.repo, pr_context.number)
    test_checks = [c for c in checks if c.check == 'test']
    assert test_checks, f'No `test` check reported for #{pr_context.number}'

    failures = [c for c in test_checks if not c.passed]
    assert not failures, f'Unit test failures:\n{_format_failures(failures)}'


def test_no_skipped_or_focused_tests(pr_context: PRContext) -> None:
    """Diff must not introduce focused (.only/fit/fdescribe) or skipped (.skip/xit/xdescribe) specs.

    Reviewer-grade: focused tests usually mean someone forgot to remove debug scoping;
    skipped tests usually mean a regression was buried instead of fixed.
    """
    diff = fetch_pr_diff(pr_context.repo, pr_context.number)
    added = [line for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++')]
    hits: list[str] = []
    for line in added:
        for pattern, label in _FORBIDDEN_PATTERNS:
            if pattern.search(line):
                hits.append(f'  {label}: {line.strip()[:120]}')
    assert not hits, 'Forbidden test patterns introduced:\n' + '\n'.join(hits)


def test_unit_spec_count_changed_when_app_changed(pr_context: PRContext) -> None:
    """If functional app code changed, the diff must include at least one .spec.ts change.

    'Functional' = src/app/**.ts excluding spec files. Not a strict gate (a CSS-only or
    template-only change won't trigger this), but covers the common case where TS logic
    changes without a corresponding spec update.
    """
    app_code_changed = any(
        f.startswith('src/app/') and f.endswith('.ts') and not f.endswith('.spec.ts') for f in pr_context.changed_files
    )
    if not app_code_changed:
        pytest.skip('No functional app code changed — spec count requirement does not apply.')

    spec_changed = any(f.endswith('.spec.ts') for f in pr_context.changed_files)
    assert spec_changed, (
        'App code changed without any *.spec.ts update. '
        f'Changed files: {sorted(f for f in pr_context.changed_files if f.startswith("src/app/"))}'
    )
