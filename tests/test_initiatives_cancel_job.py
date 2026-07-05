"""Tests for Phase D.7 — ``POST /initiatives/{id}/cancel`` for runtime=job.

Before D.7 the cancel handler only handled the asyncio path (cancels an
asyncio.Task); Job-mode runs returned a 409 because no in-process task
existed to cancel. The Job kept running until natural completion / TTL.

D.7 makes cancel work for runtime=job by deleting the K8s Job. The preStop
hook on the Job pod posts a "cancelled" sticky to the PR (covered by
``test_job_runner.py``); the API handler records the terminal status
synchronously so the next ``GET`` reflects the cancel intent and the
reconciler skips the row.

K8s is NOT exercised here; everything routes through ``patch.dict('sys.modules', ...)``
against the lazily-imported ``kubernetes_asyncio.{client,config}`` — same
pattern as ``tests/test_initiatives_logs.py`` (D.6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from kubernetes_asyncio.client.exceptions import ApiException

from app import db as db_module
from app.main import app
from app.state import InitiativeRecord, register

_client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the in-memory state path. See test_initiatives_dual_path.py
    for the same fixture rationale — the deployed pod's DSN env var would
    otherwise leak into pytest."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()


async def _seed_job_record(
    *,
    run_id: str = 'run-cancel-test',
    job_name: str = 'run-cancel-test',
    status: str = 'running',
) -> None:
    """Register an in-memory record representing a runtime=job run.

    Seeded directly via ``register`` (no HTTP) because POST /initiatives
    on the job path would call spawn_initiative_job, and we want isolated
    coverage of the cancel handler's contract.
    """
    record = InitiativeRecord(
        id=run_id,
        initiative='demo',
        status=status,
        started_at=datetime.now(UTC),
        pr_repo='owner/demo',
        runtime='job',
        job_name=job_name,
    )
    await register(record)


async def test_cancel_deletes_agentrun_and_marks_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel of a runtime=job run deletes the AgentRun (the Go controller's
    owner-ref cascades to the Job) with the run id + namespace, and marks the
    DB row 'cancelled' synchronously."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-cancel-1', job_name='run-cancel-1')

    delete_mock = AsyncMock()
    with patch('gate.agent.agentrun_client.delete_agent_run', new=delete_mock):
        resp = _client.post('/initiatives/run-cancel-1/cancel')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['status'] == 'cancelled'
    assert body['runtime'] == 'job'
    assert body['finished_at'] is not None
    delete_mock.assert_awaited_once_with('run-cancel-1', 'jx-staging')


def test_cancel_unknown_id_returns_404() -> None:
    resp = _client.post('/initiatives/never-existed-xyz/cancel')
    assert resp.status_code == 404


async def test_cancel_requires_pod_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """POD_NAMESPACE is required — without it there is no namespace to cancel in."""
    monkeypatch.delenv('POD_NAMESPACE', raising=False)
    await _seed_job_record(run_id='run-no-ns', job_name='run-no-ns')
    resp = _client.post('/initiatives/run-no-ns/cancel')
    assert resp.status_code == 500
    assert 'POD_NAMESPACE' in resp.json()['detail']


async def test_cancel_surfaces_delete_errors_as_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-404 failure (RBAC denied, apiserver unreachable) surfaces as 502
    (404 is swallowed inside delete_agent_run)."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-rbac', job_name='run-rbac')

    boom = AsyncMock(side_effect=ApiException(status=403, reason='Forbidden'))
    with patch('gate.agent.agentrun_client.delete_agent_run', new=boom):
        resp = _client.post('/initiatives/run-rbac/cancel')

    assert resp.status_code == 502
    assert 'Failed to cancel AgentRun' in resp.json()['detail']


async def test_cancel_terminal_record_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second cancel on an already-terminal run is a no-op — the AgentRun is
    NOT re-deleted and the endpoint returns 200 with the existing record."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-already', job_name='run-already', status='cancelled')

    delete_mock = AsyncMock()
    with patch('gate.agent.agentrun_client.delete_agent_run', new=delete_mock):
        resp = _client.post('/initiatives/run-already/cancel')

    assert resp.status_code == 200
    assert resp.json()['status'] == 'cancelled'
    delete_mock.assert_not_awaited()
