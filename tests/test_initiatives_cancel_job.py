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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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


def _wire_k8s_mocks(*, delete_side_effect: object = None) -> tuple[MagicMock, AsyncMock, MagicMock, MagicMock]:
    """Build the mock objects for ``kubernetes_asyncio``.

    Returns (k8s_config_mock, delete_async_mock, k8s_client_mock,
    api_client_cls_mock). The caller wraps the ``patch.dict('sys.modules', ...)``
    context themselves so they can nest other patches if needed.
    """
    delete_mock: AsyncMock = (
        AsyncMock(side_effect=delete_side_effect)
        if delete_side_effect is not None
        else AsyncMock(return_value=MagicMock())
    )
    batch_mock = MagicMock()
    batch_mock.delete_namespaced_job = delete_mock

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.BatchV1Api.return_value = batch_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    return k8s_config_mock, delete_mock, k8s_client_mock, api_client_cls_mock


def _sys_modules_patch(k8s_config_mock: MagicMock, k8s_client_mock: MagicMock, api_client_cls_mock: MagicMock) -> Any:
    """Return a context manager that patches kubernetes_asyncio in sys.modules.

    Matches the pattern test_initiatives_logs.py uses for the same
    lazily-imported library.
    """
    return patch.dict(
        'sys.modules',
        {
            'kubernetes_asyncio': MagicMock(client=k8s_client_mock, config=k8s_config_mock),
            'kubernetes_asyncio.client': k8s_client_mock,
            'kubernetes_asyncio.config': k8s_config_mock,
            'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_client_cls_mock),
            'kubernetes_asyncio.client.exceptions': MagicMock(ApiException=ApiException),
        },
    )


async def test_cancel_job_runtime_deletes_k8s_job_and_marks_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: cancel of a runtime=job run calls
    BatchV1Api.delete_namespaced_job with the right name+namespace, marks
    the DB row 'cancelled' synchronously, and returns the updated record."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-cancel-1', job_name='run-cancel-1')

    cfg, delete_mock, client_mod, api_cls = _wire_k8s_mocks()
    with _sys_modules_patch(cfg, client_mod, api_cls):
        resp = _client.post('/initiatives/run-cancel-1/cancel')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['status'] == 'cancelled'
    assert body['runtime'] == 'job'
    assert body['finished_at'] is not None

    cfg.load_incluster_config.assert_called_once()
    delete_mock.assert_awaited_once()
    call_kwargs = delete_mock.await_args.kwargs
    assert call_kwargs['name'] == 'run-cancel-1'
    assert call_kwargs['namespace'] == 'jx-staging'
    # Background propagation lets the pod terminate gracefully (preStop
    # hook posts the cancelled sticky) before K8s GCs the Job.
    assert call_kwargs['propagation_policy'] == 'Background'


def test_cancel_unknown_id_returns_404() -> None:
    """An unknown initiative_id is a 404 regardless of runtime."""
    resp = _client.post('/initiatives/never-existed-xyz/cancel')
    assert resp.status_code == 404


async def test_cancel_job_runtime_requires_pod_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POD_NAMESPACE is required — same contract as POST /initiatives and
    GET /initiatives/{id}/logs. Without it we have no target namespace
    to delete the Job from."""
    monkeypatch.delenv('POD_NAMESPACE', raising=False)
    await _seed_job_record(run_id='run-no-ns', job_name='run-no-ns')

    resp = _client.post('/initiatives/run-no-ns/cancel')

    assert resp.status_code == 500
    assert 'POD_NAMESPACE' in resp.json()['detail']


async def test_cancel_job_runtime_tolerates_already_deleted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the Job is already gone (TTL'd out, or a prior cancel completed),
    K8s returns 404. The operator's intent is already satisfied; the
    handler must still mark the DB row 'cancelled' rather than surfacing
    a 502."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-gone', job_name='run-gone')

    cfg, delete_mock, client_mod, api_cls = _wire_k8s_mocks(
        delete_side_effect=ApiException(status=404, reason='Not Found'),
    )
    with _sys_modules_patch(cfg, client_mod, api_cls):
        resp = _client.post('/initiatives/run-gone/cancel')

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body['status'] == 'cancelled'
    delete_mock.assert_awaited_once()


async def test_cancel_job_runtime_surfaces_other_k8s_errors_as_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-404 K8s API failures (RBAC denied, API server unreachable, …)
    must surface as 502 so the operator can act. Swallowing them would
    mask a misconfiguration and silently leave the Job running while the
    DB row says 'cancelled'."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-rbac', job_name='run-rbac')

    cfg, _delete_mock, client_mod, api_cls = _wire_k8s_mocks(
        delete_side_effect=ApiException(status=403, reason='Forbidden'),
    )
    with _sys_modules_patch(cfg, client_mod, api_cls):
        resp = _client.post('/initiatives/run-rbac/cancel')

    assert resp.status_code == 502
    assert 'Failed to delete initiative Job' in resp.json()['detail']


async def test_cancel_terminal_record_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second cancel on an already-cancelled run is a no-op — we must
    NOT re-delete the Job (it's already gone) and we must NOT 409. Returns
    200 with the existing terminal record."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    await _seed_job_record(run_id='run-already', job_name='run-already', status='cancelled')

    cfg, delete_mock, client_mod, api_cls = _wire_k8s_mocks()
    with _sys_modules_patch(cfg, client_mod, api_cls):
        resp = _client.post('/initiatives/run-already/cancel')

    assert resp.status_code == 200
    assert resp.json()['status'] == 'cancelled'
    # Crucial: the K8s API must NOT be touched for a terminal row.
    delete_mock.assert_not_awaited()
    cfg.load_incluster_config.assert_not_called()


async def test_cancel_legacy_asyncio_record_without_job_name_500s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy DB row with ``runtime='asyncio'`` (created before Phase F)
    has no ``job_name`` set. Cancelling it surfaces a clear 500 — there's
    no K8s Job to delete and the orphan reconciler will pick the record up
    on the next sweep."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    record = InitiativeRecord(
        id='run-asyncio-legacy',
        initiative='demo',
        status='running',
        started_at=datetime.now(UTC),
        runtime='asyncio',
        job_name=None,
    )
    await register(record)

    cfg, delete_mock, client_mod, api_cls = _wire_k8s_mocks()
    with _sys_modules_patch(cfg, client_mod, api_cls):
        resp = _client.post('/initiatives/run-asyncio-legacy/cancel')

    assert resp.status_code == 500
    assert 'missing job_name' in resp.json()['detail']
    delete_mock.assert_not_awaited()
    cfg.load_incluster_config.assert_not_called()
