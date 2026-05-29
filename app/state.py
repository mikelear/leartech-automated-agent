"""Run-state store for active and completed initiatives.

Write-through-DB when `LEARTECH_INITIATIVE_DB_DSN` is configured; falls
back to in-memory dict when not. The in-memory dict is always maintained
as a fast-path cache — so interim `update()` calls from background tasks
never race against an incomplete DB INSERT (the dict is updated first,
then the DB write follows).

Reads prefer DB when enabled (persistence across pod restarts); fall back
to `_records` when not (dev / CI / preview without Postgres).

`_tasks` is always in-memory — asyncio.Task objects cannot be persisted.

v2: pod restart leaves DB rows in 'running'/'queued'. `reconcile_orphaned_runs()`
is called on FastAPI startup and marks those rows 'orphaned' so API consumers
can detect the gap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_runs import (
    InitiativeRunRecord,
    create_run,
    get_run,
    list_in_flight_runs,
    list_runs,
    mark_orphaned_runs,
    update_run,
)

logger = logging.getLogger(__name__)


class InitiativeRecord(BaseModel):
    id: str
    initiative: str
    status: str = Field(description='queued | running | complete | failed | cancelled | orphaned | timed_out')
    started_at: datetime
    finished_at: datetime | None = None
    pr_number: int | None = None
    pr_repo: str | None = None
    turns: int | None = None
    cost_usd: float | None = None
    error: str | None = None
    cluster: str | None = None
    created_by: str | None = None
    # Phase D.4 — which spawn path created this run.
    # 'asyncio' (default): run lives inside the API pod's event loop.
    # 'job':                run lives in its own K8s Job pod (survives
    #                       API pod restarts). Set at register() time by
    #                       the router from LEARTECH_INITIATIVE_RUNTIME.
    runtime: str = 'asyncio'
    # K8s Job name when runtime='job' — equals run_id by D.3 contract.
    # None on the asyncio path.
    job_name: str | None = None


_records: dict[str, InitiativeRecord] = {}
_tasks: dict[str, asyncio.Task[Any]] = {}


def new_id() -> str:
    """Short opaque ID — not security-sensitive, just unique within the process."""
    return uuid.uuid4().hex[:12]


def now() -> datetime:
    return datetime.now(UTC)


def _run_record_to_initiative_record(run: InitiativeRunRecord) -> InitiativeRecord:
    """Convert a DB-layer run record to the API-facing pydantic model."""
    return InitiativeRecord(
        id=run.id,
        initiative=run.initiative,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        pr_number=run.pr_number,
        pr_repo=run.pr_repo,
        turns=run.turns,
        cost_usd=float(run.cost_usd) if run.cost_usd is not None else None,
        error=run.error,
        cluster=run.cluster,
        created_by=run.created_by,
        runtime=run.runtime,
        job_name=run.job_name,
    )


async def register(record: InitiativeRecord, task: asyncio.Task[Any] | None) -> None:
    """Register a new initiative run — in-memory always, DB when configured.

    The in-memory write happens first (no await), so background tasks that
    call update() immediately after creation never race against an incomplete
    DB INSERT.

    Phase D.4: ``task`` is optional. On the asyncio runtime path the caller
    passes the live asyncio.Task so cancellation can target it. On the Job
    runtime path the run lives in a separate K8s Job pod — there's no local
    Task to track, so the caller passes None and ``_tasks`` is left untouched
    for this run. Cancellation of Job-runtime runs flows through K8s (D.5
    will wire that surface).
    """
    _records[record.id] = record
    if task is not None:
        _tasks[record.id] = task
    if is_db_enabled():
        async with db_session() as s:
            await create_run(
                s,
                id=record.id,
                initiative=record.initiative,
                status=record.status,
                started_at=record.started_at,
                cluster=record.cluster,
                created_by=record.created_by,
                # pr_repo is known at register time (set by the router from
                # loaded.primary.qualified_repo). Persisting it at INSERT
                # rather than waiting for the completion update means a pod
                # restart mid-run still leaves the DB row with a usable
                # pr_repo for self_retrospect — fixes the skip-every-run
                # regression observed on run 44120e445abd (2026-05-28).
                pr_repo=record.pr_repo,
                # Phase D.4 — dual-path runtime fields, set once at INSERT
                # and never mutated afterwards.
                runtime=record.runtime,
                job_name=record.job_name,
            )


async def get(initiative_id: str) -> InitiativeRecord | None:
    """Retrieve run state — from DB when configured, in-memory fallback otherwise."""
    if is_db_enabled():
        async with db_session() as s:
            run = await get_run(s, initiative_id)
            if run is None:
                return None
            return _run_record_to_initiative_record(run)
    return _records.get(initiative_id)


async def update(initiative_id: str, **fields: Any) -> None:
    """Partial update — updates in-memory dict first, then DB if configured.

    The in-memory update is synchronous (no await) so callers in a background
    task see the change immediately even while a concurrent DB write is in
    flight.
    """
    rec = _records.get(initiative_id)
    if rec is not None:
        _records[initiative_id] = rec.model_copy(update=fields)
    if is_db_enabled():
        async with db_session() as s:
            await update_run(s, id=initiative_id, **fields)


async def cancel(initiative_id: str) -> bool:
    """Request cancellation of a running task. Returns True if cancelled."""
    task = _tasks.get(initiative_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


async def list_records() -> list[InitiativeRecord]:
    """List all run records — from DB when configured, in-memory fallback otherwise."""
    if is_db_enabled():
        async with db_session() as s:
            runs = await list_runs(s)
            return [_run_record_to_initiative_record(r) for r in runs]
    return list(_records.values())


async def reconcile_orphaned_runs() -> int:
    """Mark in-flight DB runs as 'orphaned' when no live execution backs them.

    Called on FastAPI startup. A pod restart leaves DB rows in 'running' or
    'queued' state; this function rectifies the state so API consumers can
    detect the gap and act accordingly.

    Liveness is determined per-runtime:

    - ``runtime='asyncio'``: row is live iff its id is in the in-memory
      ``_tasks`` dict. On a fresh pod start that dict is empty, so any
      asyncio-mode in-flight rows are correctly orphaned.
    - ``runtime='job'``: row is live iff K8s reports a Job in ``POD_NAMESPACE``
      labelled ``leartech.io/run-id=<id>``. This was added 2026-05-29 after
      the D.5.2 fire: when the API pod rolls mid-Job, the new pod's
      ``_tasks`` is empty BUT the Job pod is still alive and making
      progress. Marking such records orphaned creates a stale catalog
      verdict (DB says ``orphaned`` while the Job completes successfully
      and opens a PR).

    K8s API failures are treated conservatively — we DO NOT orphan
    job-runtime records when we cannot verify Job liveness. They will be
    re-evaluated on the next startup. False-orphaning a live Job is
    a worse outcome than briefly delaying orphan detection.

    Returns the count of rows marked orphaned (0 when DB is not configured).
    """
    if not is_db_enabled():
        return 0

    live_ids: set[str] = set(_tasks.keys())

    # Enumerate in-flight job-runtime candidates so we can ask K8s about each.
    # Asyncio-runtime rows skip this entirely — they have no Job to check.
    async with db_session() as s:
        in_flight = await list_in_flight_runs(s)
    job_candidates = [r for r in in_flight if r.runtime == 'job' and r.id not in live_ids]

    for record in job_candidates:
        try:
            if await _job_exists_for_run(record.id):
                live_ids.add(record.id)
        except Exception as exc:  # noqa: BLE001 — K8s failures are diverse
            # Conservative: don't orphan when we can't verify. Re-evaluated
            # on next reconcile cycle. See module docstring for rationale.
            logger.warning(
                'K8s Job-existence check failed for runtime=job run %s (%s); '
                'conservatively treating as live to avoid false orphan. '
                'Will be re-evaluated on the next startup reconcile.',
                record.id,
                exc,
            )
            live_ids.add(record.id)

    async with db_session() as s:
        return await mark_orphaned_runs(s, live_ids)


async def _job_exists_for_run(run_id: str) -> bool:
    """Return True if a K8s Job labelled ``leartech.io/run-id=<run_id>`` exists
    in ``POD_NAMESPACE``.

    Raises ``RuntimeError`` when ``POD_NAMESPACE`` is unset — caller's
    conservative fallback (don't orphan) then kicks in. In production the
    chart injects POD_NAMESPACE via fieldRef metadata.namespace; this
    branch protects against misconfigured deploys.

    NOTE: ``kubernetes_asyncio.config.load_incluster_config`` is synchronous
    in this library — do NOT await it. Same pattern as the logs/cancel
    endpoints; see PR #50 + the
    ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.
    """
    namespace = os.environ.get('POD_NAMESPACE')
    if not namespace:
        raise RuntimeError(
            'POD_NAMESPACE env var is required to verify live K8s Jobs '
            'for runtime=job runs during orphan reconciliation.'
        )

    # Late import: kubernetes_asyncio is only needed when reconciling
    # runtime=job records. Keeps `from app.state import ...` cheap for
    # in-process tests that don't exercise the Job path.
    from kubernetes_asyncio import client as k8s_client
    from kubernetes_asyncio import config as k8s_config
    from kubernetes_asyncio.client.api_client import ApiClient

    k8s_config.load_incluster_config()  # synchronous — do NOT await
    async with ApiClient() as api:
        batch = k8s_client.BatchV1Api(api)
        jobs = await batch.list_namespaced_job(
            namespace=namespace,
            label_selector=f'leartech.io/run-id={run_id}',
        )
    return bool(jobs.items)
