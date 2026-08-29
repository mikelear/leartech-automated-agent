"""A failed Bash tool result must promote its exit status and identifying error.

Same class of bug as ``_verdict_fields``: a chatty failing command
(``pytest``-with-lots-of-output, ``kaniko`` build, ``curl -v``) fills the
2000-char clip window with partial stdout, and the actual diagnostic line
— the one a Loki reader needs to tell "no such file" from "permission
denied" from "connection refused" — is the FIRST casualty of clipping.

Promotion happens BEFORE ``_clip``. Field-name wire contract:
``exit_code`` (int) + ``error`` (str, bounded to :data:`_BASH_ERROR_MAX`).
"""

from __future__ import annotations

import json
from typing import Any

from gate.agent import tool_logging


def _emitted(monkeypatch: Any, content: Any, *, is_error: bool = True, tool: str = 'Bash') -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_emit(level: str, event: str, msg: str, **fields: Any) -> None:
        captured.update(fields)
        captured['_level'] = level

    monkeypatch.setattr(tool_logging.obslog, 'emit', fake_emit)
    tool_logging.log_tool_result(tool, content, is_error=is_error)
    return captured


# ---------------------------------------------------------------------------
# Failure-case promotion
# ---------------------------------------------------------------------------


def test_exit_code_and_error_promoted_on_short_failure(monkeypatch: Any) -> None:
    """Single-line diagnostic — the common ``ls /missing`` shape."""
    payload = "Exit code 2\nls: cannot access '/nonexistent': No such file or directory"
    fields = _emitted(monkeypatch, payload)

    assert fields['exit_code'] == 2
    assert fields['error'] == "ls: cannot access '/nonexistent': No such file or directory"
    # The verdict-fields contract is untouched.
    assert 'status' not in fields


def test_exit_status_survives_when_payload_exceeds_clip_limit(monkeypatch: Any) -> None:
    """The behaviour this whole change exists to protect: an over-clip-limit
    failing bash call still yields exit status + identifying error.
    """
    prefix = 'Exit code 137\n'
    # 3000 chars of noisy pytest-style stdout, then the diagnostic LAST — mirrors
    # the wait_for_terminal-style shape that trapped `_verdict_fields`.
    noisy_middle = ('.' * 60 + '\n') * 50
    tail = 'FAILED tests/foo.py::test_bar - AssertionError: expected 1, got 2'
    payload = prefix + noisy_middle + tail
    assert len(payload) > tool_logging._MAX, 'fixture must exceed clip limit to be meaningful'

    fields = _emitted(monkeypatch, payload)

    assert fields['exit_code'] == 137
    assert fields['error'] == tail
    assert 'chars]' in fields['detail'], 'detail is still clipped — promotion does not replace clipping'
    assert tail not in fields['detail'], (
        'the fixture is only meaningful while the diagnostic line IS cut off from detail'
    )


def test_content_as_parts_list_is_handled(monkeypatch: Any) -> None:
    """SDK's ``ToolResultBlock.content`` can be a str OR a list of ``{type,text}``
    parts. Both shapes must promote."""
    payload = 'Exit code 1\nfatal: not a git repository (or any parent up to mount point /)'
    fields = _emitted(monkeypatch, [{'type': 'text', 'text': payload}])
    assert fields['exit_code'] == 1
    assert fields['error'].startswith('fatal: not a git repository')


def test_last_non_blank_line_is_chosen(monkeypatch: Any) -> None:
    """Shells emit the conclusive diagnostic AFTER partial stdout. Take the
    LAST non-blank line, not the first."""
    payload = (
        'Exit code 1\n'
        'Building wheel for foo (setup.py): started\n'
        'Building wheel for foo (setup.py): finished with status ERROR\n'
        '\n'
        '  error: subprocess-exited-with-error\n'
        '\n'
        '  × Getting requirements to build wheel did not run successfully.'
    )
    fields = _emitted(monkeypatch, payload)
    assert fields['exit_code'] == 1
    assert fields['error'] == '× Getting requirements to build wheel did not run successfully.'


def test_promoted_error_is_bounded(monkeypatch: Any) -> None:
    """A promoted field that itself grows to _MAX has moved the problem, not
    fixed it. The error field is capped at :data:`_BASH_ERROR_MAX`."""
    very_long_diagnostic = 'ERROR: ' + ('x' * 5000)
    payload = f'Exit code 1\n{very_long_diagnostic}'
    fields = _emitted(monkeypatch, payload)
    assert fields['exit_code'] == 1
    assert isinstance(fields['error'], str)
    # Well below _MAX — the whole point.
    assert len(fields['error']) < tool_logging._MAX
    assert len(fields['error']) <= tool_logging._BASH_ERROR_MAX + 32  # bound + short overflow suffix
    assert 'chars]' in fields['error'], 'the error field must indicate it was itself clipped'


def test_error_line_is_redacted(monkeypatch: Any) -> None:
    """redact() runs on the promoted error, same as on ``detail``. A
    secret-shaped bearer token in the failing curl line must NOT survive
    promotion."""
    payload = 'Exit code 22\ncurl: (22) Authorization: Bearer ya29.a0AfB_longtokenstring_secret_leak'
    fields = _emitted(monkeypatch, payload)
    assert 'ya29.a0AfB_longtokenstring_secret_leak' not in fields['error']
    assert '***REDACTED***' in fields['error']


def test_only_blank_lines_after_exit_code_yields_empty_error(monkeypatch: Any) -> None:
    """`exit 7` with no output → exit code promoted, error is empty string (not
    a KeyError-inducing None)."""
    fields = _emitted(monkeypatch, 'Exit code 7\n')
    assert fields['exit_code'] == 7
    assert fields['error'] == ''


# ---------------------------------------------------------------------------
# Non-failure cases must NOT fire — no spurious failure fields
# ---------------------------------------------------------------------------


def test_success_does_not_emit_failure_fields(monkeypatch: Any) -> None:
    """A successful bash returns raw output — no `Exit code N` prefix. Nothing
    should be promoted, and no ``exit_code`` / ``error`` field should appear."""
    fields = _emitted(
        monkeypatch,
        'On branch main\nnothing to commit, working tree clean\n',
        is_error=False,
    )
    assert 'exit_code' not in fields
    assert 'error' not in fields


def test_success_looking_output_never_promotes_even_if_it_mentions_exit_code(monkeypatch: Any) -> None:
    """Guard: even if a successful command's stdout happens to contain the
    literal string "Exit code 3" somewhere, we do NOT promote because
    ``is_error=False`` gates the entire helper."""
    fields = _emitted(
        monkeypatch,
        'Recent runs report:\n  Exit code 3 was seen once last week\n',
        is_error=False,
    )
    assert 'exit_code' not in fields
    assert 'error' not in fields


def test_non_bash_error_without_exit_code_prefix_promotes_nothing(monkeypatch: Any) -> None:
    """An MCP tool that errored (is_error=True) but returned JSON, not the
    ``Exit code N`` shape — the format-check gate prevents a false-positive
    promotion. And the verdict-fields path still fires as before."""
    payload = json.dumps({'status': 'some_failed', 'checks': []})
    fields = _emitted(
        monkeypatch,
        payload,
        is_error=True,
        tool='mcp__leartech-jx3-flow__wait_for_terminal',
    )
    # No bash promotion — different shape.
    assert 'exit_code' not in fields
    # Verdict promotion still works — the two paths are independent.
    assert fields['status'] == 'some_failed'


# ---------------------------------------------------------------------------
# Cross-contract: verdict-field behaviour is unchanged
# ---------------------------------------------------------------------------


def test_bash_failure_does_not_emit_verdict_keys(monkeypatch: Any) -> None:
    """A bash result must not start emitting verdict keys (`status`, `merged`,
    `remaining_seconds`) — those are the wire contract for MCP tools that
    return a verdict scalar, not for shell failures."""
    fields = _emitted(monkeypatch, 'Exit code 1\nfatal: could not read from remote')
    for verdict_key in tool_logging._VERDICT_KEYS:
        assert verdict_key not in fields, f'bash failure leaked verdict key {verdict_key!r}'


def test_mcp_verdict_result_does_not_emit_bash_failure_keys(monkeypatch: Any) -> None:
    """The reverse: an MCP tool result must not start emitting bash-failure
    keys (`exit_code`, `error`)."""
    payload = json.dumps(
        {
            'status': 'all_passed',
            'checks': [],
            'merged': True,
            'remaining_seconds': 0,
        }
    )
    fields = _emitted(
        monkeypatch,
        payload,
        is_error=False,
        tool='mcp__leartech-jx3-flow__wait_for_terminal',
    )
    assert fields['status'] == 'all_passed'
    assert 'exit_code' not in fields
    assert 'error' not in fields
