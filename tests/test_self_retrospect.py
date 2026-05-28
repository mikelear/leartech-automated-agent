"""Tests for gate.agent.self_retrospect — the post-Ready retrospective.

These tests verify behaviour without ever hitting Anthropic or GitHub:

- LLM call is monkeypatched at the ``anthropic.Anthropic`` import site.
- ``gh`` shell-outs are monkeypatched at ``subprocess.run``.

Coverage:
  - parse: well-formed JSON, fenced JSON, embedded JSON, malformed
  - filter: low-priority dropped, empty list, invalid form rejected
  - trivial-PR skip via line-count heuristic
  - graceful degrade: LLM raises → []
  - issue body has expected structure
  - file_issue retries without labels on label-not-found
  - env-var disable hook in initiatives router
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from gate.agent import self_retrospect as sr
from gate.agent.self_retrospect import (
    Finding,
    _count_diff_lines,
    _parse_findings,
    _render_issue_body,
    file_issue_with_findings,
    retrospect_after_ready,
)

# ─── Diff line counting ──────────────────────────────────────────────────


def test_count_diff_lines_excludes_headers() -> None:
    diff = """diff --git a/foo.py b/foo.py
index abc..def 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 unchanged
-removed_line
+added_line_one
+added_line_two
 trailing
"""
    # 1 removal + 2 additions = 3
    assert _count_diff_lines(diff) == 3


def test_count_diff_lines_empty() -> None:
    assert _count_diff_lines('') == 0


def test_count_diff_lines_only_headers() -> None:
    assert _count_diff_lines('diff --git a/x b/x\nindex 1..2\n--- a/x\n+++ b/x\n@@ -0,0 +0,0 @@\n') == 0


# ─── Finding parser ──────────────────────────────────────────────────────


def test_parse_findings_well_formed() -> None:
    text = json.dumps(
        {
            'findings': [
                {
                    'title': 'Missing env var in Tekton',
                    'root_cause': 'No cross-repo check',
                    'proposed_fix': 'Add a criterion',
                    'suggested_form': 'criterion',
                    'priority': 'high',
                }
            ]
        }
    )
    findings = _parse_findings(text)
    assert len(findings) == 1
    assert findings[0].title == 'Missing env var in Tekton'
    assert findings[0].priority == 'high'


def test_parse_findings_fenced_json() -> None:
    text = '```json\n' + json.dumps({'findings': []}) + '\n```'
    assert _parse_findings(text) == []


def test_parse_findings_invalid_form_dropped() -> None:
    text = json.dumps(
        {
            'findings': [
                {
                    'title': 'bad form',
                    'root_cause': '',
                    'proposed_fix': '',
                    'suggested_form': 'random-form-not-in-enum',
                    'priority': 'high',
                },
                {
                    'title': 'good',
                    'root_cause': 'x',
                    'proposed_fix': 'y',
                    'suggested_form': 'lesson',
                    'priority': 'medium',
                },
            ]
        }
    )
    findings = _parse_findings(text)
    assert len(findings) == 1
    assert findings[0].title == 'good'


def test_parse_findings_invalid_priority_dropped() -> None:
    text = json.dumps(
        {
            'findings': [
                {
                    'title': 'bad',
                    'root_cause': '',
                    'proposed_fix': '',
                    'suggested_form': 'lesson',
                    'priority': 'critical',
                },
            ]
        }
    )
    assert _parse_findings(text) == []


def test_parse_findings_with_surrounding_prose() -> None:
    text = 'Here is the JSON:\n' + json.dumps(
        {
            'findings': [
                {
                    'title': 't',
                    'root_cause': 'rc',
                    'proposed_fix': 'pf',
                    'suggested_form': 'tekton-step',
                    'priority': 'low',
                }
            ]
        }
    )
    findings = _parse_findings(text)
    assert len(findings) == 1
    assert findings[0].suggested_form == 'tekton-step'


def test_parse_findings_malformed_json_raises() -> None:
    with pytest.raises(json.JSONDecodeError):
        _parse_findings('not json at all and no braces')


# ─── retrospect_after_ready: integration with mocked LLM ─────────────────


def _big_diff(lines: int = 20) -> str:
    body = '\n'.join(f'+added_line_{i}' for i in range(lines))
    return f'diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -0,0 +0,0 @@\n{body}\n'


async def test_retrospect_skips_trivial_pr() -> None:
    # 3-line diff is below threshold (10)
    diff = '@@ -0,0 +0,0 @@\n+a\n+b\n+c\n'
    findings = await retrospect_after_ready(
        pr_repo='org/repo',
        pr_number=1,
        pr_diff=diff,
        ai_review_verdict=None,
        final_gate_state='',
    )
    assert findings == []


async def test_retrospect_filters_low_priority() -> None:
    """LLM returns 3 findings (high/medium/low) → low is filtered out."""
    payload = {
        'findings': [
            {'title': 'h', 'root_cause': '', 'proposed_fix': '', 'suggested_form': 'lesson', 'priority': 'high'},
            {'title': 'm', 'root_cause': '', 'proposed_fix': '', 'suggested_form': 'criterion', 'priority': 'medium'},
            {'title': 'l', 'root_cause': '', 'proposed_fix': '', 'suggested_form': 'lesson', 'priority': 'low'},
        ]
    }
    text_block = MagicMock()
    text_block.text = json.dumps(payload)
    fake_response = MagicMock()
    fake_response.content = [text_block]

    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    with patch('anthropic.Anthropic', return_value=fake_client):
        findings = await retrospect_after_ready(
            pr_repo='org/repo',
            pr_number=42,
            pr_diff=_big_diff(),
            ai_review_verdict='AI review said OK',
            final_gate_state='all green',
        )

    titles = sorted(f.title for f in findings)
    assert titles == ['h', 'm']


async def test_retrospect_returns_empty_on_llm_failure() -> None:
    """Graceful degrade: LLM raising must produce [] without propagating."""

    class _BoomError(Exception):
        pass

    with patch('anthropic.Anthropic', side_effect=_BoomError('rate limit')):
        findings = await retrospect_after_ready(
            pr_repo='org/repo',
            pr_number=42,
            pr_diff=_big_diff(),
            ai_review_verdict=None,
            final_gate_state='',
        )
    assert findings == []


async def test_retrospect_returns_empty_on_parse_failure() -> None:
    """LLM returns garbage that doesn't contain JSON → return []."""
    text_block = MagicMock()
    text_block.text = 'no json here, sorry'
    fake_response = MagicMock()
    fake_response.content = [text_block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    with patch('anthropic.Anthropic', return_value=fake_client):
        findings = await retrospect_after_ready(
            pr_repo='org/repo',
            pr_number=42,
            pr_diff=_big_diff(),
            ai_review_verdict=None,
            final_gate_state='',
        )
    assert findings == []


async def test_retrospect_empty_findings_list_ok() -> None:
    """LLM honestly returns no findings → return []."""
    text_block = MagicMock()
    text_block.text = json.dumps({'findings': []})
    fake_response = MagicMock()
    fake_response.content = [text_block]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_response

    with patch('anthropic.Anthropic', return_value=fake_client):
        findings = await retrospect_after_ready(
            pr_repo='org/repo',
            pr_number=42,
            pr_diff=_big_diff(),
            ai_review_verdict=None,
            final_gate_state='',
        )
    assert findings == []


# ─── Issue body rendering ────────────────────────────────────────────────


def test_render_issue_body_contains_all_findings() -> None:
    findings = [
        Finding(
            title='Missing env var',
            root_cause='No cross-repo consistency check',
            proposed_fix='Add a criterion that diffs Tekton task env',
            suggested_form='criterion',
            priority='high',
        ),
        Finding(
            title='Lint should have caught unused var',
            root_cause='Pre-push lint not invoked',
            proposed_fix='Add ruff to pre-push lesson list',
            suggested_form='pre-push-check',
            priority='medium',
        ),
    ]
    body = _render_issue_body(pr_number=99, findings=findings)
    assert 'PR #99' in body
    assert 'Missing env var' in body
    assert 'Lint should have caught unused var' in body
    assert '`criterion`' in body
    assert '`pre-push-check`' in body
    assert 'self-retrospect-honesty' in body


# ─── file_issue_with_findings: subprocess interaction ────────────────────


async def test_file_issue_with_findings_empty_returns_none() -> None:
    assert await file_issue_with_findings(pr_repo='org/repo', pr_number=1, findings=[]) is None


async def test_file_issue_success_uses_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: gh succeeds → returns the URL from stdout."""
    captured_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        captured_calls.append(cmd)
        return cast(Any, MagicMock(returncode=0, stdout='https://github.com/org/repo/issues/77\n', stderr=''))

    monkeypatch.setattr(subprocess, 'run', fake_run)

    findings = [
        Finding(title='t', root_cause='r', proposed_fix='p', suggested_form='lesson', priority='high'),
    ]
    url = await file_issue_with_findings(pr_repo='org/repo', pr_number=42, findings=findings)
    assert url == 'https://github.com/org/repo/issues/77'
    # Verify label flags were included
    assert len(captured_calls) == 1
    cmd = captured_calls[0]
    assert '--label' in cmd
    assert 'self-retrospective' in cmd
    assert 'candidate/lesson' in cmd


async def test_file_issue_retries_without_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Label-not-found → first call fails, retry without labels succeeds."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        if '--label' in cmd:
            return cast(
                Any, MagicMock(returncode=1, stdout='', stderr='could not add label: self-retrospective not found')
            )
        return cast(Any, MagicMock(returncode=0, stdout='https://github.com/org/repo/issues/88', stderr=''))

    monkeypatch.setattr(subprocess, 'run', fake_run)

    findings = [
        Finding(title='t', root_cause='r', proposed_fix='p', suggested_form='lesson', priority='high'),
    ]
    url = await file_issue_with_findings(pr_repo='org/repo', pr_number=42, findings=findings)
    assert url == 'https://github.com/org/repo/issues/88'
    assert len(calls) == 2
    # Second call must NOT contain --label
    assert '--label' not in calls[1]


async def test_file_issue_returns_none_on_total_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both attempts fail → returns None without raising."""

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        return cast(Any, MagicMock(returncode=1, stdout='', stderr='auth failed'))

    monkeypatch.setattr(subprocess, 'run', fake_run)

    findings = [
        Finding(title='t', root_cause='r', proposed_fix='p', suggested_form='lesson', priority='high'),
    ]
    assert await file_issue_with_findings(pr_repo='org/repo', pr_number=42, findings=findings) is None


# ─── _run_self_retrospect: env-var disable hook ──────────────────────────


async def test_env_var_disables_retrospect(monkeypatch: pytest.MonkeyPatch) -> None:
    """When LEARTECH_AGENT_SELF_RETROSPECT=false, the hook is a no-op."""
    from app.routers.initiatives import _run_self_retrospect

    monkeypatch.setenv('LEARTECH_AGENT_SELF_RETROSPECT', 'false')

    # If the env-var check fails, the next thing we'd hit is get_record() — we
    # confirm it was NOT called by patching it to raise.
    with patch('app.routers.initiatives.get_record', side_effect=AssertionError('must not be called')):
        await _run_self_retrospect('any-id')


async def test_retrospect_skipped_when_pr_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pr_number on the record → hook bails before calling the LLM."""
    from app.routers.initiatives import _run_self_retrospect
    from app.state import InitiativeRecord, now

    monkeypatch.setenv('LEARTECH_AGENT_SELF_RETROSPECT', 'true')

    record = InitiativeRecord(
        id='abc',
        initiative='x',
        status='complete',
        started_at=now(),
        pr_repo='org/repo',
        pr_number=None,  # not resolved
    )
    with patch('app.routers.initiatives.get_record', return_value=record):
        with patch('app.routers.initiatives.retrospect_after_ready', side_effect=AssertionError('must not be called')):
            await _run_self_retrospect('abc')


def test_module_constants_sensible() -> None:
    """Sanity: cost/perf-relevant constants are within documented ranges."""
    assert sr.MIN_DIFF_LINES_FOR_RETROSPECT >= 1
    assert sr.MAX_DIFF_CHARS > 1000
