"""Tool-trajectory logging + secret redaction (gate.agent.tool_logging).

Guards the fix for the "blind to what the agent ran" gap: the loop now emits
structured tool_call/tool_result events carrying the (redacted, truncated)
command and output. The redaction MUST hold — Bash I/O routinely contains the
projected SA key / tokens.
"""

from __future__ import annotations

import json
import logging

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


# ---------------------------------------------------------------------------
# log_advertised_tools — one record per run naming every MCP server + every
# allowed tool, at INFO, so a reader with only Loki and one run id can compute
# the never-called set.
# ---------------------------------------------------------------------------


def _advertised(capsys) -> list[dict]:
    return [rec for rec in _emitted(capsys) if rec.get('event') == tool_logging.ADVERTISED_TOOLS_EVENT]


def test_log_advertised_tools_emits_exactly_one_record(capsys):
    """One call → one record. The recorder computing never-called sets keys on the
    event, so a duplicate emission would double-count and skew the delta."""
    tool_logging.log_advertised_tools(
        {'leartech-pr-context': object()},
        ['Read', 'mcp__leartech-pr-context__open_pr'],
    )
    recs = _advertised(capsys)
    assert len(recs) == 1


def test_log_advertised_tools_names_every_server_and_tool(capsys):
    """The wire contract: `mcp_servers` + `allowed_tools` are sorted list[str]
    naming every advertised server and every allowed tool respectively. A
    reader with only Loki must be able to reconstruct the advertised set."""
    servers = {
        'leartech-pr-context': object(),
        'leartech-jx3-flow': object(),
        'leartech-tekton': object(),
    }
    tools = [
        'Read',
        'Bash',
        'mcp__leartech-pr-context__open_pr',
        'mcp__leartech-jx3-flow__wait_for_terminal',
        'mcp__leartech-tekton__step_logs',
    ]
    tool_logging.log_advertised_tools(servers, tools)
    (rec,) = _advertised(capsys)
    assert rec['mcp_servers'] == sorted(servers)
    assert rec['allowed_tools'] == sorted(tools)
    assert rec['mcp_server_count'] == 3
    assert rec['allowed_tool_count'] == 5


def test_log_advertised_tools_accepts_iterable_of_server_names(capsys):
    """Callers with just a list of names (no dict) should not need to build a
    dummy dict — the helper accepts either shape."""
    tool_logging.log_advertised_tools(
        ['leartech-pr-context', 'leartech-jx3-flow'],
        ['Read'],
    )
    (rec,) = _advertised(capsys)
    assert rec['mcp_servers'] == ['leartech-jx3-flow', 'leartech-pr-context']


def test_log_advertised_tools_carries_run_id(monkeypatch, capsys):
    """The record MUST carry the run id — it's what joins this line to the rest
    of the run in Loki. Without it a reader can't compute the never-called
    delta scoped to one run."""
    monkeypatch.setenv('LEARTECH_RUN_ID', 'run-abc-42')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    tool_logging.log_advertised_tools({'leartech-pr-context': object()}, ['Read'])
    (rec,) = _advertised(capsys)
    assert rec['run_id'] == 'run-abc-42'
    assert rec['namespace'] == 'jx-staging'


def test_log_advertised_tools_emitted_at_info_survives_deployed_verbosity(capsys):
    """The controller runs at INFO and a DEBUG record is the same as no record.
    Assert the record survives a level filter clamped to INFO — not merely
    that the call happens."""
    # Clamp the obslog logger to INFO (deployed-verbosity floor). A DEBUG
    # record would be filtered out here; INFO must survive.
    obslog._ensure_configured()
    obslog._logger.setLevel(logging.INFO)
    try:
        tool_logging.log_advertised_tools({'leartech-pr-context': object()}, ['Read'])
        (rec,) = _advertised(capsys)
        assert rec['level'] == 'INFO', (
            'log_advertised_tools MUST emit at INFO — DEBUG is filtered by the deployed '
            'log config and the record would be invisible in Loki'
        )
    finally:
        obslog._logger.setLevel(logging.DEBUG)


def test_log_advertised_tools_msg_is_human_readable(capsys):
    """The `msg` field should be a one-glance summary — the counts. Operators
    reading Loki without the JSON expander still see something useful."""
    tool_logging.log_advertised_tools(
        {'leartech-pr-context': object(), 'leartech-jx3-flow': object()},
        ['Read', 'Bash', 'Grep'],
    )
    (rec,) = _advertised(capsys)
    assert '2 MCP server(s)' in rec['msg']
    assert '3 allowed tool(s)' in rec['msg']


def test_log_advertised_tools_stable_event_name(capsys):
    """The `event` field is the wire contract the recorder keys on. Pin the
    literal name so an accidental rename breaks this test loudly."""
    tool_logging.log_advertised_tools({}, [])
    (rec,) = _advertised(capsys)
    assert rec['event'] == 'agent_advertised_tools'


def test_log_advertised_tools_empty_case_still_emits(capsys):
    """A run with no MCPs / no allowed tools still emits the record — an empty
    advertised set is a fact worth logging (helps a reader distinguish 'the
    agent was blank' from 'the record was dropped')."""
    tool_logging.log_advertised_tools({}, [])
    (rec,) = _advertised(capsys)
    assert rec['mcp_servers'] == []
    assert rec['allowed_tools'] == []
    assert rec['mcp_server_count'] == 0
    assert rec['allowed_tool_count'] == 0
