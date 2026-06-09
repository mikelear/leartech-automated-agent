"""V5 D2.1 — integration test for the stale-progress detector wiring.

End-to-end mock-based integration test:

  1. Register an in-flight initiative run via ``app.state.register``
     (in-memory backend — same code path the API pod uses in DB-less
     test mode).
  2. Mock ``kubernetes_asyncio`` to surface a backing Job with NO
     terminal condition + a backing pod stuck in ``ImagePullBackOff``
     for longer than the threshold.
  3. Call ``reconcile_once`` and assert the in-memory record flips
     to ``status='failed'`` with ``error='pod_stuck_ImagePullBackOff'``.

Simulates the "preview-deploy with deliberately-broken image tag" path
described in the initiative without needing a live K8s cluster — the
real preview deploy is exercised by the chart's preview-pipeline + the
``end2end`` cluster-side tier, which carries its own gate marks.

Marker: ``integration`` per ``pyproject.toml::tool.pytest.ini_options``
(tier 2 — mock-based integration). The detector's per-case behaviour
is covered exhaustively under ``unit`` in
``tests/agent/test_stale_progress_detector.py``; this module pins the
glue between ``app.state`` ↔ ``job_reconciler`` ↔ ``run_driver`` so a
future refactor that breaks the wiring (e.g. moves get_record/update
behind a different import path) surfaces here.

Memory: ``feedback_async_tests_need_event_not_sleep`` — no async sleep
used; all coordination is via explicit ``await`` ordering and the
threshold knob (``STALE_PROGRESS_THRESHOLD_S=1``) so the test never
flakes on CI runners under load.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.state as state_module
from app import db as db_module
from app.state import InitiativeRecord, register
from app.state import get as get_record_state
from gate.agent.job_reconciler import reconcile_once


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test runs against an isolated in-memory app.state store.

    Cleanup prevents row leakage between tests in the same module.
    Same pattern as ``test_run_driver_first_turn.py``.
    """
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()
    state_module._records.clear()


def _make_stuck_pod(reason: str = 'ImagePullBackOff') -> SimpleNamespace:
    """Pod object whose first container is waiting with the given reason."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name='runner-pod-x9'),
        status=SimpleNamespace(
            container_statuses=[
                SimpleNamespace(
                    state=SimpleNamespace(
                        waiting=SimpleNamespace(reason=reason),
                        terminated=None,
                    ),
                ),
            ],
            start_time=datetime.now(UTC) - timedelta(seconds=120),
        ),
    )


def _make_job(name: str) -> SimpleNamespace:
    """K8s Job with NO terminal condition — the pre-watchdog stall shape."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        status=SimpleNamespace(conditions=[]),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_broken_image_tag_short_circuits_run_to_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Headline integration: spawn a run, simulate a Job whose pod is
    stuck in ImagePullBackOff (the deliberately-broken-image-tag scenario
    from the initiative spec), call ``reconcile_once`` once, and verify
    the in-memory record flips to ``failed`` with the K8s reason tagged.

    Threshold set to 1s via env to keep the test runtime in the
    millisecond range — the run record's ``started_at`` is then set to
    5s ago so the threshold is unambiguously exceeded on the first
    pass. This mirrors the initiative's "use shorter threshold (e.g.
    60s) in the test deploy" guidance, just compressed further for
    pytest-scale latency.
    """
    monkeypatch.setenv('STALE_PROGRESS_THRESHOLD_S', '1')

    run_id = 'integration-broken-image-1'
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='broken-image-tag-demo',
            status='running',
            started_at=datetime.now(UTC) - timedelta(seconds=5),
            pr_repo='mikelear/example-svc',
            branch='agent/broken-image',
            runtime='job',
            job_name=run_id,
            # The headline invariant — agent never executed its first
            # turn because the container never even started pulling
            # the (broken) image. This is the unambiguous
            # "nothing happened yet" signal that the detector keys off.
            started_executing_at=None,
        )
    )

    batch = MagicMock()
    core = MagicMock()
    batch.list_namespaced_job = AsyncMock(
        return_value=SimpleNamespace(items=[_make_job(run_id)]),
    )
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_make_stuck_pod('ImagePullBackOff')]),
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        # Force the orphan-sweep branch to no-op — we're testing the
        # detector path, not the V5 D2 Job-deleted-externally path.
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=False),
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = core

        updates = await reconcile_once('jx-staging')

    assert updates >= 1, 'reconcile_once should have patched at least one row'

    # The in-memory record must now reflect the watchdog's verdict —
    # this is the contract that lets the API surface the verdict to
    # the operator without a separate K8s round-trip.
    final = await get_record_state(run_id)
    assert final is not None
    assert final.status == 'failed', f'Expected status=failed after watchdog short-circuit, got {final.status!r}'
    assert final.error == 'pod_stuck_ImagePullBackOff', f'Expected error tagged with K8s reason, got {final.error!r}'
    assert final.finished_at is not None, 'Watchdog must set finished_at on flip'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_integration_started_executing_at_set_protects_in_flight_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative-case integration. A run whose first SDK turn HAS fired
    (``started_executing_at`` is non-NULL) must NOT be flipped, even
    when the pod transiently reports an image-pull-failure waiting
    reason (which can happen if the pod object is stale by the time
    the reconciler polls — the container has actually moved past
    image-pull but the cached status hasn't refreshed yet).

    This pairs with the headline test to prove the V5 D2.2 column is
    the unambiguous "agent is alive" guard — without it the watchdog
    would false-fail healthy in-flight runs whenever K8s status was
    momentarily stale.
    """
    monkeypatch.setenv('STALE_PROGRESS_THRESHOLD_S', '1')

    run_id = 'integration-protected-in-flight-1'
    now = datetime.now(UTC)
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='in-flight-protected',
            status='running',
            started_at=now - timedelta(seconds=5),
            pr_repo='mikelear/example-svc',
            branch='agent/in-flight',
            runtime='job',
            job_name=run_id,
            # First turn DID fire — the V5 D2.2 guard kicks in.
            started_executing_at=now - timedelta(seconds=2),
        )
    )

    batch = MagicMock()
    core = MagicMock()
    batch.list_namespaced_job = AsyncMock(
        return_value=SimpleNamespace(items=[_make_job(run_id)]),
    )
    core.list_namespaced_pod = AsyncMock(
        return_value=SimpleNamespace(items=[_make_stuck_pod('ImagePullBackOff')]),
    )

    with (
        patch('gate.agent.job_reconciler.config') as mock_config,
        patch('gate.agent.job_reconciler.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_reconciler.client') as mock_client_mod,
        patch('gate.agent.job_reconciler.is_db_enabled', return_value=False),
    ):
        mock_config.load_incluster_config = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        mock_client_mod.BatchV1Api.return_value = batch
        mock_client_mod.CoreV1Api.return_value = core

        await reconcile_once('jx-staging')

    final = await get_record_state(run_id)
    assert final is not None
    assert final.status == 'running', f'In-flight run was false-failed by the watchdog: {final.status!r}'
    assert final.error is None
