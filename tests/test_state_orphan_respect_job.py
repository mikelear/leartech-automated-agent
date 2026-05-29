"""Tests for the Phase D loose-end fix (2026-05-29): orphan detection must
respect live K8s Jobs.

Background — D.5.2 fire: when the API pod rolls during a runtime=job run's
execution, the new pod's startup ``reconcile_orphaned_runs`` hook marked the
run 'orphaned' even though the K8s Job was still alive and making progress.
The Job then completed successfully (DB row showed 'orphaned' but the run
opened a PR), so the catalog verdict disagreed with reality.

Fix: before marking a non-terminal runtime=job record orphaned, query K8s
for a live Job with matching ``leartech.io/run-id=<id>`` label. Only when
no such Job exists is the row truly orphaned. Asyncio-runtime records
continue to use the historical in-memory ``_tasks`` check.

K8s is NOT exercised here; everything routes through
``patch.dict('sys.modules', ...)`` against the lazily-imported
``kubernetes_asyncio.{client,config}`` — same pattern as
``test_initiatives_cancel_job.py`` (D.7) and ``test_initiatives_logs.py`` (D.6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

import app.state as state_module
from app import db as db_module
from app.db.initiative_runs import create_run, get_run
from app.db.models import Base
from app.state import reconcile_orphaned_runs


def _started_at() -> datetime:
    return datetime.now(UTC)


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Enable the DB for state.py tests using an in-memory SQLite engine.

    Mirrors the fixture in ``test_initiative_runs_db.py``. Clears module
    state on entry + exit so each test starts with a fresh _records dict.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    state_module._records.clear()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await db_module.dispose_engine()
    db_module._reset_for_tests()
    state_module._records.clear()


def _wire_k8s_mocks(
    *, list_jobs_side_effect: object = None, jobs_items: list[Any] | None = None
) -> tuple[MagicMock, AsyncMock, MagicMock, MagicMock]:
    """Build the mock objects for ``kubernetes_asyncio.BatchV1Api.list_namespaced_job``.

    Returns (k8s_config_mock, list_mock, k8s_client_mock, api_client_cls_mock).
    The caller wraps the ``patch.dict('sys.modules', ...)`` context themselves.

    Either ``list_jobs_side_effect`` (raises) or ``jobs_items`` (returns a
    SimpleNamespace with ``items=...``) drives the mock. When neither is
    given, the call returns an empty ``items`` list.
    """
    if list_jobs_side_effect is not None:
        list_mock: AsyncMock = AsyncMock(side_effect=list_jobs_side_effect)
    else:
        items = jobs_items if jobs_items is not None else []
        list_mock = AsyncMock(return_value=SimpleNamespace(items=items))

    batch_mock = MagicMock()
    batch_mock.list_namespaced_job = list_mock

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.BatchV1Api.return_value = batch_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    return k8s_config_mock, list_mock, k8s_client_mock, api_client_cls_mock


def _sys_modules_patch(k8s_config_mock: MagicMock, k8s_client_mock: MagicMock, api_client_cls_mock: MagicMock) -> Any:
    """Return a context manager that patches kubernetes_asyncio in sys.modules.

    Matches the pattern test_initiatives_cancel_job.py uses for the same
    lazily-imported library.
    """
    return patch.dict(
        'sys.modules',
        {
            'kubernetes_asyncio': MagicMock(client=k8s_client_mock, config=k8s_config_mock),
            'kubernetes_asyncio.client': k8s_client_mock,
            'kubernetes_asyncio.config': k8s_config_mock,
            'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_client_cls_mock),
        },
    )


# ─── Job-mode record + live K8s Job → NOT marked orphaned ───────────────


@pytest.mark.asyncio
async def test_job_mode_with_live_k8s_job_is_not_orphaned(db_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinned regression for the D.5.2 fire: a runtime=job row with a live
    K8s Job in POD_NAMESPACE must NOT be marked orphaned, even though
    _tasks is empty (the API pod just restarted)."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    async with db_module.session() as s:
        await create_run(
            s,
            id='job-live-1',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='job',
            job_name='job-live-1',
        )

    # K8s reports one Job present for that run-id.
    cfg, list_mock, client_mod, api_cls = _wire_k8s_mocks(
        jobs_items=[SimpleNamespace(metadata=SimpleNamespace(name='job-live-1'))],
    )
    with _sys_modules_patch(cfg, client_mod, api_cls):
        count = await reconcile_orphaned_runs()

    assert count == 0, 'live Job-mode run must not be marked orphaned'
    list_mock.assert_awaited_once()
    call_kwargs = list_mock.await_args.kwargs
    assert call_kwargs['namespace'] == 'jx-staging'
    assert call_kwargs['label_selector'] == 'leartech.io/run-id=job-live-1'

    async with db_module.session() as s:
        rec = await get_run(s, 'job-live-1')
    assert rec is not None
    assert rec.status == 'running', 'status must remain running while Job is alive'


# ─── Job-mode record + no K8s Job → marked orphaned ─────────────────────


@pytest.mark.asyncio
async def test_job_mode_with_no_k8s_job_is_marked_orphaned(db_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A runtime=job row with NO matching K8s Job is truly orphaned (the
    Job pod completed-and-was-GC'd, or never existed) and must be marked
    orphaned so callers can detect the gap."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    async with db_module.session() as s:
        await create_run(
            s,
            id='job-gone-1',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='job',
            job_name='job-gone-1',
        )

    # K8s returns an empty items list — no Job exists for that run-id.
    cfg, list_mock, client_mod, api_cls = _wire_k8s_mocks(jobs_items=[])
    with _sys_modules_patch(cfg, client_mod, api_cls):
        count = await reconcile_orphaned_runs()

    assert count == 1
    list_mock.assert_awaited_once()

    async with db_module.session() as s:
        rec = await get_run(s, 'job-gone-1')
    assert rec is not None and rec.status == 'orphaned'


# ─── K8s API failure → conservative behaviour ───────────────────────────


@pytest.mark.asyncio
async def test_k8s_api_failure_does_not_orphan_job_runtime_records(
    db_enabled: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If the K8s list_namespaced_job call raises (RBAC, network blip,
    API server unreachable…), we must NOT false-orphan the row. The next
    reconcile cycle will re-evaluate. A warning is logged so operators
    can see the deferred state."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    async with db_module.session() as s:
        await create_run(
            s,
            id='job-k8s-err-1',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='job',
            job_name='job-k8s-err-1',
        )

    cfg, _list_mock, client_mod, api_cls = _wire_k8s_mocks(
        list_jobs_side_effect=RuntimeError('K8s API unreachable'),
    )
    with _sys_modules_patch(cfg, client_mod, api_cls), caplog.at_level('WARNING', logger='app.state'):
        count = await reconcile_orphaned_runs()

    assert count == 0, 'job-runtime record must not be orphaned when K8s check fails'

    async with db_module.session() as s:
        rec = await get_run(s, 'job-k8s-err-1')
    assert rec is not None and rec.status == 'running'

    # And the operator gets a warning that explains the deferred decision.
    assert any('K8s Job-existence check failed' in r.message for r in caplog.records), (
        'expected a warning about deferred orphan-detection on K8s failure'
    )


# ─── Asyncio-mode record (no runtime, no job_name) → existing path ──────


@pytest.mark.asyncio
async def test_legacy_asyncio_mode_record_orphaned_without_k8s(db_enabled: None) -> None:
    """Legacy DB rows with runtime='asyncio' (created before Phase F)
    must not trigger any K8s calls — there's no backing Job to check —
    and must always be orphaned. The API pod that owned their task is
    gone; the row is stale by definition."""
    async with db_module.session() as s:
        await create_run(
            s,
            id='asyncio-stale-1',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='asyncio',  # legacy pre-Phase-F row
        )

    # K8s mocks are wired but should NOT be invoked — non-'job' runtime
    # never reaches the Job-existence check.
    cfg, list_mock, client_mod, api_cls = _wire_k8s_mocks(jobs_items=[])
    with _sys_modules_patch(cfg, client_mod, api_cls):
        count = await reconcile_orphaned_runs()

    assert count == 1
    list_mock.assert_not_awaited()
    cfg.load_incluster_config.assert_not_called()

    async with db_module.session() as s:
        rec = await get_run(s, 'asyncio-stale-1')
    assert rec is not None and rec.status == 'orphaned'


# ─── Mixed asyncio + job records ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_runtime_records_route_correctly(db_enabled: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity check: a mix of legacy asyncio + Phase-F job records,
    where only the live job-mode one has a backing K8s Job, results in:
      - legacy asyncio-mode row → orphaned (no K8s Job lookup)
      - job-mode w/ live Job → still running
      - job-mode w/o live Job → orphaned
    """
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    async with db_module.session() as s:
        await create_run(
            s,
            id='mix-asyncio',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='asyncio',  # legacy pre-Phase-F row
        )
        await create_run(
            s,
            id='mix-job-alive',
            initiative='demo',
            status='running',
            started_at=_started_at(),
            runtime='job',
            job_name='mix-job-alive',
        )
        await create_run(
            s,
            id='mix-job-gone',
            initiative='demo',
            status='queued',
            started_at=_started_at(),
            runtime='job',
            job_name='mix-job-gone',
        )

    async def list_jobs_only_alive(**kwargs: object) -> SimpleNamespace:
        # The selector is per-record; return a hit only for 'mix-job-alive'.
        sel = kwargs.get('label_selector', '')
        if sel == 'leartech.io/run-id=mix-job-alive':
            return SimpleNamespace(items=[SimpleNamespace(metadata=SimpleNamespace(name='mix-job-alive'))])
        return SimpleNamespace(items=[])

    cfg, list_mock, client_mod, api_cls = _wire_k8s_mocks()
    list_mock.side_effect = list_jobs_only_alive

    with _sys_modules_patch(cfg, client_mod, api_cls):
        count = await reconcile_orphaned_runs()

    assert count == 2  # asyncio one + the gone-Job one

    async with db_module.session() as s:
        assert (await get_run(s, 'mix-asyncio')).status == 'orphaned'  # type: ignore[union-attr]
        assert (await get_run(s, 'mix-job-alive')).status == 'running'  # type: ignore[union-attr]
        assert (await get_run(s, 'mix-job-gone')).status == 'orphaned'  # type: ignore[union-attr]
