"""Tests for the end-of-run targetPR runtime backstop.

The backstop guarantees ``AgentRun.status.targetPR`` is set even when the LLM
opened a PR via raw ``gh`` without calling the ``open_pr`` MCP tool, and emits a
loud, greppable Loki signal (event="targetpr_backstop_fired") so a future
forensic / scrum-master agent can harvest the "open_pr skipped" cases.
"""

from __future__ import annotations

from typing import Any

import pytest

import gate.agent.initiative as initiative


class _Recorder:
    """Captures patch_target_pr / get_target_pr calls + obslog emits."""

    def __init__(self, *, current_target_pr: str | None, patch_raises: bool = False) -> None:
        self.current_target_pr = current_target_pr
        self.patch_raises = patch_raises
        self.get_calls: list[tuple[str, str]] = []
        self.patch_calls: list[tuple[str, str, int]] = []
        self.emits: list[dict[str, Any]] = []

    async def get_target_pr(self, name: str, namespace: str) -> str | None:
        self.get_calls.append((name, namespace))
        return self.current_target_pr

    async def patch_target_pr(self, name: str, namespace: str, pr_number: int) -> None:
        self.patch_calls.append((name, namespace, pr_number))
        if self.patch_raises:
            raise RuntimeError('patch blew up')

    def emit(self, level: str, event: str, msg: str, **fields: Any) -> None:
        self.emits.append({'level': level, 'event': event, 'msg': msg, **fields})


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    rec: _Recorder,
    *,
    as_agentrun: bool = True,
) -> None:
    monkeypatch.setattr(initiative.agentrun_client, 'get_target_pr', rec.get_target_pr)
    monkeypatch.setattr(initiative.agentrun_client, 'patch_target_pr', rec.patch_target_pr)
    monkeypatch.setattr(initiative.obslog, 'emit', rec.emit)
    if as_agentrun:
        monkeypatch.setenv('AGENT_RUN_NAME', 'run-1')
        monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
        monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'true')
    else:
        monkeypatch.delenv('AGENT_RUN_NAME', raising=False)
        monkeypatch.delenv('AGENT_RUN_NAMESPACE', raising=False)
        monkeypatch.delenv('LEARTECH_AGENTRUN_STATUS', raising=False)


def _fired_events(rec: _Recorder) -> list[dict[str, Any]]:
    return [e for e in rec.emits if e['event'] == 'targetpr_backstop_fired']


@pytest.mark.asyncio
async def test_fires_when_target_pr_empty_and_pr_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(current_target_pr=None)
    _wire(monkeypatch, rec)

    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=77)

    assert rec.patch_calls == [('run-1', 'jx-staging', 77)]
    fired = _fired_events(rec)
    assert len(fired) == 1
    ev = fired[0]
    assert ev['level'] == 'WARN'
    assert ev['logger'] == 'agent.initiative'
    assert ev['run_id'] == 'run-1'
    assert ev['repo'] == 'mikelear/x'
    assert ev['branch'] == 'feat/a'
    assert ev['targetPR'] == 77
    assert ev['reason'] == 'open_pr_not_called'


@pytest.mark.asyncio
async def test_does_not_fire_when_target_pr_already_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # open_pr already published the field → no patch, no event (no false signal).
    rec = _Recorder(current_target_pr='55')
    _wire(monkeypatch, rec)

    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=77)

    assert rec.patch_calls == []
    assert _fired_events(rec) == []


@pytest.mark.asyncio
async def test_does_not_fire_when_no_pr_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    # Agent legitimately opened no PR on the branch.
    rec = _Recorder(current_target_pr=None)
    _wire(monkeypatch, rec)

    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=None)

    assert rec.patch_calls == []
    assert _fired_events(rec) == []


@pytest.mark.asyncio
async def test_does_not_fire_when_not_agentrun(monkeypatch: pytest.MonkeyPatch) -> None:
    # Local/dev run: no AGENT_RUN_NAME → skip entirely (never even reads status).
    rec = _Recorder(current_target_pr=None)
    _wire(monkeypatch, rec, as_agentrun=False)

    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=77)

    assert rec.get_calls == []
    assert rec.patch_calls == []
    assert _fired_events(rec) == []


@pytest.mark.asyncio
async def test_does_not_fire_when_status_reporting_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(current_target_pr=None)
    _wire(monkeypatch, rec)
    monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'false')

    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=77)

    assert rec.get_calls == []
    assert rec.patch_calls == []
    assert _fired_events(rec) == []


@pytest.mark.asyncio
async def test_patch_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A patch failure must never propagate (would change the run's exit code).
    rec = _Recorder(current_target_pr=None, patch_raises=True)
    _wire(monkeypatch, rec)

    # Must not raise.
    await initiative._backstop_target_pr(qualified_repo='mikelear/x', branch='feat/a', pr_number=77)

    assert rec.patch_calls == [('run-1', 'jx-staging', 77)]
