"""Unit tests for gate.tools.pr_context — exercises qualified_repo derivation +
commit-message fetch used by the chart-flip / overlay-PR criterion."""

from __future__ import annotations

import json

import pytest

from gate.tools import pr_context as pr_context_module
from gate.tools.pr_context import PRContext, fetch_pr_commit_messages


def _ctx(repo: str) -> PRContext:
    return PRContext(
        repo=repo,
        number=1,
        head_sha='deadbeef',
        base_sha='cafebabe',
        title='t',
        body='',
        changed_files=(),
        state='OPEN',
    )


def test_qualified_repo_passes_through_when_already_qualified() -> None:
    assert _ctx('mikelear/leartech-auth-ui').qualified_repo == 'mikelear/leartech-auth-ui'


def test_qualified_repo_defaults_owner_to_mikelear() -> None:
    assert _ctx('leartech-auth-ui').qualified_repo == 'mikelear/leartech-auth-ui'


# ---------------------------------------------------------------------------
# fetch_pr_commit_messages — every failure branch must fall back to '' cleanly
# ---------------------------------------------------------------------------


def test_fetch_commit_messages_concatenates_headline_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path — every commit contributes both messageHeadline and messageBody."""
    payload = json.dumps(
        {
            'commits': [
                {
                    'messageHeadline': 'feat(chart): add dcr',
                    'messageBody': 'Paired with mikelear/jx-build-cluster-gsm#1.',
                },
                {'messageHeadline': 'test: cover dcr toggle', 'messageBody': ''},
            ]
        }
    )
    monkeypatch.setattr(pr_context_module, '_gh', lambda args: payload)
    result = fetch_pr_commit_messages('mikelear/foo', 42)
    assert 'feat(chart): add dcr' in result
    assert 'Paired with mikelear/jx-build-cluster-gsm#1.' in result
    assert 'test: cover dcr toggle' in result


def test_fetch_commit_messages_returns_empty_when_gh_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_gh(args: list[str]) -> str:
        raise RuntimeError('gh pr view failed: 403')

    monkeypatch.setattr(pr_context_module, '_gh', raising_gh)
    assert fetch_pr_commit_messages('mikelear/foo', 42) == ''


def test_fetch_commit_messages_returns_empty_on_blank_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_context_module, '_gh', lambda args: '   \n')
    assert fetch_pr_commit_messages('mikelear/foo', 42) == ''


def test_fetch_commit_messages_returns_empty_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_context_module, '_gh', lambda args: 'not-json{{{')
    assert fetch_pr_commit_messages('mikelear/foo', 42) == ''


def test_fetch_commit_messages_returns_empty_when_commits_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pr_context_module, '_gh', lambda args: json.dumps({'other': 'shape'}))
    assert fetch_pr_commit_messages('mikelear/foo', 42) == ''


def test_fetch_commit_messages_handles_missing_headline_or_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty commits or ones missing either field don't blow up."""
    payload = json.dumps(
        {
            'commits': [
                {'messageHeadline': 'only headline'},
                {'messageBody': 'only body'},
                {},
                'not-a-dict',
            ]
        }
    )
    monkeypatch.setattr(pr_context_module, '_gh', lambda args: payload)
    result = fetch_pr_commit_messages('mikelear/foo', 42)
    assert 'only headline' in result
    assert 'only body' in result


def test_fetch_commit_messages_defaults_owner_to_mikelear(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unqualified repo names get the same `mikelear/` default as PRContext.qualified_repo."""
    captured: list[list[str]] = []

    def capturing_gh(args: list[str]) -> str:
        captured.append(list(args))
        return json.dumps({'commits': []})

    monkeypatch.setattr(pr_context_module, '_gh', capturing_gh)
    fetch_pr_commit_messages('leartech-automated-agent', 7)
    assert captured
    assert '-R' in captured[0]
    assert 'mikelear/leartech-automated-agent' in captured[0]
