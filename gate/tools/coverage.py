"""Coverage threshold + per-cluster coverage parsing.

Two sources of truth:

- **Threshold** lives in the consumer repo's `.lighthouse/jenkins-x/test.yaml` as the
  `COVERAGE_THRESHOLD` env var (golden std: 60% Angular, 60% Go, configurable per repo).
- **Per-PR coverage** is posted as sticky comments by `tasks/ng-test/pullrequest.yaml`
  with markers `<!-- leartech-coverage-gcp -->` / `<!-- leartech-coverage-az -->`. We
  parse the comment header `Coverage: X% (threshold Y%) [gcp] — PASS|FAIL`.

The comment is the only artifact we can read without GCS auth; deeper artifact-level
analysis (LCOV file diffs, per-file delta) lands once `playwright_artifacts.py` brings
GCS plumbing in for the Playwright tier.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

# `### :white_check_mark: Coverage: 100.0% (threshold 60.0%) `[gcp]` — PASS`
_COMMENT_HEADER_RE = re.compile(
    r'Coverage:\s*([\d.]+)%\s*\(threshold\s*([\d.]+)%\)\s*`\[(?P<cluster>gcp|az)\]`\s*—\s*(?P<verdict>PASS|FAIL)',
)
# Tolerant match for `COVERAGE_THRESHOLD` env value in the consumer's .lighthouse YAML.
_THRESHOLD_RE = re.compile(
    r'name:\s*COVERAGE_THRESHOLD\s*\n\s*value:\s*["\']?(?P<v>[\d.]+)["\']?',
)


@dataclass(frozen=True)
class CoverageReport:
    cluster: str  # 'gcp' | 'az'
    coverage_pct: float
    threshold_pct: float
    verdict: str  # 'PASS' | 'FAIL'

    @property
    def passed(self) -> bool:
        return self.verdict == 'PASS'


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def read_coverage_threshold(repo: str, ref: str = 'main') -> float | None:
    """Fetch the COVERAGE_THRESHOLD env value from .lighthouse/jenkins-x/test.yaml on `ref`.

    Returns None if the repo has no test.yaml (e.g. backend service with go-test wrapper instead).
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    try:
        raw = _gh(['api', f'repos/{qualified}/contents/.lighthouse/jenkins-x/test.yaml', '--jq', '.content'])
    except RuntimeError:
        return None

    import base64

    yaml_text = base64.b64decode(raw).decode('utf-8', errors='replace')
    return parse_coverage_threshold_from_yaml(yaml_text)


def parse_coverage_comment(body: str) -> CoverageReport | None:
    """Extract a coverage report from a single comment body. None if not a leartech-coverage sticky."""
    if 'leartech-coverage-' not in body:
        return None
    match = _COMMENT_HEADER_RE.search(body)
    if not match:
        return None
    return CoverageReport(
        cluster=match.group('cluster'),
        coverage_pct=float(match.group(1)),
        threshold_pct=float(match.group(2)),
        verdict=match.group('verdict'),
    )


def parse_coverage_threshold_from_yaml(yaml_text: str) -> float | None:
    """Parse a COVERAGE_THRESHOLD env value out of a `.lighthouse/jenkins-x/test.yaml` body."""
    match = _THRESHOLD_RE.search(yaml_text)
    return float(match.group('v')) if match else None


def read_coverage_from_pr_comments(repo: str, pr_number: int) -> list[CoverageReport]:
    """Parse the leartech-coverage-{gcp,az} sticky comments on a PR.

    Returns one CoverageReport per cluster comment found; empty list if none posted yet.
    """
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    raw = _gh(['pr', 'view', str(pr_number), '-R', qualified, '--json', 'comments'])
    comments = json.loads(raw).get('comments', [])

    reports: list[CoverageReport] = []
    for c in comments:
        report = parse_coverage_comment(c.get('body', ''))
        if report is not None:
            reports.append(report)
    return reports
