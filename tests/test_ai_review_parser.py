"""Unit tests for gate.tools.ai_review.parse_ai_review_comment."""

from __future__ import annotations

from gate.tools.ai_review import parse_ai_review_comment

OK_GCP = """## :white_check_mark: AI Code Review: **95/100 — Excellent** `[gcp]`

**Scores:** 95 | 95 (avg: 95) | **3 reviewers**

> All reviewers passed. This PR is eligible for auto-merge.
"""

WARN_AZ = """## :warning: AI Code Review: **66/100 — Needs Work** `[az]`

**Scores:** -2 | 100 | 100 (avg: 66) | **3 reviewers**
"""

UNRELATED = '## Coverage report: 80%'


def test_parses_passing_verdict() -> None:
    v = parse_ai_review_comment(OK_GCP)
    assert v is not None
    assert v.cluster == 'gcp'
    assert v.emoji == 'white_check_mark'
    assert v.score == 95
    assert v.verdict == 'Excellent'
    assert v.auto_merge_eligible
    assert v.passed
    assert not v.blocking


def test_parses_warning_verdict() -> None:
    v = parse_ai_review_comment(WARN_AZ)
    assert v is not None
    assert v.cluster == 'az'
    assert v.emoji == 'warning'
    assert v.score == 66
    assert v.verdict == 'Needs Work'
    assert not v.auto_merge_eligible
    assert not v.passed
    assert not v.blocking


def test_returns_none_for_unrelated_comment() -> None:
    assert parse_ai_review_comment(UNRELATED) is None


def test_returns_none_for_empty_body() -> None:
    assert parse_ai_review_comment('') is None
