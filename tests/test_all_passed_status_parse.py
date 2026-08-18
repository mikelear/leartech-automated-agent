"""The all_passed flag decides the run's verdict, so it reads the status field exactly.

Two of its three consumers turn a failure into a success: the verdict gate stops
recording "PR opened but never green" as a failure, and the exit-code normalisation
downgrades exit 1/2 to 0 so Kubernetes does not retry. Only the early-stop consumer is
additionally guarded by the agent having made no tool call that turn.

The previous implementation asked `'all_passed' in text` over a stringified payload. That
was correct only because `all_passed` happens to be a status VALUE on the Go side and
never a field name — a property owned by another repo, alongside a free-text
`coverage_note` in the same payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gate.agent.initiative import _tool_result_reports_all_passed


@dataclass
class FakeResult:
    """Stands in for claude_agent_sdk's ToolResultBlock: content is a str or a parts list."""

    content: Any
    is_error: bool = False


ALL_PASSED = '{"status": "all_passed", "checks": [], "clusters_observed": ["az"], "merged": true}'
SOME_FAILED = '{"status": "some_failed", "checks": [], "clusters_observed": ["az"]}'


def test_all_passed_status_is_reported() -> None:
    assert _tool_result_reports_all_passed(FakeResult(ALL_PASSED)) is True


def test_some_failed_status_is_not_reported() -> None:
    assert _tool_result_reports_all_passed(FakeResult(SOME_FAILED)) is False


def test_a_future_all_passed_field_does_not_flip_a_failure_to_success() -> None:
    """The case the substring match would get wrong.

    If the Go payload ever gains a boolean field of this name — the natural way to express
    the same fact — a substring match reads every some_failed result as green, and the
    exit-code normalisation then downgrades a real failure to 0.
    """
    payload = '{"status": "some_failed", "all_passed": false, "checks": []}'
    assert _tool_result_reports_all_passed(FakeResult(payload)) is False


def test_coverage_note_mentioning_the_token_does_not_flip_a_failure(caplog: Any) -> None:
    """coverage_note is free text in the same payload, written by another repo."""
    payload = '{"status": "timeout", "coverage_note": "no cluster reported all_passed", "checks": []}'
    assert _tool_result_reports_all_passed(FakeResult(payload)) is False


def test_content_as_parts_list_is_joined_and_parsed() -> None:
    assert _tool_result_reports_all_passed(FakeResult([{'type': 'text', 'text': ALL_PASSED}])) is True
    assert _tool_result_reports_all_passed(FakeResult([{'type': 'text', 'text': SOME_FAILED}])) is False


def test_error_result_is_never_all_passed() -> None:
    assert _tool_result_reports_all_passed(FakeResult(ALL_PASSED, is_error=True)) is False


def test_missing_content_is_never_all_passed() -> None:
    assert _tool_result_reports_all_passed(FakeResult(None)) is False


def test_unparseable_payload_falls_back_and_says_so(caplog: Any) -> None:
    """A shape we cannot parse keeps the old behaviour rather than silently reporting False,
    but logs, so the fallback is visible instead of becoming the norm unnoticed."""
    with caplog.at_level('WARNING'):
        assert _tool_result_reports_all_passed(FakeResult('status=all_passed (not json)')) is True
    assert any('fell back to a substring match' in r.message for r in caplog.records)
