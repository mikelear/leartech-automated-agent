"""Tests for ``_resolve_target_pr`` — the status-first PR-identity resolver.

This is the single end-of-run PR read. It exists to STOP re-deriving "did this run
produce a PR?" from a flaky ``gh pr list --state open`` query that could not tell a
MERGED PR (a success — Tide merged it) from a never-created one — the root cause of
the expected_pr_missing false-FAIL. The resolver reads the authoritative value the
``open_pr`` MCP tool wrote (``AgentRun.status.targetPR``) FIRST, and only falls back
to a merge-aware ``gh`` query when that field is empty. Every resolution logs its
``source`` to Loki (event="target_pr_resolved") so provenance is greppable.
"""

from __future__ import annotations

from typing import Any

import pytest

import gate.agent.initiative as initiative


class _Recorder:
    def __init__(self, *, status_pr: str | None, gh_pr: int | None = None) -> None:
        self.status_pr = status_pr
        self.gh_pr = gh_pr
        self.get_calls: list[tuple[str, str]] = []
        self.gh_calls: list[tuple[str, str, str]] = []
        self.hint_writes: list[int | None] = []
        self.emits: list[dict[str, Any]] = []

    async def get_target_pr(self, name: str, namespace: str) -> str | None:
        self.get_calls.append((name, namespace))
        return self.status_pr

    def resolve_pr_number(self, qualified_repo: str, branch: str, *, state: str = 'open') -> int | None:
        self.gh_calls.append((qualified_repo, branch, state))
        return self.gh_pr

    def write_hint(self, pr_number: int | None) -> None:
        self.hint_writes.append(pr_number)

    def emit(self, level: str, event: str, msg: str, **fields: Any) -> None:
        self.emits.append({'level': level, 'event': event, 'msg': msg, **fields})


def _wire(monkeypatch: pytest.MonkeyPatch, rec: _Recorder, *, as_agentrun: bool = True) -> None:
    monkeypatch.setattr(initiative.agentrun_client, 'get_target_pr', rec.get_target_pr)
    monkeypatch.setattr(initiative, '_resolve_pr_number', rec.resolve_pr_number)
    monkeypatch.setattr(initiative, '_write_pr_number_hint', rec.write_hint)
    monkeypatch.setattr(initiative.obslog, 'emit', rec.emit)
    if as_agentrun:
        monkeypatch.setenv('AGENT_RUN_NAME', 'run-1')
        monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
        monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'true')
    else:
        monkeypatch.delenv('AGENT_RUN_NAME', raising=False)
        monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
        monkeypatch.delenv('LEARTECH_AGENTRUN_STATUS', raising=False)


def _resolved_events(rec: _Recorder) -> list[dict[str, Any]]:
    return [e for e in rec.emits if e['event'] == 'target_pr_resolved']


@pytest.mark.asyncio
async def test_status_first_returns_authoritative_and_skips_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(status_pr='12', gh_pr=999)
    _wire(monkeypatch, rec)

    number = await initiative._resolve_target_pr('mikelear/x', 'feat/a')

    assert number == 12
    assert rec.get_calls == [('run-1', 'jx-staging')]
    assert rec.gh_calls == []
    assert rec.hint_writes == [12]
    ev = _resolved_events(rec)
    assert len(ev) == 1
    assert ev[0]['source'] == 'status'
    assert ev[0]['targetPR'] == 12


@pytest.mark.asyncio
async def test_merged_pr_is_not_lost_regression(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(status_pr='12', gh_pr=None)
    _wire(monkeypatch, rec)

    number = await initiative._resolve_target_pr('mikelear/leartech-plan-api', 'feat/plan-api-dto-fidelity')

    assert number == 12
    assert rec.gh_calls == []


@pytest.mark.asyncio
async def test_falls_back_to_gh_all_state_when_status_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(status_pr=None, gh_pr=55)
    _wire(monkeypatch, rec)

    number = await initiative._resolve_target_pr('mikelear/x', 'feat/a')

    assert number == 55
    assert rec.gh_calls == [('mikelear/x', 'feat/a', 'all')]
    ev = _resolved_events(rec)
    assert len(ev) == 1
    assert ev[0]['source'] == 'gh_fallback'
    assert ev[0]['targetPR'] == 55


@pytest.mark.asyncio
async def test_returns_none_when_no_pr_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(status_pr=None, gh_pr=None)
    _wire(monkeypatch, rec)

    number = await initiative._resolve_target_pr('mikelear/x', 'feat/a')

    assert number is None
    ev = _resolved_events(rec)
    assert len(ev) == 1
    assert ev[0]['source'] == 'none'
    assert ev[0]['targetPR'] is None


@pytest.mark.asyncio
async def test_local_run_skips_status_read_and_uses_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(status_pr='12', gh_pr=88)
    _wire(monkeypatch, rec, as_agentrun=False)

    number = await initiative._resolve_target_pr('mikelear/x', 'feat/a')

    assert number == 88
    assert rec.get_calls == []
    assert rec.gh_calls == [('mikelear/x', 'feat/a', 'all')]
