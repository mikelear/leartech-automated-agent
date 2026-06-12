"""Run-state store for active and completed initiatives.

Write-through-DB when `LEARTECH_INITIATIVE_DB_DSN` is configured; falls
back to in-memory dict when not. The in-memory dict is always maintained
as a fast-path cache — so interim `update()` calls never race against an
incomplete DB INSERT (the dict is updated first, then the DB write
follows).

Reads prefer DB when enabled (persistence across pod restarts); fall back
to `_records` when not (dev / CI / preview without Postgres).

Phase F: every run is a K8s Job; the in-process asyncio.Task path was
removed. Liveness for orphan detection is determined purely by K8s
(`_job_exists_for_run`).

Pod restart leaves DB rows in 'running'/'queued'.
`reconcile_orphaned_runs()` is called on FastAPI startup and marks those
rows 'orphaned' so API consumers can detect the gap when the K8s Job has
also disappeared.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
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
    # Phase F — every run is 'job' now (the in-process asyncio path was
    # removed). Kept on the model for backwards-compat with DB rows
    # created before this phase (default still 'job' since legacy rows
    # carry the original 'asyncio' string but new ones never do).
    runtime: str = 'job'
    # K8s Job name — equals run_id by D.3 contract. None only for legacy
    # asyncio rows from before Phase F.
    job_name: str | None = None
    # Phase D.5.1.2 — the initiative YAML's declared `branch` field
    # (e.g. `agent/d5-1-2-persist-branch-on-record`). Persisted at register
    # time so the job_reconciler's GH-side PR fallback (D.5.1.1) can look up
    # the open PR by `--head <branch>` without re-deriving the branch name
    # from `record.initiative` (which doesn't match the YAML convention
    # cleanly — see migration 0004 + the reconciler comment). NULL on old
    # rows pre-migration; the reconciler treats NULL as "skip fallback".
    #
    # Phase D.5.1.3 — also surfaced through the FastAPI response_model on
    # POST/GET /initiatives so operators (and `scripts/list_runs.sh`) can
    # see which branch each run targets without a DB round-trip.
    branch: str | None = None
    # V5 D2.2 — wall-clock time the agent's first SDK turn fired. NULL
    # until the run-driver's `mark_first_turn` hook runs. See
    # ``app.db.models.InitiativeRunRow.started_executing_at`` for the
    # full rationale: this distinguishes "agent hasn't done anything
    # yet" from "agent is slow" so the V3 reconciler staleness check
    # and V4 image-pull watchdog don't false-orphan healthy in-flight
    # runs (which would otherwise show turns=0 right up to the first
    # ResultMessage even though they're actively executing).
    started_executing_at: datetime | None = None
    # Per-turn writeback surface (initiative
    # agent-add-per-turn-writeback). Name of the LAST tool the agent
    # invoked during the most recent SDK turn. NULL until the first
    # turn fires (and on plain-text turns thereafter). Surfaced through
    # GET /initiatives so operators see live "what is the agent doing
    # right now?" without polling the decision-log table.
    last_tool_call: str | None = None


_records: dict[str, InitiativeRecord] = {}


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
        branch=run.branch,
        started_executing_at=run.started_executing_at,
        last_tool_call=run.last_tool_call,
    )


async def register(record: InitiativeRecord) -> None:
    """Register a new initiative run — in-memory always, DB when configured.

    The in-memory write happens first (no await), so subsequent update()
    calls never race against an incomplete DB INSERT.

    Phase F: every run lives in a separate K8s Job pod, so there's no
    in-process task to track. Cancellation flows through K8s (the cancel
    endpoint deletes the Job).
    """
    _records[record.id] = record
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
                # Runtime fields set once at INSERT and never mutated.
                runtime=record.runtime,
                job_name=record.job_name,
                # Phase D.5.1.2 — YAML-declared branch, set once at INSERT
                # and read back by the job_reconciler's PR fallback.
                branch=record.branch,
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


async def list_records() -> list[InitiativeRecord]:
    """List all run records — from DB when configured, in-memory fallback otherwise."""
    if is_db_enabled():
        async with db_session() as s:
            runs = await list_runs(s)
            return [_run_record_to_initiative_record(r) for r in runs]
    return list(_records.values())


async def reconcile_orphaned_runs(older_than_seconds: int | None = None) -> int:
    """Mark in-flight DB runs as 'orphaned' when no live execution backs them.

    Called on FastAPI startup (no age filter) AND from the Phase B admin
    cleanup endpoint (with ``older_than_seconds`` set so that recent runs
    that legitimately haven't reached terminal yet are skipped). A pod
    restart leaves DB rows in 'running' or 'queued' state; this function
    rectifies the state so API consumers can detect the gap and act
    accordingly.

    Liveness: a row is live iff K8s reports a Job in ``POD_NAMESPACE``
    labelled ``leartech.io/run-id=<id>``. This is the post-Phase-F
    contract — every run is runtime='job', so every liveness verdict
    routes through the K8s API. Legacy DB rows from pre-Phase-F that
    carry ``runtime='asyncio'`` (and have no backing K8s Job) get
    orphaned on the next reconcile, which is the correct outcome
    (their asyncio task was killed when the API pod that owned it
    rolled).

    K8s API failures are treated conservatively — we DO NOT orphan
    runs when we cannot verify Job liveness. They will be re-evaluated
    on the next startup. False-orphaning a live Job is a worse outcome
    than briefly delaying orphan detection.

    When ``older_than_seconds`` is not None, candidates whose
    ``started_at`` is more recent than that threshold are treated as live
    — keeping them safe from the orphan marker even when there's no
    backing K8s Job yet (e.g. a Job that has been Pending for 30s after
    spawn, before the pod is scheduled). The startup callsite passes None
    (pod restart invalidates every in-memory assumption about liveness);
    the operator cleanup passes 86400 (default 24h) so the endpoint
    can't accidentally sweep healthy mid-run state.

    Returns the count of rows marked orphaned (0 when DB is not configured).
    """
    if not is_db_enabled():
        return 0

    live_ids: set[str] = set()

    async with db_session() as s:
        in_flight = await list_in_flight_runs(s)

    # Age filter: when ``older_than_seconds`` is set, anything more recent
    # than the cutoff is treated as live (kept out of the orphan set). This
    # protects the in-flight steady state on a healthy pod from the admin
    # cleanup endpoint — only stale rows are eligible to be marked orphaned.
    if older_than_seconds is not None:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        for record in in_flight:
            started = record.started_at
            # Postgres returns TZ-aware datetimes; SQLite (tests) can return
            # naive — normalise so the comparison is meaningful.
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if started > cutoff:
                live_ids.add(record.id)

    # Only runtime='job' rows have a backing K8s Job to check. Legacy
    # 'asyncio' rows always fall through to mark_orphaned_runs (correct:
    # the API pod that owned their task is gone).
    job_candidates = [r for r in in_flight if r.runtime == 'job' and r.id not in live_ids]

    for record in job_candidates:
        try:
            if await _job_exists_for_run(record.id):
                live_ids.add(record.id)
        except Exception as exc:  # noqa: BLE001 — K8s failures are diverse
            # Conservative: don't orphan when we can't verify. Re-evaluated
            # on next reconcile cycle. See module docstring for rationale.
            logger.warning(
                'K8s Job-existence check failed for run %s (%s); '
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
