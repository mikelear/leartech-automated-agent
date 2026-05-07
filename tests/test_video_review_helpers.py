"""Unit tests for the pure parts of gate.tools.video_review (prompt + parser).

The actual SDK call (review_video) needs an API key + network and is exercised by the
live gate run, not by these unit tests.
"""

from __future__ import annotations

from gate.tools.video_review import (
    REPORT_ANOMALIES_TOOL,
    build_user_message,
    parse_tool_use_response,
)


def test_user_message_contains_text_block_then_frames() -> None:
    frames = [b'\x89PNG\r\nFRAME0', b'\x89PNG\r\nFRAME1']
    blocks = build_user_message('01-page-loads', frames, expected_flow='Home page renders.')
    assert blocks[0]['type'] == 'text'
    assert '01-page-loads' in blocks[0]['text']
    assert 'Home page renders.' in blocks[0]['text']
    assert len(blocks) == 1 + len(frames)
    assert all(b['type'] == 'image' for b in blocks[1:])


def test_user_message_omits_expected_flow_when_none() -> None:
    blocks = build_user_message('02-login', [b'frame'], expected_flow=None)
    assert 'Expected user flow' not in blocks[0]['text']


def test_parse_tool_use_response_extracts_verdict() -> None:
    content = [
        {
            'type': 'tool_use',
            'name': 'report_anomalies',
            'input': {
                'anomalies_found': True,
                'summary': 'Login button missing in frame 2 onwards.',
                'flagged_frames': [2, 3, 4],
            },
        }
    ]
    v = parse_tool_use_response(content, '02-login')
    assert v.spec_name == '02-login'
    assert v.anomalies_found is True
    assert v.summary.startswith('Login button missing')
    assert v.flagged_frames == (2, 3, 4)
    assert not v.passed


def test_parse_tool_use_response_handles_clean_verdict() -> None:
    content = [
        {
            'type': 'tool_use',
            'name': 'report_anomalies',
            'input': {'anomalies_found': False, 'summary': 'Normal home-page render.', 'flagged_frames': []},
        }
    ]
    v = parse_tool_use_response(content, '01-home')
    assert not v.anomalies_found
    assert v.passed
    assert v.flagged_frames == ()


def test_tool_schema_has_required_fields() -> None:
    schema = REPORT_ANOMALIES_TOOL['input_schema']
    assert set(schema['required']) == {'anomalies_found', 'summary', 'flagged_frames'}
