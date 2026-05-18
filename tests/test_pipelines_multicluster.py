"""Multi-cluster failure-mode tests for `list_pr_checks` and friends.

The existing `test_pipelines.py` covers parser-layer behaviour against synthetic
inputs. This file covers the *orchestrating* `list_pr_checks` function against
the realistic-but-degraded scenarios we hit in production during the
2026-05-17/18 multi-cluster asymmetry session:

- One cluster's pipelines never trigger (GCP foghorn selective drop on PR #66)
- Lighthouse Merge Status / ai-review mixed in with Tekton checks
- gh CLI command itself fails (auth, rate limit, network)
- All-pending state during the period before any check terminates
- Cluster filter parameter (`cluster='gcp'|'az'|'both'`)

We stub `_gh` via monkeypatch — keeps tests offline and deterministic.
"""

from __future__ import annotations

import json
from typing import Callable

import pytest

import gate.tools.pipelines as pipelines_mod
from gate.tools.pipelines import list_pr_checks


def _stub_gh(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    """Replace `_gh` with one that returns a fixed JSON-serialised payload."""

    def fake_gh(args: list[str]) -> str:
        return json.dumps(payload)

    monkeypatch.setattr(pipelines_mod, '_gh', fake_gh)


def _stub_gh_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Replace `_gh` with one that raises (simulates auth failure, network, etc.)."""

    def fake_gh(args: list[str]) -> str:
        raise exc

    monkeypatch.setattr(pipelines_mod, '_gh', fake_gh)


# ─── Happy path ───────────────────────────────────────────────────────────


def test_returns_checks_from_both_clusters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default filter (cluster='both') yields checks from both gcp and az."""
    _stub_gh(monkeypatch, [
        {'context': 'gcp/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
        {'context': 'az/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/r-2'},
    ])
    checks = list_pr_checks('webcoder-ui', 11)
    clusters = sorted(c.cluster for c in checks)
    assert clusters == ['az', 'gcp']


def test_cluster_filter_gcp_excludes_az(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh(monkeypatch, [
        {'context': 'gcp/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
        {'context': 'az/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/r-2'},
    ])
    checks = list_pr_checks('webcoder-ui', 11, cluster='gcp')
    assert {c.cluster for c in checks} == {'gcp'}
    assert {c.check for c in checks} == {'lint'}


def test_cluster_filter_az_excludes_gcp(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh(monkeypatch, [
        {'context': 'gcp/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
        {'context': 'az/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/r-2'},
    ])
    checks = list_pr_checks('webcoder-ui', 11, cluster='az')
    assert {c.cluster for c in checks} == {'az'}


# ─── Selective-drop scenarios (today's GCP foghorn issue) ─────────────────


def test_only_one_cluster_triggered(monkeypatch: pytest.MonkeyPatch) -> None:
    """GCP foghorn missed PR #66's webhook initially — only az/* checks existed.

    `list_pr_checks(cluster='gcp')` should return [] gracefully, not error.
    """
    _stub_gh(monkeypatch, [
        {'context': 'az/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
        {'context': 'az/test', 'state': 'FAILURE',
         'targetUrl': 'https://tekton-dashboard-jx.az.leartech.com/#/namespaces/jx/pipelineruns/r-2'},
    ])
    assert list_pr_checks('webcoder-ui', 11, cluster='gcp') == []
    az_checks = list_pr_checks('webcoder-ui', 11, cluster='az')
    assert len(az_checks) == 2


def test_no_pipelines_yet_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh PR — Tekton hasn't spawned anything. gh returns just Lighthouse meta."""
    _stub_gh(monkeypatch, [
        {'context': 'Lighthouse Merge Status', 'state': 'PENDING', 'targetUrl': ''},
    ])
    assert list_pr_checks('webcoder-ui', 99) == []


# ─── Non-Tekton mixed in (Lighthouse / ai-review / GitHub Actions) ───────


def test_lighthouse_meta_check_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lighthouse Merge Status has empty targetUrl — must not appear in results."""
    _stub_gh(monkeypatch, [
        {'context': 'Lighthouse Merge Status', 'state': 'PENDING', 'targetUrl': ''},
        {'context': 'gcp/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
    ])
    checks = list_pr_checks('webcoder-ui', 11)
    assert {c.check for c in checks} == {'lint'}
    assert all(c.cluster in ('gcp', 'az') for c in checks)


def test_unrecognised_target_url_is_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Tekton URL (e.g. GitHub Actions, BuildKite) must not pollute the list."""
    _stub_gh(monkeypatch, [
        {'context': 'github-actions/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://github.com/mikelear/webcoder-ui/actions/runs/123'},
        {'context': 'gcp/lint', 'state': 'SUCCESS',
         'targetUrl': 'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/r-1'},
    ])
    checks = list_pr_checks('webcoder-ui', 11)
    assert len(checks) == 1
    assert checks[0].cluster == 'gcp'


# ─── gh CLI failures ──────────────────────────────────────────────────────


def test_gh_command_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """If `gh` itself fails (auth, network, rate limit), don't swallow it.

    The caller should see the error so they can decide whether to retry, fail
    the run, or post a diagnostic. Silently returning [] would look identical
    to 'no checks yet' and hide real outages.
    """
    _stub_gh_raises(monkeypatch, RuntimeError('gh pr view failed: HTTP 403: rate limit exceeded'))
    with pytest.raises(RuntimeError, match='rate limit'):
        list_pr_checks('webcoder-ui', 11)


def test_empty_rollup_returns_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_gh(monkeypatch, [])
    assert list_pr_checks('webcoder-ui', 11) == []


# ─── Repo-qualifier shorthand ─────────────────────────────────────────────


def test_repo_without_owner_qualified_to_mikelear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passing just `webcoder-ui` (no owner) gets prefixed with `mikelear/`.

    Catches the case where an agent passes the short name and we want the
    qualified one in the gh call.
    """
    captured_args: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        captured_args.append(args)
        return '[]'

    monkeypatch.setattr(pipelines_mod, '_gh', fake_gh)
    list_pr_checks('webcoder-ui', 11)
    assert any('mikelear/webcoder-ui' in str(a) for a in captured_args[0])


def test_repo_with_owner_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """`other-org/webcoder-ui` must NOT be re-prefixed with `mikelear/`."""
    captured_args: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        captured_args.append(args)
        return '[]'

    monkeypatch.setattr(pipelines_mod, '_gh', fake_gh)
    list_pr_checks('other-org/webcoder-ui', 11)
    full_args = captured_args[0]
    assert 'other-org/webcoder-ui' in full_args
    assert not any('mikelear/other-org' in str(a) for a in full_args)
