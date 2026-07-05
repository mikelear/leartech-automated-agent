"""Tests for the AgentRun-status → initiative_runs projection reconciler (Slice B)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import gate.agent.job_reconciler as rec


def _run(name: str, phase: str, target_pr: Any = None) -> dict[str, Any]:
    status: dict[str, Any] = {'phase': phase}
    if target_pr is not None:
        status['targetPR'] = str(target_pr)
    return {'metadata': {'name': name}, 'status': status}


def _wire(monkeypatch, *, runs, record_status, retro_raises=False):
    updates: list[dict[str, Any]] = []
    retro: list[str] = []

    async def fake_list(_ns: str) -> list[dict[str, Any]]:
        return runs

    async def fake_get(_rid: str) -> Any:
        return None if record_status is None else SimpleNamespace(status=record_status)

    async def fake_update(rid: str, **fields: Any) -> None:
        updates.append({'id': rid, **fields})

    async def fake_retro(rid: str) -> None:
        if retro_raises:
            raise RuntimeError('boom')
        retro.append(rid)

    monkeypatch.setattr(rec, 'list_agent_runs', fake_list)
    monkeypatch.setattr(rec, 'get_record', fake_get)
    monkeypatch.setattr(rec, 'update', fake_update)
    monkeypatch.setattr(rec, '_run_self_retrospect', fake_retro)
    return updates, retro


@pytest.mark.asyncio
async def test_projects_terminal_phase_and_pr_and_fires_retrospect(monkeypatch: pytest.MonkeyPatch) -> None:
    updates, retro = _wire(monkeypatch, runs=[_run('r1', 'Succeeded', 53)], record_status='running')
    assert await rec.reconcile_once('ns') == 1
    assert updates[0]['status'] == 'complete'
    assert updates[0]['pr_number'] == 53
    assert 'finished_at' in updates[0]
    assert retro == ['r1']  # retrospect fires only on complete


@pytest.mark.asyncio
async def test_running_projection_no_finish_no_retrospect(monkeypatch: pytest.MonkeyPatch) -> None:
    updates, retro = _wire(monkeypatch, runs=[_run('r1', 'Running')], record_status='queued')
    assert await rec.reconcile_once('ns') == 1
    assert updates[0]['status'] == 'running'
    assert updates[0]['pr_number'] is None
    assert 'finished_at' not in updates[0]
    assert retro == []


@pytest.mark.asyncio
async def test_skips_already_terminal_and_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # record already 'complete' (terminal) → skip; and Running==running → skip
    updates, _ = _wire(monkeypatch, runs=[_run('r1', 'Succeeded', 9)], record_status='complete')
    assert await rec.reconcile_once('ns') == 0
    updates2, _ = _wire(monkeypatch, runs=[_run('r2', 'Running')], record_status='running')
    assert await rec.reconcile_once('ns') == 0
    assert updates == [] and updates2 == []


@pytest.mark.asyncio
async def test_unknown_phase_and_missing_record_and_bad_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    # unmapped phase → skip
    updates, _ = _wire(
        monkeypatch, runs=[{'metadata': {'name': 'r'}, 'status': {'phase': 'Weird'}}], record_status='running'
    )
    assert await rec.reconcile_once('ns') == 0
    # no DB record → skip
    updates2, _ = _wire(monkeypatch, runs=[_run('r', 'Failed')], record_status=None)
    assert await rec.reconcile_once('ns') == 0
    # non-numeric targetPR → pr_number None (still projects)
    updates3, _ = _wire(monkeypatch, runs=[_run('r', 'Failed', 'not-a-number')], record_status='running')
    assert await rec.reconcile_once('ns') == 1
    assert updates3[0]['status'] == 'failed' and updates3[0]['pr_number'] is None


@pytest.mark.asyncio
async def test_retrospect_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    updates, _ = _wire(monkeypatch, runs=[_run('r1', 'Succeeded', 1)], record_status='running', retro_raises=True)
    # must not raise despite retrospect boom
    assert await rec.reconcile_once('ns') == 1
    assert updates[0]['status'] == 'complete'


def test_phase_mapping_is_total_over_known_phases() -> None:
    for phase in ('Pending', 'Queued', 'Running', 'Iterating', 'Succeeded', 'Failed', 'Cancelled'):
        assert rec._PHASE_TO_STATUS[phase] in {'queued', 'running', 'complete', 'failed', 'cancelled'}
