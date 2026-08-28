"""A tool result's verdict must survive clipping.

Observed on the controller-respawn-observability run: all nine `wait_for_*` results
logged an identical 2013-character `detail`. `wait_for_terminal` returns `checks` (nine
rows, ~2k chars) BEFORE `status`, so the clip at _MAX cut the verdict off every time and
the outcomes had to be read from the MCP server's own logs instead. That breaks the bar
that no outcome is decided from a value whose provenance is not in Loki.
"""

from __future__ import annotations

import json
from typing import Any

from gate.agent import tool_logging


def _wait_for_terminal_payload(status: str, checks: int = 9) -> str:
    """The real shape: a long `checks` array first, the verdict last."""
    return json.dumps(
        {
            'checks': [
                {
                    'check': f'check-{i}',
                    'cluster': 'gcp',
                    'completed_at': '2026-08-19T08:04:07Z',
                    'name': f'some-reasonably-long-check-name-{i}',
                    'state': 'running',
                    'url': f'https://tekton-dashboard-jx.jx.leartech.com/#/namespaces/jx/pipelineruns/run-{i}',
                }
                for i in range(checks)
            ],
            'clusters_observed': ['gcp'],
            'clusters_unobserved': ['az'],
            'remaining_seconds': 1650,
            'merged': False,
            'status': status,
        }
    )


def _emitted(monkeypatch: Any, content: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_emit(level: str, event: str, msg: str, **fields: Any) -> None:
        captured.update(fields)
        captured['_level'] = level

    monkeypatch.setattr(tool_logging.obslog, 'emit', fake_emit)
    tool_logging.log_tool_result('mcp__leartech-jx3-flow__wait_for_terminal', content)
    return captured


def test_status_survives_when_the_payload_is_clipped(monkeypatch: Any) -> None:
    payload = _wait_for_terminal_payload('window_elapsed')
    assert len(payload) > tool_logging._MAX, 'fixture must exceed the clip limit to be meaningful'

    fields = _emitted(monkeypatch, payload)

    assert fields['status'] == 'window_elapsed'
    assert fields['remaining_seconds'] == 1650
    assert fields['merged'] is False
    assert 'chars]' in fields['detail'], (
        'detail should still be clipped — this promotes fields, it does not stop clipping'
    )
    assert 'window_elapsed' not in fields['detail'], 'the fixture is only meaningful while the verdict IS cut off'


def test_all_passed_is_promoted_too(monkeypatch: Any) -> None:
    fields = _emitted(monkeypatch, _wait_for_terminal_payload('all_passed'))
    assert fields['status'] == 'all_passed'


def test_content_as_parts_list_is_handled(monkeypatch: Any) -> None:
    payload = _wait_for_terminal_payload('some_failed')
    fields = _emitted(monkeypatch, [{'type': 'text', 'text': payload}])
    assert fields['status'] == 'some_failed'


def test_non_json_output_promotes_nothing_and_does_not_raise(monkeypatch: Any) -> None:
    fields = _emitted(monkeypatch, 'On branch main\nnothing to commit, working tree clean')
    assert 'status' not in fields
    assert fields['detail']


def test_json_without_verdict_keys_promotes_nothing(monkeypatch: Any) -> None:
    fields = _emitted(monkeypatch, json.dumps({'files': ['a.go', 'b.go']}))
    assert 'status' not in fields


def test_nested_objects_are_not_promoted(monkeypatch: Any) -> None:
    """Only scalars — a `first_failure` object would bloat the record it is meant to slim."""
    fields = _emitted(monkeypatch, json.dumps({'status': 'first_failure', 'first_failure': {'name': 'lint'}}))
    assert fields['status'] == 'first_failure'
    assert 'first_failure' not in {k for k in fields if k != 'status'} or not isinstance(
        fields.get('first_failure'), dict
    )
