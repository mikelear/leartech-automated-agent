"""Unit tests for gate.tools.playwright_artifacts.parse_playwright_sticky_comment."""

from __future__ import annotations

from gate.tools.playwright_artifacts import parse_playwright_sticky_comment

GCP_OK = """<!-- leartech-end2end-ui-gcp -->
:white_check_mark: **End-to-end UI: PASS** `[gcp]` — 9/9 browser tests passed

Preview: https://leartech-auth-ui-pr25.jx.leartech.com

**Artifacts** (screenshots, videos, traces):

- :camera: [01-page-loads-page-loads-app-root-element-renders screenshot](https://storage.googleapis.com/test-artifacts-product-first/leartech-auth-ui/pr-25/gcp/1777378209/01-page-loads-page-loads-app-root-element-renders--test-finished-1.png)
- :movie_camera: [01-page-loads-page-loads-app-root-element-renders video](https://storage.googleapis.com/test-artifacts-product-first/leartech-auth-ui/pr-25/gcp/1777378209/01-page-loads-page-loads-app-root-element-renders--video.webm)
- :mag: [01-page-loads-page-loads-app-root-element-renders trace](https://storage.googleapis.com/test-artifacts-product-first/leartech-auth-ui/pr-25/gcp/1777378209/01-page-loads-page-loads-app-root-element-renders--trace.zip)
- :camera: [02-login-form-login-form-login-page-renders-form-or-content screenshot](https://storage.googleapis.com/test-artifacts-product-first/leartech-auth-ui/pr-25/gcp/1777378209/02-login-form-login-form-login-page-renders-form-or-content--test-finished-1.png)
"""

AZ_GATE_TIMEOUT = """<!-- leartech-end2end-ui-az -->
:x: **End-to-end UI: FAIL (preview gate timeout)** `[az]`

preview-gate never reported ready within 10 min.
"""

UNRELATED = '## AI Code Review: 100/100'


def test_parses_passing_run_with_artifacts() -> None:
    run = parse_playwright_sticky_comment(GCP_OK)
    assert run is not None
    assert run.cluster == 'gcp'
    assert run.passed_all
    assert run.passed == 9
    assert run.total == 9
    assert run.verdict == 'PASS'
    assert len(run.artifacts) == 4
    spec_names = run.specs()
    assert '01-page-loads-page-loads-app-root-element-renders' in spec_names
    assert '02-login-form-login-form-login-page-renders-form-or-content' in spec_names

    video = run.artifact_for('01-page-loads-page-loads-app-root-element-renders', 'video')
    assert video is not None
    assert video.url.endswith('--video.webm')
    assert video.cluster == 'gcp'


def test_parses_failing_run_without_artifacts() -> None:
    run = parse_playwright_sticky_comment(AZ_GATE_TIMEOUT)
    assert run is not None
    assert run.cluster == 'az'
    assert not run.passed_all
    assert run.emoji == 'x'
    assert run.verdict.startswith('FAIL')
    assert run.passed == 0
    assert run.total == 0
    assert run.artifacts == ()


def test_returns_none_for_non_end2end_ui_comment() -> None:
    assert parse_playwright_sticky_comment(UNRELATED) is None
    assert parse_playwright_sticky_comment('') is None


def test_artifact_for_returns_none_when_missing() -> None:
    run = parse_playwright_sticky_comment(GCP_OK)
    assert run is not None
    # Spec 02 in the fixture only has screenshot, no video.
    assert run.artifact_for('02-login-form-login-form-login-page-renders-form-or-content', 'video') is None
