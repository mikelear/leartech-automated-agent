"""CRUD operations for the DB-backed initiative run store.

Thin async wrappers around InitiativeRunRow. Keeps the state module thin
and makes operations independently testable against an in-memory SQLite
engine (real production uses Postgres via asyncpg).

Pattern mirrors app/db/initiative_catalog.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InitiativeRunRow


@dataclass(frozen=True)
class InitiativeRunRecord:
    """Plain-data view of a DB-stored run — returned to API handlers.

    Keeping a separate dataclass insulates the state/API layer from
    SQLAlchemy types (lazy loading, transient session-bound state, etc.).
    """

    id: str
    initiative: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    pr_number: int | None
    pr_repo: str | None
    turns: int | None
    cost_usd: Decimal | None
    error: str | None
    cluster: str | None
    created_by: str | None
    # Phase F: runtime is always 'job' on new rows; legacy DB rows from
    # before Phase F may still carry 'asyncio'. `job_name` is the K8s
    # Job name (None only for legacy asyncio rows).
    runtime: str
    job_name: str | None
    updated_at: datetime

    @classmethod
    def from_row(cls, row: InitiativeRunRow) -> InitiativeRunRecord:
        return cls(
            id=row.id,
            initiative=row.initiative,
            status=row.status,
            started_at=row.started_at,
            finished_at=row.finished_at,
            pr_number=row.pr_number,
            pr_repo=row.pr_repo,
            turns=row.turns,
            cost_usd=row.cost_usd,
            error=row.error,
            cluster=row.cluster,
            created_by=row.created_by,
            runtime=row.runtime,
            job_name=row.job_name,
            updated_at=row.updated_at,
        )


async def create_run(
    session: AsyncSession,
    *,
    id: str,
    initiative: str,
    status: str,
    started_at: datetime,
    cluster: str | None = None,
    created_by: str | None = None,
    pr_repo: str | None = None,
    runtime: str = 'job',
    job_name: str | None = None,
) -> InitiativeRunRecord:
    """Create a new DB-stored run row. Raises on IntegrityError for duplicate id.

    Caller maps IntegrityError → HTTP 409 at the router layer (kept out of
    this module to avoid HTTP coupling).

    ``pr_repo`` is accepted at insert time because the initiative loader knows
    the qualified repo BEFORE the run starts — see
    ``app.state.register``. Persisting it at INSERT (rather than waiting for
    the completion ``update_run``) means a pod restart between INSERT and
    completion still leaves the DB row with a usable pr_repo for downstream
    consumers (notably the self_retrospect hook, which needs pr_repo +
    pr_number to file Issues — regression observed on run ``44120e445abd``
    2026-05-28 when pr_repo arrived NULL and the hook skipped every run).
    """
    row = InitiativeRunRow(
        id=id,
        initiative=initiative,
        status=status,
        started_at=started_at,
        cluster=cluster,
        created_by=created_by,
        pr_repo=pr_repo,
        runtime=runtime,
        job_name=job_name,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return InitiativeRunRecord.from_row(row)


async def get_run(session: AsyncSession, id: str) -> InitiativeRunRecord | None:
    """Return a single run record or None if not found."""
    row = await session.get(InitiativeRunRow, id)
    return InitiativeRunRecord.from_row(row) if row is not None else None


async def list_runs(
    session: AsyncSession,
    *,
    status: str | None = None,
    initiative: str | None = None,
    limit: int = 100,
) -> list[InitiativeRunRecord]:
    """Return runs ordered by started_at DESC, with optional filters.

    `status` and `initiative` are exact-match filters. Both may be combined.
    `limit` caps the result set — default 100 to avoid unbounded scans.
    """
    stmt = select(InitiativeRunRow).order_by(InitiativeRunRow.started_at.desc())
    if status is not None:
        stmt = stmt.where(InitiativeRunRow.status == status)
    if initiative is not None:
        stmt = stmt.where(InitiativeRunRow.initiative == initiative)
    stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return [InitiativeRunRecord.from_row(row) for row in result.scalars()]


async def update_run(
    session: AsyncSession,
    *,
    id: str,
    **fields: object,
) -> InitiativeRunRecord | None:
    """Partial update of a run row. Returns None if not found.

    Accepted field names mirror the InitiativeRunRow columns:
    status, finished_at, pr_number, pr_repo, turns, cost_usd, error, cluster.

    `id`, `initiative`, `started_at`, `created_by` are immutable after creation.
    Unknown field names are silently ignored (avoids tight coupling to callers
    passing arbitrary kwargs).
    """
    _mutable = frozenset(
        {
            'status',
            'finished_at',
            'pr_number',
            'pr_repo',
            'turns',
            'cost_usd',
            'error',
            'cluster',
        }
    )
    filtered = {k: v for k, v in fields.items() if k in _mutable}
    if not filtered:
        return await get_run(session, id)

    row = await session.get(InitiativeRunRow, id)
    if row is None:
        return None
    for k, v in filtered.items():
        setattr(row, k, v)
    await session.flush()
    await session.refresh(row)
    return InitiativeRunRecord.from_row(row)


async def list_in_flight_runs(session: AsyncSession) -> list[InitiativeRunRecord]:
    """Return all runs with status in ('queued', 'running') — no limit applied.

    Used by orphan reconciliation to enumerate candidates BEFORE marking
    them orphaned, so callers can per-record decide whether the run is
    actually live (e.g. by checking K8s for a backing Job pod — see
    ``app.state.reconcile_orphaned_runs``).

    The returned records preserve ``runtime`` and ``job_name`` so the
    caller can branch on runtime when deciding the orphan-detection
    strategy: legacy asyncio rows have no backing process and always
    orphan; job-runtime rows are validated against K8s.
    """
    stmt = select(InitiativeRunRow).where(InitiativeRunRow.status.in_(['queued', 'running']))
    result = await session.execute(stmt)
    return [InitiativeRunRecord.from_row(row) for row in result.scalars()]


async def mark_orphaned_runs(session: AsyncSession, live_ids: set[str]) -> int:
    """Mark in-flight DB runs as 'orphaned' if their id is NOT in `live_ids`.

    Called on FastAPI startup after a pod restart. `live_ids` carries the
    set of run-ids whose backing K8s Job is still alive in POD_NAMESPACE
    (Phase F: every run has a backing Job; legacy asyncio rows always
    fall out of this set). Anything in 'queued'/'running' that's not in
    the live set is unreachable — mark it orphaned so callers can detect
    the gap.

    Returns the count of rows updated.
    """
    # Fetch candidates first (avoids a NOT IN subquery with a potentially
    # empty set, which has dialect-specific behaviour).
    stmt = select(InitiativeRunRow).where(InitiativeRunRow.status.in_(['queued', 'running']))
    result = await session.execute(stmt)
    rows = result.scalars().all()
    orphan_ids = [row.id for row in rows if row.id not in live_ids]
    if not orphan_ids:
        return 0

    # Bulk-update the orphaned rows.
    await session.execute(
        sa_update(InitiativeRunRow).where(InitiativeRunRow.id.in_(orphan_ids)).values(status='orphaned')
    )
    await session.flush()
    return len(orphan_ids)
