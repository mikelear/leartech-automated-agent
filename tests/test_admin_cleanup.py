"""Tests for the Phase B admin cleanup endpoint + module.

Coverage matrix:

1. Auth gate — no header / wrong header → 401; correct header → 200.
2. Auth gate — server-side env unset returns 401 even with a header
   (closed-by-default invariant).
3. Happy path — both reconcile and pipelinerun-cancel return values are
   surfaced verbatim in the JSON summary.
4. Orphan detection — stuck rows whose Job has been Pending for >24h
   AND has no live K8s Job get marked ``orphaned``; younger rows are
   left alone (age filter wired through).
5. PipelineRun cancellation — only superseded PRs (label SHA !=
   PR HEAD) get the cancel patch; PRs whose head matches stay live;
   already-terminal Runs are skipped without a patch call.
6. POD_NAMESPACE missing → 500 with a clear chart-deployment hint.

K8s is never touched. ``kubernetes_asyncio.{client,config}`` is patched
into ``sys.modules`` exactly the way ``tests/test_initiatives_logs.py``
and ``tests/test_state_orphan_respect_job.py`` do it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.state as state_module
from app import db as db_module
from app.db.initiative_runs import create_run, get_run
from app.db.models import Base
from app.main import app
from app.routers.admin import ADMIN_TOKEN_ENV

_client = TestClient(app)

_TOKEN = 'sek-rit-admin-token'  # noqa: S105 — test-only token, not a real secret


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """In-memory SQLite for DB-backed state.

    Mirrors ``test_state_orphan_respect_job.py``. Clears module-level
    state on entry + exit so each test starts fresh.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    state_module._records.clear()
    state_module._tasks.clear()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await db_module.dispose_engine()
    db_module._reset_for_tests()
    state_module._records.clear()
    state_module._tasks.clear()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set POD_NAMESPACE + LEARTECH_ADMIN_TOKEN for the default-happy tests.

    Tests that need to assert the closed-by-default behaviour call
    ``monkeypatch.delenv(...)`` themselves before invoking the endpoint.
    """
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv(ADMIN_TOKEN_ENV, _TOKEN)


# ─── K8s mock builders ──────────────────────────────────────────────────


def _wire_pipelineruns_mock(
    items: list[dict[str, Any]] | None = None,
    *,
    list_side_effect: object = None,
    patch_side_effect: object = None,
) -> tuple[MagicMock, AsyncMock, AsyncMock, MagicMock, MagicMock]:
    """Build kubernetes_asyncio mocks for the PipelineRun list+patch path.

    Returns (k8s_config_mock, list_mock, patch_mock, k8s_client_mock,
    api_client_cls_mock). The caller wraps the ``patch.dict('sys.modules', ...)``
    context themselves.
    """
    if list_side_effect is not None:
        list_mock = AsyncMock(side_effect=list_side_effect)
    else:
        list_mock = AsyncMock(return_value={'items': items or []})

    if patch_side_effect is not None:
        patch_mock = AsyncMock(side_effect=patch_side_effect)
    else:
        patch_mock = AsyncMock(return_value={})

    custom_mock = MagicMock()
    custom_mock.list_namespaced_custom_object = list_mock
    custom_mock.patch_namespaced_custom_object = patch_mock

    api_client_cls_mock = MagicMock()
    api_client_cls_mock.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    api_client_cls_mock.return_value.__aexit__ = AsyncMock(return_value=None)

    k8s_client_mock = MagicMock()
    k8s_client_mock.CustomObjectsApi.return_value = custom_mock

    k8s_config_mock = MagicMock()
    k8s_config_mock.load_incluster_config = MagicMock()

    return k8s_config_mock, list_mock, patch_mock, k8s_client_mock, api_client_cls_mock


def _sys_modules_patch(cfg: MagicMock, client_mod: MagicMock, api_cls: MagicMock) -> Any:
    return patch.dict(
        'sys.modules',
        {
            'kubernetes_asyncio': MagicMock(client=client_mod, config=cfg),
            'kubernetes_asyncio.client': client_mod,
            'kubernetes_asyncio.config': cfg,
            'kubernetes_asyncio.client.api_client': MagicMock(ApiClient=api_cls),
        },
    )


def _make_pipelinerun(
    name: str,
    *,
    pull: str,
    last_sha: str,
    terminal_reason: str | None = None,
) -> dict[str, Any]:
    """Construct a minimal PipelineRun dict shape matching the K8s API.

    Only the fields ``cleanup.cancel_superseded_pipelineruns`` reads are
    populated. ``terminal_reason``, when set, adds a ``status.conditions``
    entry so the test can pin the "already-terminal → skip" branch.
    """
    pr: dict[str, Any] = {
        'metadata': {
            'name': name,
            'labels': {
                'lighthouse.jenkins-x.io/refs.repo': 'mikelear/leartech-automated-agent',
                'lighthouse.jenkins-x.io/refs.pull': pull,
                'lighthouse.jenkins-x.io/lastCommitSHA': last_sha,
            },
        },
    }
    if terminal_reason is not None:
        pr['status'] = {
            'conditions': [
                {'type': 'Succeeded', 'status': 'True', 'reason': terminal_reason},
            ],
        }
    return pr


# ─── 1. Auth ────────────────────────────────────────────────────────────


def test_admin_cleanup_no_header_returns_401() -> None:
    resp = _client.post('/admin/cleanup')
    assert resp.status_code == 401
    assert resp.json()['detail'] == 'invalid admin token'


def test_admin_cleanup_wrong_header_returns_401() -> None:
    resp = _client.post('/admin/cleanup', headers={'X-Admin-Token': 'not-the-token'})
    assert resp.status_code == 401


def test_admin_cleanup_unset_server_env_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Closed-by-default: an unconfigured server-side env rejects every
    request, even when the client provides a (valid-looking) header."""
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    resp = _client.post('/admin/cleanup', headers={'X-Admin-Token': 'anything'})
    assert resp.status_code == 401


# ─── 2. POD_NAMESPACE missing ───────────────────────────────────────────


def test_admin_cleanup_pod_namespace_missing_returns_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('POD_NAMESPACE', raising=False)
    resp = _client.post('/admin/cleanup', headers={'X-Admin-Token': _TOKEN})
    assert resp.status_code == 500
    assert 'POD_NAMESPACE' in resp.json()['detail']


# ─── 3. Happy path — both counters surfaced ─────────────────────────────


def test_admin_cleanup_happy_path_returns_summary() -> None:
    """With both helper functions mocked, the endpoint returns the JSON
    summary with the counters exactly as the helpers returned them."""
    with (
        patch('app.routers.admin.reconcile_orphaned_runs', new=AsyncMock(return_value=4)),
        patch('app.routers.admin.cancel_superseded_pipelineruns', new=AsyncMock(return_value=2)),
    ):
        resp = _client.post(
            '/admin/cleanup',
            headers={'X-Admin-Token': _TOKEN},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {'stuck_runs_marked': 4, 'pipelineruns_cancelled': 2}


def test_admin_cleanup_forwards_older_than_seconds_to_reconcile() -> None:
    """The ``older_than_seconds`` query param flows through to the
    reconcile call so operators can sweep stale state more aggressively
    when needed."""
    reconcile_mock = AsyncMock(return_value=0)
    with (
        patch('app.routers.admin.reconcile_orphaned_runs', new=reconcile_mock),
        patch('app.routers.admin.cancel_superseded_pipelineruns', new=AsyncMock(return_value=0)),
    ):
        resp = _client.post(
            '/admin/cleanup?older_than_seconds=3600',
            headers={'X-Admin-Token': _TOKEN},
        )
    assert resp.status_code == 200, resp.text
    reconcile_mock.assert_awaited_once_with(older_than_seconds=3600)


# ─── 4. Orphan detection respects the age filter ────────────────────────


@pytest.mark.asyncio
async def test_reconcile_age_filter_protects_recent_records(
    db_enabled: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime=asyncio row younger than the cutoff must NOT be orphaned
    even when no live Task backs it — the age filter keeps the operator
    cleanup endpoint from sweeping legitimately mid-flight rows."""
    from app.state import reconcile_orphaned_runs

    now = datetime.now(UTC)
    async with db_module.session() as s:
        await create_run(
            s,
            id='young',
            initiative='demo',
            status='running',
            started_at=now - timedelta(seconds=60),  # 1 min old
        )
        await create_run(
            s,
            id='old',
            initiative='demo',
            status='running',
            started_at=now - timedelta(days=2),  # 2 days old
        )

    # 24-hour cutoff → 'young' is protected, 'old' is orphaned.
    count = await reconcile_orphaned_runs(older_than_seconds=86400)
    assert count == 1

    async with db_module.session() as s:
        young = await get_run(s, 'young')
        old = await get_run(s, 'old')
    assert young is not None and young.status == 'running'
    assert old is not None and old.status == 'orphaned'


@pytest.mark.asyncio
async def test_reconcile_default_no_age_filter_sweeps_every_stale_row(
    db_enabled: None,
) -> None:
    """Backwards-compatibility: when ``older_than_seconds`` is None the
    function preserves the historical behaviour (every in-flight row
    without backing is orphaned). Pinned because the startup callsite
    relies on this."""
    from app.state import reconcile_orphaned_runs

    async with db_module.session() as s:
        await create_run(
            s,
            id='whatever-age',
            initiative='demo',
            status='running',
            started_at=datetime.now(UTC),
        )

    count = await reconcile_orphaned_runs()
    assert count == 1


# ─── 5. PipelineRun cancellation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_superseded_only_patches_when_sha_diverges() -> None:
    """Two PipelineRuns: one whose label SHA matches the PR HEAD (live —
    must NOT be patched), one whose SHA diverges (superseded — must be
    patched). The returned count is 1."""
    from gate.admin import cleanup as cleanup_module

    items = [
        _make_pipelinerun('live-run', pull='1', last_sha='aaa'),
        _make_pipelinerun('stale-run', pull='2', last_sha='zzz'),
    ]
    cfg, list_mock, patch_mock, client_mod, api_cls = _wire_pipelineruns_mock(items)

    # Mock the gh-api shellout: PR #1 has head=aaa (matches → live);
    # PR #2 has head=bbb (diverges from label → superseded).
    def _fake_head(repo: str, pr_number: str) -> str:
        return {'1': 'aaa', '2': 'bbb'}[pr_number]

    with (
        patch.object(cleanup_module, '_pr_head_sha', side_effect=_fake_head),
        _sys_modules_patch(cfg, client_mod, api_cls),
    ):
        cancelled = await cleanup_module.cancel_superseded_pipelineruns(namespace='jx-staging')

    assert cancelled == 1
    patch_mock.assert_awaited_once()
    call_kwargs = patch_mock.await_args.kwargs
    assert call_kwargs['name'] == 'stale-run'
    assert call_kwargs['namespace'] == 'jx-staging'
    assert call_kwargs['plural'] == 'pipelineruns'
    assert call_kwargs['body'] == {'spec': {'status': 'PipelineRunCancelled'}}
    # List was scoped to the repo label.
    list_kwargs = list_mock.await_args.kwargs
    assert list_kwargs['label_selector'] == 'lighthouse.jenkins-x.io/refs.repo=mikelear/leartech-automated-agent'


@pytest.mark.asyncio
async def test_cancel_superseded_skips_terminal_runs() -> None:
    """A PipelineRun already in a terminal Tekton condition (Succeeded /
    Failed / PipelineRunCancelled) must not be patched — submitting the
    cancel body against a terminal Run is wasted API noise."""
    from gate.admin import cleanup as cleanup_module

    items = [
        _make_pipelinerun('done-run', pull='5', last_sha='aaa', terminal_reason='Succeeded'),
    ]
    cfg, _list_mock, patch_mock, client_mod, api_cls = _wire_pipelineruns_mock(items)

    with (
        patch.object(cleanup_module, '_pr_head_sha', return_value='bbb'),
        _sys_modules_patch(cfg, client_mod, api_cls),
    ):
        cancelled = await cleanup_module.cancel_superseded_pipelineruns(namespace='jx-staging')

    assert cancelled == 0
    patch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_superseded_skips_when_pr_head_unresolved() -> None:
    """If gh api returns no head SHA (transient failure / 404 / token
    revoked), we treat the run as live and skip the patch. Wrongful
    cancellation of a healthy run is the worst outcome — better to
    leave it for the next sweep."""
    from gate.admin import cleanup as cleanup_module

    items = [_make_pipelinerun('maybe-stale', pull='9', last_sha='aaa')]
    cfg, _list_mock, patch_mock, client_mod, api_cls = _wire_pipelineruns_mock(items)

    with (
        patch.object(cleanup_module, '_pr_head_sha', return_value=None),
        _sys_modules_patch(cfg, client_mod, api_cls),
    ):
        cancelled = await cleanup_module.cancel_superseded_pipelineruns(namespace='jx-staging')

    assert cancelled == 0
    patch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_superseded_continues_when_one_patch_fails() -> None:
    """A patch failure on one PipelineRun is logged but does not abort
    the sweep — the other superseded runs in the list must still be
    processed. Pins the in-loop ``try/except`` contract."""
    from gate.admin import cleanup as cleanup_module

    items = [
        _make_pipelinerun('first-stale', pull='1', last_sha='zzz'),
        _make_pipelinerun('second-stale', pull='2', last_sha='zzz'),
    ]

    # First patch raises, second succeeds.
    side_effects: list[Any] = [RuntimeError('boom'), {}]
    cfg, _list_mock, patch_mock, client_mod, api_cls = _wire_pipelineruns_mock(
        items,
        patch_side_effect=side_effects,
    )

    with (
        patch.object(cleanup_module, '_pr_head_sha', return_value='aaa'),
        _sys_modules_patch(cfg, client_mod, api_cls),
    ):
        cancelled = await cleanup_module.cancel_superseded_pipelineruns(namespace='jx-staging')

    assert cancelled == 1
    assert patch_mock.await_count == 2
