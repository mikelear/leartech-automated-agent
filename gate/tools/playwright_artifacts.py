"""Playwright run + artifact metadata parsed from end2end-ui sticky comments.

The catalog's `tasks/end2end-ui/pullrequest.yaml` posts one sticky per cluster:

    <!-- leartech-end2end-ui-gcp -->
    :white_check_mark: **End-to-end UI: PASS** `[gcp]` — 9/9 browser tests passed

    Preview: https://<repo>-pr<n>.<cluster-domain>

    **Artifacts** (screenshots, videos, traces):

    - :camera: [<spec> screenshot](<gcs-url>)
    - :movie_camera: [<spec> video](<gcs-url>)
    - :mag: [<spec> trace](<gcs-url>)

We parse:
- Verdict (PASS/FAIL) + numeric summary (passed/total)
- Artifact list grouped by spec, classified by kind (screenshot|video|trace)

GCS URLs are publicly readable on this bucket — no gcloud auth required for download.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_ARTIFACT_HEADERS = {'User-Agent': 'leartech-automated-agent/0.1'}

# `:white_check_mark: **End-to-end UI: PASS** `[gcp]` — 9/9 browser tests passed`
_HEADER_RE = re.compile(
    r':(?P<emoji>white_check_mark|x|warning|grey_question):\s+\*\*End-to-end UI:\s+(?P<verdict>PASS|FAIL[^*]*)\*\*\s+`\[(?P<cluster>gcp|az)\]`(?:\s+—\s+(?P<passed>\d+)/(?P<total>\d+)\s+browser tests passed)?',
)
# `- :camera: [<spec-name> screenshot](<url>)`
_ARTIFACT_RE = re.compile(
    r'-\s+:(?P<icon>camera|movie_camera|mag):\s+\[(?P<label>[^\]]+?)\s+(?P<kind>screenshot|video|trace)\]\((?P<url>https://[^)]+)\)',
)
_ICON_TO_KIND = {'camera': 'screenshot', 'movie_camera': 'video', 'mag': 'trace'}


# Banned text-based locator patterns. We only flag the *actual* text-content selectors —
# `getByText(...)` and the various `locator('text=...')` / `locator(':has-text(...)')` shapes.
# CSS attribute selectors like `locator('[data-testid="..."]')` or `locator('input[type="password"]')`
# are *not* fragile in the same way — they target structure, not user-visible copy.
_FRAGILE_TEXT_SELECTOR_RE = re.compile(
    r'\b(?:'
    r'getByText\s*\('
    r"|locator\s*\(\s*['\"]\s*text\s*="
    r"|locator\s*\(\s*['\"]\s*:(?:has-text|text-is|text-matches|text)\s*\("
    r')',
)


def is_fragile_text_selector(line: str) -> bool:
    """Returns True if `line` introduces a text-content Playwright selector that breaks on copy edits."""
    return _FRAGILE_TEXT_SELECTOR_RE.search(line) is not None


@dataclass(frozen=True)
class Artifact:
    spec_name: str  # e.g. '01-page-loads-page-loads-app-root-element-renders'
    kind: str  # 'screenshot' | 'video' | 'trace'
    url: str
    cluster: str


@dataclass(frozen=True)
class PlaywrightRun:
    cluster: str
    emoji: str
    verdict: str  # 'PASS' or 'FAIL ...'
    passed: int  # tests passed (0 if comment didn't include the count, e.g. preview-gate-timeout)
    total: int  # total tests (0 if no count)
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)

    @property
    def passed_all(self) -> bool:
        return self.emoji == 'white_check_mark'

    def specs(self) -> list[str]:
        """Distinct spec names that have at least one artifact attached."""
        return sorted({a.spec_name for a in self.artifacts})

    def artifact_for(self, spec_name: str, kind: str) -> Artifact | None:
        for a in self.artifacts:
            if a.spec_name == spec_name and a.kind == kind:
                return a
        return None


def parse_playwright_sticky_comment(body: str) -> PlaywrightRun | None:
    """Extract a PlaywrightRun from one sticky comment body. None if not an end2end-ui sticky."""
    if 'leartech-end2end-ui-' not in body:
        return None
    header = _HEADER_RE.search(body)
    if not header:
        return None
    cluster = header.group('cluster')
    artifacts: list[Artifact] = []
    for match in _ARTIFACT_RE.finditer(body):
        artifacts.append(
            Artifact(
                spec_name=match.group('label'),
                kind=match.group('kind'),
                url=match.group('url'),
                cluster=cluster,
            )
        )
    return PlaywrightRun(
        cluster=cluster,
        emoji=header.group('emoji'),
        verdict=header.group('verdict').strip(),
        passed=int(header.group('passed') or 0),
        total=int(header.group('total') or 0),
        artifacts=tuple(artifacts),
    )


def _gh(args: list[str]) -> str:
    result = subprocess.run(['gh', *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {result.stderr.strip()}')
    return result.stdout


def read_playwright_runs(repo: str, pr_number: int) -> list[PlaywrightRun]:
    """Returns one PlaywrightRun per cluster comment found. Empty if end2end-ui hasn't posted yet."""
    qualified = repo if '/' in repo else f'mikelear/{repo}'
    raw = _gh(['pr', 'view', str(pr_number), '-R', qualified, '--json', 'comments'])
    comments = json.loads(raw).get('comments', [])

    runs: list[PlaywrightRun] = []
    seen_clusters: set[str] = set()
    # Newest-first so re-runs supersede older comments per cluster.
    for c in reversed(comments):
        run = parse_playwright_sticky_comment(c.get('body', ''))
        if run is None or run.cluster in seen_clusters:
            continue
        seen_clusters.add(run.cluster)
        runs.append(run)
    return runs


def _require_https(url: str) -> None:
    if not url.startswith('https://'):
        raise ValueError(f'artifact URL must be https://, got {url!r}')


def download_artifact(url: str, dest: Path) -> Path:
    """Fetch a public GCS artifact to `dest`. Returns dest. Raises on HTTP error or empty body."""
    _require_https(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resp = httpx.get(url, timeout=60, follow_redirects=True, headers=_ARTIFACT_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f'GET {url} → HTTP {resp.status_code}')
    if not resp.content:
        raise RuntimeError(f'GET {url} returned empty body')
    dest.write_bytes(resp.content)
    return dest


def head_artifact(url: str, timeout: float = 15.0) -> int:
    """Return the HTTP status of a HEAD request — use to verify artifact reachability without downloading."""
    _require_https(url)
    resp = httpx.head(url, timeout=timeout, follow_redirects=True, headers=_ARTIFACT_HEADERS)
    return int(resp.status_code)
