"""Unit tests for the pure parts of gate.tools.video_review (prompt + parser).

Vision review is gateway-only (OpenAI seam) — these cover the pure helpers; the
actual review_video call needs the gateway + network and is exercised by the live
gate run + tests/test_video_review_gateway.py.
"""

from __future__ import annotations

import json

from gate.tools.video_review import (
    REPORT_ANOMALIES_TOOL,
    build_openai_user_message,
    parse_openai_tool_call,
)


def test_openai_user_message_contains_text_block_then_frames() -> None:
    frames = [b'\x89PNG\r\nFRAME0', b'\x89PNG\r\nFRAME1']
    blocks = build_openai_user_message('01-page-loads', frames, expected_flow='Home page renders.')
    assert blocks[0]['type'] == 'text'
    assert '01-page-loads' in blocks[0]['text']
    assert 'Home page renders.' in blocks[0]['text']
    assert len(blocks) == 1 + len(frames)
    # gateway/OpenAI multimodal shape: image_url data URIs, not Anthropic image blocks
    assert all(b['type'] == 'image_url' for b in blocks[1:])
    assert blocks[1]['image_url']['url'].startswith('data:image/png;base64,')


def test_openai_user_message_omits_expected_flow_when_none() -> None:
    blocks = build_openai_user_message('02-login', [b'frame'], expected_flow=None)
    assert 'Expected user flow' not in blocks[0]['text']


def _openai_resp(args: dict) -> dict:
    return {
        'choices': [
            {'message': {'tool_calls': [{'function': {'name': 'report_anomalies', 'arguments': json.dumps(args)}}]}}
        ]
    }


def test_parse_openai_tool_call_extracts_verdict() -> None:
    resp = _openai_resp(
        {
            'anomalies_found': True,
            'summary': 'Login button missing in frame 2 onwards.',
            'flagged_frames': [2, 3, 4],
        }
    )
    v = parse_openai_tool_call(resp, '02-login')
    assert v.spec_name == '02-login'
    assert v.anomalies_found is True
    assert v.summary.startswith('Login button missing')
    assert v.flagged_frames == (2, 3, 4)
    assert not v.passed


def test_parse_openai_tool_call_handles_clean_verdict() -> None:
    resp = _openai_resp({'anomalies_found': False, 'summary': 'Normal home-page render.', 'flagged_frames': []})
    v = parse_openai_tool_call(resp, '01-home')
    assert not v.anomalies_found
    assert v.passed
    assert v.flagged_frames == ()


def test_tool_schema_has_required_fields() -> None:
    schema = REPORT_ANOMALIES_TOOL['input_schema']
    assert set(schema['required']) == {'anomalies_found', 'summary', 'flagged_frames'}
