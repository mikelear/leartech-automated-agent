"""Unit tests for gate.obslog — the structured JSON logger (Phase A).

Local CoS: every record is valid JSON with the stable schema fields, ambient run
context is picked up from env, None fields are dropped, levels are normalised, and
a run_start/run_end pair emits as two parseable lines. This is what makes the
Loki/Grafana queries (`| json | event="run_end"` etc.) reliable.

obslog logs via the stdlib logging module through a dedicated ``%(message)s``
handler, so we capture the logger directly (a StringIO handler on the obslog
logger) rather than capsys — robust regardless of the handler's bound stream.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from gate import obslog


@pytest.fixture
def cap_obslog() -> Iterator[io.StringIO]:
    """Capture obslog's JSON lines via a StringIO handler on its logger."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter('%(message)s'))
    lg = logging.getLogger('leartech.obslog')
    lg.addHandler(handler)
    lg.setLevel(logging.DEBUG)
    try:
        yield buf
    finally:
        lg.removeHandler(handler)


def _lines(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().strip().splitlines() if line.strip()]


def test_emit_is_valid_json_with_schema_fields(cap_obslog: io.StringIO) -> None:
    obslog.emit('INFO', 'run_start', 'starting', logger='agent.initiative', model='claude-opus-4-8')
    (rec,) = _lines(cap_obslog)
    assert rec['level'] == 'INFO'
    assert rec['logger'] == 'agent.initiative'
    assert rec['event'] == 'run_start'
    assert rec['msg'] == 'starting'
    assert rec['model'] == 'claude-opus-4-8'
    assert 'time' in rec


def test_context_env_included_and_absent_omitted(cap_obslog: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_RUN_ID', 's0-run-x')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    monkeypatch.delenv('CLUSTER', raising=False)
    obslog.info('run_start', 'hi')
    (rec,) = _lines(cap_obslog)
    assert rec['run_id'] == 's0-run-x'
    assert rec['namespace'] == 'jx-staging'
    assert 'cluster' not in rec


def test_none_fields_dropped_and_level_normalised(cap_obslog: io.StringIO) -> None:
    obslog.emit('bogus', 'run_end', 'done', exit_code=0, targetPR=None, turns=5)
    (rec,) = _lines(cap_obslog)
    assert rec['level'] == 'INFO'
    assert rec['exit_code'] == 0
    assert rec['turns'] == 5
    assert 'targetPR' not in rec


def test_level_wrappers(cap_obslog: io.StringIO) -> None:
    obslog.info('e', 'i')
    obslog.warning('e', 'w')
    obslog.error('e', 'x')
    levels = [r['level'] for r in _lines(cap_obslog)]
    assert levels == ['INFO', 'WARN', 'ERROR']


def test_run_start_end_pair_parses(cap_obslog: io.StringIO) -> None:
    obslog.info('run_start', 'starting', logger='agent.initiative')
    obslog.emit('ERROR', 'run_end', 'finished', logger='agent.initiative', exit_code=1, reason='no PR')
    recs = _lines(cap_obslog)
    assert [r['event'] for r in recs] == ['run_start', 'run_end']
    assert recs[1]['exit_code'] == 1
    assert recs[1]['reason'] == 'no PR'


def test_initiative_main_crash_emits_run_end_error(
    cap_obslog: io.StringIO, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The main() crash branch must emit a run_end ERROR (exit_code=1,
    reason='crashed') and re-raise — so a crashed run still produces its
    authoritative outcome line in Loki instead of vanishing."""
    from gate.agent import initiative

    async def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError('kaboom')

    monkeypatch.setattr(initiative, 'run_initiative', _boom)
    with pytest.raises(RuntimeError, match='kaboom'):
        initiative.main.callback(  # type: ignore[misc]  # click Command.callback = the underlying fn
            initiative_path=tmp_path / 'x.yaml', repo_root=None, model='m', max_turns=1
        )
    ends = [r for r in _lines(cap_obslog) if r['event'] == 'run_end']
    assert ends, 'crash path emitted no run_end'
    assert ends[-1]['level'] == 'ERROR'
    assert ends[-1]['exit_code'] == 1
    assert ends[-1]['reason'] == 'crashed'
