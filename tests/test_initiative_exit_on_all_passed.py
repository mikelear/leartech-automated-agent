"""Unit tests for the post-green hard-stop safety net in gate.agent.initiative.

fix-agent-exit-on-mcp-success. The PRIMARY lever is the system prompt telling
the LLM to STOP once `wait_for_terminal` returns `all_passed` (pinned in
``test_initiative_prompt_hold_option.py``). These tests cover the belt-and-braces
programmatic backstop: the two pure helpers that let the SDK loop recognise a
terminal `all_passed` result so it can break the loop once the agent emits its
final no-tool summary turn (rather than lingering — the "agent outlives merged
PR" overrun, worsened by SDK issue #913 not cleanly terminating the session).

We test the pure helpers directly; the loop wiring that consumes them lives in
``run_initiative`` (needs an ANTHROPIC_API_KEY + a real SDK stream, out of scope
for a unit test).
"""

from __future__ import annotations

from claude_agent_sdk.types import ToolResultBlock

from gate.agent.initiative import (
    _tool_name_is_wait_for_terminal,
    _tool_result_reports_all_passed,
)


def test_wait_for_terminal_name_matches_qualified_and_bare() -> None:
    """Match on the unqualified suffix so an MCP namespace rename can't silently
    disable the hard-stop."""
    assert _tool_name_is_wait_for_terminal('mcp__leartech-jx3-flow__wait_for_terminal')
    assert _tool_name_is_wait_for_terminal('wait_for_terminal')
    # A future prefix rename still matches on the suffix.
    assert _tool_name_is_wait_for_terminal('mcp__some-other-ns__wait_for_terminal')


def test_wait_for_terminal_name_excludes_fail_fast_and_others() -> None:
    """The in-loop fail-fast primitive can legitimately return all_passed mid-loop
    before the final-pass verification, so it must NOT be treated as the completion
    signal. Only the FULL-terminal check is."""
    assert not _tool_name_is_wait_for_terminal('mcp__leartech-jx3-flow__wait_for_first_failure_or_all_pass')
    assert not _tool_name_is_wait_for_terminal('mcp__leartech-jx3-flow__list_pr_checks')
    assert not _tool_name_is_wait_for_terminal('Bash')


def _result_block(content: object, *, is_error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id='tu_1', content=content, is_error=is_error)


def test_all_passed_detected_in_string_content() -> None:
    assert _tool_result_reports_all_passed(_result_block('{"status": "all_passed", "checks": []}'))


def test_all_passed_detected_in_list_content() -> None:
    """The SDK may surface content as a list of {"text": …} parts."""
    assert _tool_result_reports_all_passed(_result_block([{'type': 'text', 'text': '{"status": "all_passed"}'}]))
    # Plain-string list entries are also handled.
    assert _tool_result_reports_all_passed(_result_block(['status: all_passed']))


def test_some_failed_and_timeout_are_not_all_passed() -> None:
    assert not _tool_result_reports_all_passed(_result_block('{"status": "some_failed"}'))
    assert not _tool_result_reports_all_passed(_result_block('{"status": "timeout"}'))


def test_error_result_is_never_all_passed() -> None:
    """A tool error must not be mistaken for a green terminal, even if the error text
    happened to contain the token."""
    assert not _tool_result_reports_all_passed(_result_block('all_passed', is_error=True))


def test_none_and_empty_content_are_safe() -> None:
    assert not _tool_result_reports_all_passed(_result_block(None))
    assert not _tool_result_reports_all_passed(_result_block([]))
    assert not _tool_result_reports_all_passed(_result_block(''))
