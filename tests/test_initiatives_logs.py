"""Tests for D.6 — GET /initiatives/{id}/logs endpoint.

Coverage matrix:

1. **404** when initiative_id is unknown.
2. **Happy path** — mocked K8s client returns pod log text; response
   carries the body with content-type text/plain.
3. **tail_lines query param** is forwarded to ``read_namespaced_pod_log``
   so operators can ask for arbitrarily long log tails.
4. **No pod found** — no matching pod returns 404 (Job pod GC'd, or pod
   hasn't been scheduled yet).
5. **POD_NAMESPACE missing** — 500 with a clear chart-deployment hint.

Phase F: every run is runtime='job' so the historical 501-for-asyncio
branch is gone. Legacy asyncio records that happen to be in the DB
flow through the same K8s lookup; they return 404 (no matching pod).

K8s is NOT exercised here; everything routes through unittest.mock.patch
against the lazily-imported ``kubernetes_asyncio.{client,config}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import InitiativeRecord, register

client = TestClient(app)


def _make_record(*, initiative_id: str, runtime: str) -> InitiativeRecord:
    return InitiativeRecord(
        id=initiative_id,
        initiative='test-initiative',
        status='complete',
        started_at=datetime.now(UTC),
        runtime=runtime,
        job_name=initiative_id if runtime == 'job' else None,
    )


def test_get_logs_returns_404_when_unknown() -> None:
    response = client.get('/initiatives/unknown-id-xyz/logs')
    assert response.status_code == 404
    assert 'unknown-id-xyz' in response.json()['detail']


async def test_get_logs_happy_path_returns_text_plain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the K8s client; the endpoint returns the log body as text/plain."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    record = _make_record(initiative_id='job-run-bbb', runtime='job')
    await register(record)

    log_text = 'agent boot\n--- turns=2  in=10  out=20  cost=$0.01\nPR opened: https://github.com/owner/repo/pull/777\n'

    core_mock = MagicMock()
    core_mock.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name='job-run-bbb-abc12'))],
        ),
    )
    core_mock.read_namespaced_pod_log = AsyncMock(return_value=log_text)

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.CoreV1Api.return_value = core_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    with (
        patch.dict(
            'sys.modules',
            {
                'kubernetes_asyncio': MagicMock(client=k8s_client_mock, config=k8s_config_mock),
                'kubernetes_asyncio.client': k8s_client_mock,
                'kubernetes_asyncio.config': k8s_config_mock,
                'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_client_cls_mock),
            },
        ),
    ):
        response = client.get('/initiatives/job-run-bbb/logs')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/plain')
    assert response.text == log_text
    # load_incluster_config is synchronous — must be called, not awaited.
    k8s_config_mock.load_incluster_config.assert_called_once()
    core_mock.list_namespaced_pod.assert_awaited_once_with(
        namespace='jx-staging',
        label_selector='leartech.io/run-id=job-run-bbb',
    )


async def test_get_logs_forwards_tail_lines_to_k8s(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``tail_lines`` query param must flow through to read_namespaced_pod_log."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    record = _make_record(initiative_id='job-run-ccc', runtime='job')
    await register(record)

    core_mock = MagicMock()
    core_mock.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name='job-run-ccc-xyz'))],
        ),
    )
    core_mock.read_namespaced_pod_log = AsyncMock(return_value='ok\n')

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.CoreV1Api.return_value = core_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    with patch.dict(
        'sys.modules',
        {
            'kubernetes_asyncio': MagicMock(client=k8s_client_mock, config=k8s_config_mock),
            'kubernetes_asyncio.client': k8s_client_mock,
            'kubernetes_asyncio.config': k8s_config_mock,
            'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_client_cls_mock),
        },
    ):
        response = client.get('/initiatives/job-run-ccc/logs?tail_lines=1500')

    assert response.status_code == 200
    core_mock.read_namespaced_pod_log.assert_awaited_once_with(
        name='job-run-ccc-xyz',
        namespace='jx-staging',
        tail_lines=1500,
    )


async def test_get_logs_returns_404_when_no_pod_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Job pod GC'd (TTL fired) or pod scheduling not yet complete — surface as 404."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    record = _make_record(initiative_id='job-run-ddd', runtime='job')
    await register(record)

    core_mock = MagicMock()
    core_mock.list_namespaced_pod = AsyncMock(return_value=SimpleNamespace(items=[]))

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.CoreV1Api.return_value = core_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    with patch.dict(
        'sys.modules',
        {
            'kubernetes_asyncio': MagicMock(client=k8s_client_mock, config=k8s_config_mock),
            'kubernetes_asyncio.client': k8s_client_mock,
            'kubernetes_asyncio.config': k8s_config_mock,
            'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_client_cls_mock),
        },
    ):
        response = client.get('/initiatives/job-run-ddd/logs')

    assert response.status_code == 404
    assert 'job-run-ddd' in response.json()['detail']


async def test_get_logs_returns_500_when_pod_namespace_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without POD_NAMESPACE we can't address the right namespace — fail loud."""
    monkeypatch.delenv('POD_NAMESPACE', raising=False)
    record = _make_record(initiative_id='job-run-eee', runtime='job')
    await register(record)

    response = client.get('/initiatives/job-run-eee/logs')
    assert response.status_code == 500
    assert 'POD_NAMESPACE' in response.json()['detail']
