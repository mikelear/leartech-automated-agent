"""Tier 4 — AI-powered visual review of Playwright videos. The wow piece.

For each Playwright spec on the gcp run, this:
  1. Downloads the .webm to a temp path.
  2. Extracts N evenly-spaced PNG frames via ffmpeg.
  3. Sends frames + spec context to Claude with a `report_anomalies` tool.
  4. Asserts the verdict says no anomalies.

Cost-conscious by default — caps at 3 specs per gate run. Bump
`VIDEO_REVIEW_MAX_SPECS` env var to extend coverage; set to 0 to skip the criterion entirely.
Skips cleanly when prerequisites (ffmpeg + ANTHROPIC_API_KEY) are missing.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from gate.tools import (
    PlaywrightRun,
    UISurfaceDelta,
    check_prerequisites,
    compute_ui_surface_delta,
    download_artifact,
    extract_frames,
    fetch_pr_diff,
    read_playwright_runs,
    review_video,
)
from gate.tools.pr_context import PRContext

pytestmark = pytest.mark.playwright

DEFAULT_SPEC_CAP = 3


def _spec_cap() -> int:
    raw = os.environ.get('VIDEO_REVIEW_MAX_SPECS')
    if raw is None:
        return DEFAULT_SPEC_CAP
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_SPEC_CAP


def _name_fragments(delta: UISurfaceDelta) -> set[str]:
    """Extract lowercase name fragments worth matching against spec names.

    'app-profile' → 'profile'; 'profile-page' → 'profile-page' + 'profile';
    route '/users' → 'users'. Empty set when delta is empty.
    """
    fragments: set[str] = set()
    for selector in delta.new_component_selectors:
        # 'app-profile' → 'profile' (drop the Angular convention prefix)
        cleaned = selector.removeprefix('app-').lower()
        if cleaned:
            fragments.add(cleaned)
            fragments.add(selector.lower())
    for testid in delta.new_data_testids:
        # 'profile-page' → both the full testid and any single-word stems
        lowered = testid.lower()
        fragments.add(lowered)
        for piece in lowered.split('-'):
            if len(piece) > 3:  # skip very short stems like 'btn' / 'edit' (too noisy)
                fragments.add(piece)
    for route in delta.new_route_paths:
        cleaned = route.lstrip('/').lower()
        if cleaned:
            fragments.add(cleaned)
    return fragments


def _overlap_score(spec_name: str, fragments: set[str]) -> int:
    """How many name fragments appear in the spec name (case-insensitive)."""
    lowered = spec_name.lower()
    return sum(1 for fragment in fragments if fragment in lowered)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Parametrize test_video_visual_review over (cluster, spec_name) discovered at run time.

    When the diff introduces new UI surface, sort specs by name-overlap with that surface
    so the AI-drafted spec for `app-profile` (etc.) is reviewed even when capped at N=3.
    Falls back to alphabetical when there's no UI delta (spec-only / config-only PRs).
    """
    if 'spec_target' not in metafunc.fixturenames:
        return

    repo = metafunc.config.getoption('--repo')
    pr_number = metafunc.config.getoption('--pr')
    if not repo or not pr_number:
        # Will get skipped at fixture time via the pr_context guard.
        metafunc.parametrize('spec_target', [], ids=[])
        return

    try:
        runs = read_playwright_runs(repo, pr_number)
    except RuntimeError:
        runs = []

    # Compute UI surface delta to bias toward specs exercising newly-added surface.
    # On error or empty diff, fragments is empty and ordering falls back to alphabetical.
    fragments: set[str] = set()
    try:
        diff = fetch_pr_diff(repo, pr_number)
        fragments = _name_fragments(compute_ui_surface_delta(diff))
    except RuntimeError:
        pass

    cap = _spec_cap()
    targets: list[tuple[PlaywrightRun, str] | None] = []
    ids: list[str] = []
    for run in runs:
        if run.cluster != 'gcp':
            continue  # gcp-only by default — az often empty / failing for env reasons
        if not run.passed_all:
            continue
        # Sort: descending by overlap-with-new-surface, then alphabetical as tiebreaker.
        ordered = sorted(run.specs(), key=lambda s: (-_overlap_score(s, fragments), s))
        for spec in ordered[:cap]:
            targets.append((run, spec))
            ids.append(f'{run.cluster}/{spec}')

    if not targets:
        # Single placeholder so the criterion shows up with a clear skip reason in the report
        # instead of pytest's default `[NOTSET]` ID.
        metafunc.parametrize('spec_target', [None], ids=['no-eligible-runs'])
        return
    metafunc.parametrize('spec_target', targets, ids=ids)


@pytest.fixture(scope='session', autouse=True)
def _video_prerequisites() -> None:
    """Skip the whole video-review module if ffmpeg or ANTHROPIC_API_KEY is missing."""
    prereqs = check_prerequisites()
    if not prereqs.ok:
        pytest.skip('Video review prerequisites missing: ' + ', '.join(prereqs.missing()))


def test_video_visual_review(
    pr_context: PRContext,
    spec_target: tuple[PlaywrightRun, str] | None,
) -> None:
    """Claude visually inspects the spec's video; assert no anomalies found."""
    if spec_target is None:
        pytest.skip('No PASS Playwright runs on gcp — nothing to visually review')
    run, spec_name = spec_target
    artifact = run.artifact_for(spec_name, 'video')
    assert artifact is not None, f'No video artifact for {run.cluster}/{spec_name}'

    with tempfile.TemporaryDirectory() as tmp:
        video_path = Path(tmp) / f'{spec_name}.webm'
        download_artifact(artifact.url, video_path)
        frames = extract_frames(video_path)

    verdict = review_video(spec_name, frames)
    assert not verdict.anomalies_found, (
        f'[{run.cluster}] {spec_name} — {verdict.summary} (flagged frames: {list(verdict.flagged_frames)})'
    )
