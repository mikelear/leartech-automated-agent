"""Playwright (Tier 4) criteria for leartech-auth-ui.

Covers execution, selector quality, and artifact integrity. Trace-content analysis
(`test_no_console_errors`, `test_no_failed_network_calls`) is deferred to v1.5 — it
needs a trace.zip parser. Video review lives in `test_video_review.py`.
"""

from __future__ import annotations

import re

import pytest

from gate.tools import (
    Artifact,
    PlaywrightRun,
    PRContext,
    fetch_pr_diff,
    head_artifact,
    is_fragile_text_selector,
    read_playwright_runs,
)

pytestmark = pytest.mark.playwright

# Hard-sleep anti-pattern. Text-selector check lives in gate.tools.is_fragile_text_selector.
_WAIT_FOR_TIMEOUT_RE = re.compile(r'\bpage\.waitForTimeout\s*\(')


@pytest.fixture(scope='session')
def playwright_runs(pr_context: PRContext) -> list[PlaywrightRun]:
    runs = read_playwright_runs(pr_context.repo, pr_context.number)
    if not runs:
        pytest.skip(f'No end2end-ui sticky comments posted yet for #{pr_context.number}')
    return runs


def _run_for(runs: list[PlaywrightRun], cluster: str) -> PlaywrightRun | None:
    for r in runs:
        if r.cluster == cluster:
            return r
    return None


@pytest.mark.parametrize('cluster', ['gcp', 'az'])
def test_specs_pass(playwright_runs: list[PlaywrightRun], cluster: str) -> None:
    """The end2end-ui sticky comment for `cluster` reports PASS — every browser test green."""
    run = _run_for(playwright_runs, cluster)
    if run is None:
        pytest.skip(f'No end2end-ui comment posted for [{cluster}] (cluster may not have run yet)')
    # Capture into a bool local so pytest's assertion-rewrite introspection shows
    # `where False = ok` instead of dumping the entire PlaywrightRun + every artifact URL.
    ok = run.passed_all
    assert ok, f'[{cluster}] end2end-ui {run.verdict}: {run.passed}/{run.total} passed'


@pytest.mark.parametrize('cluster', ['gcp', 'az'])
def test_artifacts_present_for_each_spec(playwright_runs: list[PlaywrightRun], cluster: str) -> None:
    """Every spec referenced in the cluster's run has at least a video and a trace artifact.

    Screenshots are only emitted on failure or final-state, so we don't require them.
    Video + trace are unconditional outputs from playwright-runner.
    """
    run = _run_for(playwright_runs, cluster)
    if run is None or not run.passed_all:
        pytest.skip(f'No PASS run for [{cluster}] — artifact-integrity check N/A')

    missing: list[str] = []
    for spec in run.specs():
        for kind in ('video', 'trace'):
            if run.artifact_for(spec, kind) is None:
                missing.append(f'  [{cluster}] {spec} — missing {kind}')
    assert not missing, 'Missing artifacts:\n' + '\n'.join(missing)


@pytest.mark.parametrize('cluster', ['gcp', 'az'])
def test_artifact_urls_reachable(playwright_runs: list[PlaywrightRun], cluster: str) -> None:
    """Spot-check: video + trace URLs for the first spec respond HTTP 200.

    Avoids HEADing every artifact (~3 per spec × 9 specs = 27 round-trips) — the goal is
    to confirm the GCS upload step actually landed, not exhaustive verification.
    """
    run = _run_for(playwright_runs, cluster)
    if run is None or not run.artifacts:
        pytest.skip(f'No artifacts to probe for [{cluster}]')

    first_spec = run.specs()[0]
    failures: list[str] = []
    for kind in ('video', 'trace'):
        artifact: Artifact | None = run.artifact_for(first_spec, kind)
        if artifact is None:
            continue
        status = head_artifact(artifact.url)
        if status != 200:
            failures.append(f'  [{cluster}] {kind}: HTTP {status} — {artifact.url}')
    assert not failures, 'Artifact URLs not reachable:\n' + '\n'.join(failures)


def test_no_wait_for_timeout(pr_context: PRContext) -> None:
    """Diff must not introduce `page.waitForTimeout(...)` — hard sleeps are flake bait."""
    diff = fetch_pr_diff(pr_context.repo, pr_context.number)
    added = [line for line in diff.splitlines() if line.startswith('+') and not line.startswith('+++')]
    hits = [line.strip()[:120] for line in added if _WAIT_FOR_TIMEOUT_RE.search(line)]
    assert not hits, 'page.waitForTimeout introduced (use waitFor / expect.toHaveX instead):\n' + '\n'.join(hits)


def test_uses_data_testid_selectors(pr_context: PRContext) -> None:
    """Diff in *.spec.ts files must not introduce fragile text-content / locator-text selectors.

    Looks for `getByText(...)` and `locator('text=...')` patterns in added Playwright spec lines.
    These break on copy edits; use `data-testid` attributes (`getByTestId(...)`) instead.
    """
    diff = fetch_pr_diff(pr_context.repo, pr_context.number)
    in_spec_file = False
    hits: list[str] = []
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            path = line[6:]
            in_spec_file = path.endswith('.spec.ts') and ('end2end-ui/' in path or 'e2e/' in path)
            continue
        if not in_spec_file or not line.startswith('+') or line.startswith('+++'):
            continue
        if is_fragile_text_selector(line):
            hits.append(line[1:].strip()[:120])
    assert not hits, 'Fragile text-based selectors introduced:\n' + '\n'.join(f'  {h}' for h in hits)
