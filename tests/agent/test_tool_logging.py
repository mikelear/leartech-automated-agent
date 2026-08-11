"""Tool-trajectory logging + secret redaction (gate.agent.tool_logging).

Guards the fix for the "blind to what the agent ran" gap: the loop now emits
structured tool_call/tool_result events carrying the (redacted, truncated)
command and output. The redaction MUST hold — Bash I/O routinely contains the
projected SA key / tokens.
"""

from __future__ import annotations

import json

import pytest

from gate import obslog
from gate.agent import tool_logging


@pytest.fixture(autouse=True)
def _reset_obslog():
    """obslog binds a StreamHandler to sys.stderr on first configure; reset it so
    each test rebinds to capsys's freshly-swapped stderr."""
    obslog._configured = False
    obslog._logger.handlers.clear()
    yield
    obslog._configured = False
    obslog._logger.handlers.clear()


def _emitted(capsys) -> list[dict]:
    out = capsys.readouterr().err
    return [json.loads(line) for line in out.splitlines() if line.strip().startswith('{')]


def test_log_tool_call_carries_bash_command(capsys):
    tool_logging.log_tool_call('Bash', {'command': 'find /tmp/artifact -type f'})
    rec = _emitted(capsys)[-1]
    assert rec['event'] == 'tool_call'
    assert rec['tool'] == 'Bash'
    assert 'find /tmp/artifact' in rec['detail']


def test_redacts_secret_env_value(monkeypatch, capsys):
    monkeypatch.setenv('GH_TOKEN', 'ghp_supersecrettokenvalue1234567890')
    tool_logging.log_tool_call('Bash', {'command': 'git push https://x:ghp_supersecrettokenvalue1234567890@github.com'})
    detail = _emitted(capsys)[-1]['detail']
    assert 'ghp_supersecrettokenvalue1234567890' not in detail
    assert '***REDACTED***' in detail


def test_redacts_private_key_block_by_shape(capsys):
    pem = '-----BEGIN PRIVATE KEY-----\nMIIEvQIBADAN...\n-----END PRIVATE KEY-----'
    tool_logging.log_tool_result('Bash', f'contents: {pem}')
    detail = _emitted(capsys)[-1]['detail']
    assert 'BEGIN PRIVATE KEY' not in detail
    assert '***REDACTED***' in detail


def test_redacts_gcp_access_token_by_shape(capsys):
    tool_logging.log_tool_result('Bash', 'Authorization: Bearer ya29.a0AfB_longtokenstring12345')
    assert 'ya29.a0AfB_longtokenstring12345' not in _emitted(capsys)[-1]['detail']


def test_result_list_content_and_error_flag(capsys):
    tool_logging.log_tool_result('Bash', [{'type': 'text', 'text': 'boom failed'}], is_error=True)
    rec = _emitted(capsys)[-1]
    assert rec['event'] == 'tool_result'
    assert rec['ok'] is False
    assert rec['level'] == 'WARN'
    assert 'boom failed' in rec['detail']


def test_truncates_large_output(capsys):
    tool_logging.log_tool_result('Bash', 'x' * 5000)
    detail = _emitted(capsys)[-1]['detail']
    assert len(detail) < 5000
    assert 'chars]' in detail


def test_input_summary_prefers_salient_field(capsys):
    tool_logging.log_tool_call('Read', {'file_path': '/workspace/styles.scss'})
    assert '/workspace/styles.scss' in _emitted(capsys)[-1]['detail']
