"""Tests for Phase E.1 — language-aware image routing for Job-per-run.

E.1 makes the spawn pick a per-language ``leartech-agent-<lang>`` image
instead of the single ``leartech-agent-runtime`` everyone shares today.
Detection precedence (highest to lowest):

1. ``image_override`` (E.3) — exact image ref wins outright.
2. ``language`` arg (E.2) — explicit hint from the initiative YAML.
3. Repo manifest auto-detect (E.1) — sniff the primary repo's root.
4. ``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` env (D.4.4 default).

These tests cover layers 2 + 3, plus the URL-composition contract that
relies on the chart-rendered ``LEARTECH_JOB_IMAGE_REGISTRY_PREFIX`` and
``LEARTECH_JOB_IMAGE_TAG`` env vars. Layer 1 / Layer 4 are covered by
``test_initiatives_e3_image.py``.

GitHub API access is mocked at the ``_gh_api_list_repo_root`` boundary so
these tests run without a live ``gh`` CLI or network — the mock returns
a list of filenames as if from the GitHub Contents API.
"""

from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from app.routers import initiatives as initiatives_mod
from app.routers.initiatives import (
    _compose_image_url,
    _detect_language_from_repo,
    _image_for_language,
    _pick_image_for_initiative,
)


@pytest.fixture(autouse=True)
def _clear_language_cache() -> None:
    """Wipe :data:`_LANGUAGE_CACHE` before each test so prior cases don't
    leak detection results into later ones. The cache is module-level by
    design (cheap across POSTs); tests need it to start empty."""
    initiatives_mod._LANGUAGE_CACHE.clear()
    yield
    initiatives_mod._LANGUAGE_CACHE.clear()


@pytest.fixture
def routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the chart-rendered env vars that compose the per-language URL."""
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', 'ghcr.io/leartech-org')
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_TAG', '1.2.3')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/leartech-org/leartech-agent-runtime:1.2.3')


# ---------------------------------------------------------------------------
# _image_for_language — language hint → short image name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('language', 'expected'),
    [
        ('go', 'leartech-agent-go'),
        ('python', 'leartech-agent-python'),
        ('angular', 'leartech-agent-angular'),
        ('node', 'leartech-agent-node'),
        ('rust', 'leartech-agent-rust'),
        ('dotnet', 'leartech-agent-dotnet'),
    ],
)
def test_image_for_language_known_values(language: str, expected: str) -> None:
    """Every supported language maps to the canonical short image name."""
    assert _image_for_language(language) == expected


def test_image_for_language_is_case_insensitive() -> None:
    """YAML authors sometimes write ``Python`` or ``GO`` — accept those too."""
    assert _image_for_language('Python') == 'leartech-agent-python'
    assert _image_for_language('GO') == 'leartech-agent-go'
    assert _image_for_language('  rust  ') == 'leartech-agent-rust'


def test_image_for_language_unknown_returns_none() -> None:
    """Unknown languages return None so the caller can fall back to default."""
    assert _image_for_language('kotlin') is None
    assert _image_for_language('elixir') is None


def test_image_for_language_empty_returns_none() -> None:
    """Empty / whitespace-only strings are treated as no hint, not a key."""
    assert _image_for_language('') is None
    assert _image_for_language('   ') is None


# ---------------------------------------------------------------------------
# _detect_language_from_repo — manifest sniffing + caching
# ---------------------------------------------------------------------------


def test_detect_language_go_mod_returns_go() -> None:
    """A repo with go.mod at the root → 'go'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['go.mod', 'README.md', 'main.go']):
        assert _detect_language_from_repo('mikelear/some-go-svc') == 'go'


def test_detect_language_pyproject_returns_python() -> None:
    """A repo with pyproject.toml → 'python'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['pyproject.toml', 'README.md', 'app']):
        assert _detect_language_from_repo('mikelear/some-python-svc') == 'python'


def test_detect_language_requirements_txt_returns_python() -> None:
    """A pre-pyproject Python repo with requirements.txt → 'python'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['requirements.txt', 'README.md', 'src']):
        assert _detect_language_from_repo('mikelear/legacy-python-svc') == 'python'


def test_detect_language_angular_repo() -> None:
    """A repo with both package.json AND angular.json → 'angular'."""
    with patch.object(
        initiatives_mod,
        '_gh_api_list_repo_root',
        return_value=['package.json', 'angular.json', 'tsconfig.json'],
    ):
        assert _detect_language_from_repo('mikelear/some-ui') == 'angular'


def test_detect_language_node_repo() -> None:
    """A repo with only package.json (no angular.json) → 'node'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['package.json', 'README.md']):
        assert _detect_language_from_repo('mikelear/some-node-svc') == 'node'


def test_detect_language_rust_repo() -> None:
    """A repo with Cargo.toml → 'rust'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['Cargo.toml', 'README.md', 'src']):
        assert _detect_language_from_repo('mikelear/some-rust-svc') == 'rust'


def test_detect_language_dotnet_repo() -> None:
    """A repo with any *.csproj file → 'dotnet'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['MyService.csproj', 'README.md']):
        assert _detect_language_from_repo('mikelear/some-dotnet-svc') == 'dotnet'


def test_detect_language_priority_go_beats_python() -> None:
    """Priority order is fixed: go.mod wins over pyproject.toml when both
    are present (rare but possible — polyglot repos / scripts directories)."""
    with patch.object(
        initiatives_mod,
        '_gh_api_list_repo_root',
        return_value=['go.mod', 'pyproject.toml', 'package.json', 'Cargo.toml'],
    ):
        assert _detect_language_from_repo('mikelear/polyglot') == 'go'


def test_detect_language_priority_python_beats_node() -> None:
    """pyproject.toml wins over package.json (e.g. a Python service with
    a build-tool npm helper)."""
    with patch.object(
        initiatives_mod,
        '_gh_api_list_repo_root',
        return_value=['pyproject.toml', 'package.json'],
    ):
        assert _detect_language_from_repo('mikelear/python-with-npm') == 'python'


def test_detect_language_empty_repo_returns_none() -> None:
    """Empty root listing → None. Caller falls back to default image."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=[]):
        assert _detect_language_from_repo('mikelear/empty-or-unknown') is None


def test_detect_language_unrecognised_manifests_returns_none() -> None:
    """README + LICENSE + arbitrary files, no recognised manifest → None."""
    with patch.object(
        initiatives_mod,
        '_gh_api_list_repo_root',
        return_value=['README.md', 'LICENSE', 'CHANGELOG.md', 'NOTES.txt'],
    ):
        assert _detect_language_from_repo('mikelear/docs-only') is None


def test_detect_language_api_failure_returns_none_and_does_not_cache() -> None:
    """When the gh API call fails (mocked _gh_api_list_repo_root returns None),
    detection returns None AND the negative is NOT cached — a follow-up call
    must retry. This is the contract for transient failures: retry, don't
    stick on a stale 'unknown'."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=None) as mocked:
        assert _detect_language_from_repo('mikelear/transient-fail') is None
        assert _detect_language_from_repo('mikelear/transient-fail') is None
        # Both calls hit the underlying API — no caching of the failure.
        assert mocked.call_count == 2
    assert 'mikelear/transient-fail' not in initiatives_mod._LANGUAGE_CACHE


def test_detect_language_cache_hit_avoids_refetch() -> None:
    """Successful detections are cached so repeated POSTs for the same repo
    don't re-hit the GitHub API. The cache lives for the process lifetime."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['go.mod']) as mocked:
        first = _detect_language_from_repo('mikelear/some-go-svc')
        second = _detect_language_from_repo('mikelear/some-go-svc')
        third = _detect_language_from_repo('mikelear/some-go-svc')
    assert first == second == third == 'go'
    assert mocked.call_count == 1


def test_detect_language_negative_result_is_cached() -> None:
    """Confirmed negatives (API succeeded, no recognised manifest) are
    cached too — there's no reason to re-fetch a docs-only repo."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['README.md']) as mocked:
        assert _detect_language_from_repo('mikelear/docs-only') is None
        assert _detect_language_from_repo('mikelear/docs-only') is None
    assert mocked.call_count == 1
    assert initiatives_mod._LANGUAGE_CACHE['mikelear/docs-only'] is None


# ---------------------------------------------------------------------------
# _gh_api_list_repo_root — subprocess boundary
# ---------------------------------------------------------------------------


def _fake_completed(stdout: str = '', stderr: str = '', returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a CompletedProcess that ``subprocess.run`` would return."""
    return subprocess.CompletedProcess(args=['gh'], returncode=returncode, stdout=stdout, stderr=stderr)


def test_gh_api_list_repo_root_parses_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: subprocess.run returns a JSON array; helper parses it."""

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(stdout='["go.mod","README.md"]\n')

    monkeypatch.setattr(initiatives_mod.subprocess, 'run', fake_run)
    result = initiatives_mod._gh_api_list_repo_root('mikelear/some-go-svc')
    assert result == ['go.mod', 'README.md']


def test_gh_api_list_repo_root_nonzero_exit_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero exit (auth fail, repo not found, rate limit) → None so the
    caller falls back. We log a warning; we do NOT raise."""

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(returncode=1, stderr='HTTP 404: Not Found\n')

    monkeypatch.setattr(initiatives_mod.subprocess, 'run', fake_run)
    assert initiatives_mod._gh_api_list_repo_root('mikelear/missing') is None


def test_gh_api_list_repo_root_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess timeout → None (don't crash the spawn path on a slow API)."""

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd='gh', timeout=15)

    monkeypatch.setattr(initiatives_mod.subprocess, 'run', fake_run)
    assert initiatives_mod._gh_api_list_repo_root('mikelear/slow') is None


def test_gh_api_list_repo_root_invalid_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed JSON on stdout → None. We never want a JSONDecodeError to
    escape into the FastAPI response."""

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _fake_completed(stdout='not json at all\n')

    monkeypatch.setattr(initiatives_mod.subprocess, 'run', fake_run)
    assert initiatives_mod._gh_api_list_repo_root('mikelear/broken') is None


# ---------------------------------------------------------------------------
# _compose_image_url — chart-rendered env composition
# ---------------------------------------------------------------------------


def test_compose_image_url_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefix + short name + tag compose to the canonical URL shape."""
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', 'ghcr.io/leartech-org')
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_TAG', '1.2.3')
    assert _compose_image_url('leartech-agent-go') == 'ghcr.io/leartech-org/leartech-agent-go:1.2.3'


def test_compose_image_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: an operator-set prefix ending in ``/`` doesn't double-slash."""
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', 'ghcr.io/leartech-org/')
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_TAG', '1.2.3')
    assert _compose_image_url('leartech-agent-python') == 'ghcr.io/leartech-org/leartech-agent-python:1.2.3'


def test_compose_image_url_returns_none_when_prefix_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollout-gap guard: old chart, new code → caller falls back to default."""
    monkeypatch.delenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', raising=False)
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_TAG', '1.2.3')
    assert _compose_image_url('leartech-agent-go') is None


def test_compose_image_url_returns_none_when_tag_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rollout-gap guard for the tag — fall back, don't raise."""
    monkeypatch.setenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', 'ghcr.io/leartech-org')
    monkeypatch.delenv('LEARTECH_JOB_IMAGE_TAG', raising=False)
    assert _compose_image_url('leartech-agent-go') is None


# ---------------------------------------------------------------------------
# _pick_image_for_initiative — end-to-end routing precedence
# ---------------------------------------------------------------------------


def test_pick_image_explicit_language_python_bypasses_detection(
    routing_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An initiative with ``language: 'python'`` returns the python image
    without ever sniffing the repo. The detection helper must NOT be called."""

    def boom(*_args: Any, **_kwargs: Any) -> list[str] | None:
        raise AssertionError('detection should not run when language is explicit')

    monkeypatch.setattr(initiatives_mod, '_gh_api_list_repo_root', boom)
    image = _pick_image_for_initiative(
        'any-name',
        language='python',
        qualified_repo='mikelear/some-go-svc',
    )
    assert image == 'ghcr.io/leartech-org/leartech-agent-python:1.2.3'


def test_pick_image_routes_to_go_for_a_go_repo(routing_env: None) -> None:
    """End-to-end: a Go primary repo with no explicit language hint resolves
    via manifest auto-detection to the per-language Go image."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['go.mod', 'README.md']):
        image = _pick_image_for_initiative(
            'any-name',
            qualified_repo='mikelear/some-go-svc',
        )
    assert image == 'ghcr.io/leartech-org/leartech-agent-go:1.2.3'


def test_pick_image_routes_to_angular_for_an_angular_repo(routing_env: None) -> None:
    """End-to-end: package.json + angular.json → angular image URL."""
    with patch.object(
        initiatives_mod,
        '_gh_api_list_repo_root',
        return_value=['package.json', 'angular.json', 'tsconfig.json'],
    ):
        image = _pick_image_for_initiative(
            'any-name',
            qualified_repo='mikelear/leartech-auth-ui',
        )
    assert image == 'ghcr.io/leartech-org/leartech-agent-angular:1.2.3'


def test_pick_image_unknown_repo_falls_back_to_env_default(routing_env: None) -> None:
    """A repo whose root has no recognised manifest → default image URL.
    No raise, no 500 — the runtime image is the fleet-wide fallback."""
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['README.md']):
        image = _pick_image_for_initiative(
            'any-name',
            qualified_repo='mikelear/docs-only',
        )
    assert image == 'ghcr.io/leartech-org/leartech-agent-runtime:1.2.3'


def test_pick_image_override_wins_over_language_and_repo_detect(
    routing_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.3: image_override short-circuits before detection AND language
    routing. Neither the GH API helper nor the language map is consulted."""

    def boom(*_args: Any, **_kwargs: Any) -> list[str] | None:
        raise AssertionError('detection must not run when image_override is set')

    monkeypatch.setattr(initiatives_mod, '_gh_api_list_repo_root', boom)
    image = _pick_image_for_initiative(
        'any-name',
        language='angular',
        image_override='ghcr.io/foo/experimental:abc123',
        qualified_repo='mikelear/some-go-svc',
    )
    assert image == 'ghcr.io/foo/experimental:abc123'


def test_pick_image_explicit_language_wins_over_repo_autodetect(
    routing_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.2 over E.1: an explicit ``language='node'`` beats whatever the repo
    sniffer would have returned. Detection is skipped entirely."""

    def boom(*_args: Any, **_kwargs: Any) -> list[str] | None:
        raise AssertionError('detection must not run when language is explicit')

    monkeypatch.setattr(initiatives_mod, '_gh_api_list_repo_root', boom)
    image = _pick_image_for_initiative(
        'any-name',
        language='node',
        qualified_repo='mikelear/some-go-svc',
    )
    assert image == 'ghcr.io/leartech-org/leartech-agent-node:1.2.3'


def test_pick_image_unknown_language_falls_back_to_default(routing_env: None) -> None:
    """An unrecognised language hint (e.g. 'kotlin') falls back to the
    default image rather than composing a URL for an image that doesn't
    exist. The agent fleet only ships images we list in _LANGUAGE_TO_IMAGE."""
    image = _pick_image_for_initiative('any-name', language='kotlin')
    assert image == 'ghcr.io/leartech-org/leartech-agent-runtime:1.2.3'


def test_pick_image_chart_rollout_gap_degrades_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the new chart env vars (LEARTECH_JOB_IMAGE_REGISTRY_PREFIX /
    LEARTECH_JOB_IMAGE_TAG) aren't rendered yet — running new code against
    an older chart release — the picker degrades to
    LEARTECH_INITIATIVE_DEFAULT_IMAGE instead of raising. This is the
    rollout-gap guard."""
    monkeypatch.delenv('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX', raising=False)
    monkeypatch.delenv('LEARTECH_JOB_IMAGE_TAG', raising=False)
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/leartech-org/leartech-agent-runtime:0.0.99')
    with patch.object(initiatives_mod, '_gh_api_list_repo_root', return_value=['go.mod']):
        image = _pick_image_for_initiative(
            'any-name',
            qualified_repo='mikelear/some-go-svc',
        )
    assert image == 'ghcr.io/leartech-org/leartech-agent-runtime:0.0.99'


def test_pick_image_no_qualified_repo_no_language_falls_back(routing_env: None) -> None:
    """When neither qualified_repo nor language is provided (defensive: callers
    that bypass the loader), the picker goes straight to the env default —
    no detection attempt, no spurious gh API calls."""
    image = _pick_image_for_initiative('any-name')
    assert image == 'ghcr.io/leartech-org/leartech-agent-runtime:1.2.3'
