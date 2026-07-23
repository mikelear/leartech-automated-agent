"""Unit tests for gate.obslog — the structured JSON logger (Phase A).

Local CoS: every record is valid JSON with the stable schema fields, ambient run
context is picked up from env, None fields are dropped, levels are normalised, and
a run_start/run_end pair emits as two parseable lines. This is what makes the
Loki/Grafana queries (`| json | event="run_end"` etc.) reliable.
"""

from __future__ import annotations

import json

import pytest

from gate import obslog


def _lines(captured: str) -> list[dict]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


def test_emit_is_valid_json_with_schema_fields(capsys: pytest.CaptureFixture[str]) -> None:
    obslog.emit('INFO', 'run_start', 'starting', logger='agent.initiative', model='claude-opus-4-8')
    (rec,) = _lines(capsys.readouterr().err)
    assert rec['level'] == 'INFO'
    assert rec['logger'] == 'agent.initiative'
    assert rec['event'] == 'run_start'
    assert rec['msg'] == 'starting'
    assert rec['model'] == 'claude-opus-4-8'
    assert 'time' in rec  # ISO8601 timestamp present


def test_context_env_included_and_absent_omitted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('LEARTECH_RUN_ID', 's0-run-x')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    monkeypatch.delenv('CLUSTER', raising=False)
    obslog.info('run_start', 'hi')
    (rec,) = _lines(capsys.readouterr().err)
    assert rec['run_id'] == 's0-run-x'
    assert rec['namespace'] == 'jx-staging'
    assert 'cluster' not in rec  # absent env → omitted, no crash


def test_none_fields_dropped_and_level_normalised(capsys: pytest.CaptureFixture[str]) -> None:
    obslog.emit('bogus', 'run_end', 'done', exit_code=0, targetPR=None, turns=5)
    (rec,) = _lines(capsys.readouterr().err)
    assert rec['level'] == 'INFO'  # unknown level → INFO
    assert rec['exit_code'] == 0
    assert rec['turns'] == 5
    assert 'targetPR' not in rec  # None dropped


def test_level_wrappers(capsys: pytest.CaptureFixture[str]) -> None:
    obslog.info('e', 'i')
    obslog.warning('e', 'w')
    obslog.error('e', 'x')
    levels = [r['level'] for r in _lines(capsys.readouterr().err)]
    assert levels == ['INFO', 'WARN', 'ERROR']


def test_run_start_end_pair_parses(capsys: pytest.CaptureFixture[str]) -> None:
    obslog.info('run_start', 'starting', logger='agent.initiative')
    obslog.emit('ERROR', 'run_end', 'finished', logger='agent.initiative', exit_code=1, reason='no PR')
    recs = _lines(capsys.readouterr().err)
    assert [r['event'] for r in recs] == ['run_start', 'run_end']
    assert recs[1]['exit_code'] == 1
    assert recs[1]['reason'] == 'no PR'
