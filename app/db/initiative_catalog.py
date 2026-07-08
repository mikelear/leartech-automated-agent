"""CRUD operations for the DB-backed initiative catalogue.

Thin async wrappers around the SQLAlchemy model. Keeps the router thin and
makes the operations independently testable against an in-memory SQLite
engine (real production uses Postgres via asyncpg).

v7-P1 step 5 — multi-tenant data isolation:

  - ``create_initiative`` writes the caller's ``tenant_id`` onto the row.
    ``None`` is the encoding for "global, system-tenant-owned" — visible
    to every tenant via the reader paths.
  - ``list_initiatives`` returns the tenant's own initiatives PLUS the
    global set (``tenant_id IS NULL``). When the caller has no
    ``tenant_id`` context (system tenant operating in raw mode, or
    unauthenticated dev traffic), it returns everything — same as
    pre-tenancy behaviour.
  - ``get_initiative`` / ``update_initiative`` / ``delete_initiative``
    treat a cross-tenant access as "not found" (404 at the router). The
    motivation: 403 vs 404 leaks the existence of the row to a tenant
    that has no business knowing it exists. Mirrors the orchestrator
    step 4 contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InitiativeRow


@dataclass(frozen=True)
class InitiativeRecord:
    """Plain-data view of a DB-stored initiative — returned to API handlers.

    Keeping a separate dataclass insulates the API layer from SQLAlchemy types
    (lazy loading, transient session-bound state, etc.).
    """

    name: str
    yaml_body: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    # v7-P1 step 5 — the row's owning tenant, or None for global entries.
    tenant_id: str | None

    @classmethod
    def from_row(cls, row: InitiativeRow) -> InitiativeRecord:
        return cls(
            name=row.name,
            yaml_body=row.yaml_body,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
            tenant_id=row.tenant_id,
        )


async def create_initiative(
    session: AsyncSession,
    *,
    name: str,
    yaml_body: str,
    description: str | None = None,
    created_by: str | None = None,
    tenant_id: str | None = None,
) -> InitiativeRecord:
    """Create a new DB-stored initiative. Raises if `name` already exists.

    ``tenant_id`` defaults to ``None`` — global entries (system-tenant
    library) write NULL, tenant-specific entries write the tenant's id.

    Caller is responsible for catching IntegrityError → 409 conflict mapping
    at the router layer (kept out of this module to avoid HTTP coupling).
    """
    row = InitiativeRow(
        name=name,
        yaml_body=yaml_body,
        description=description,
        created_by=created_by,
        tenant_id=tenant_id,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return InitiativeRecord.from_row(row)


async def list_initiatives(
    session: AsyncSession,
    *,
    tenant_id: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[InitiativeRecord]:
    """Return DB-stored initiatives visible to ``tenant_id``, ordered by name.

    Visibility:

    - ``tenant_id`` is None → return every row (system tenant or
      unauthenticated context). This matches pre-tenancy semantics so
      filesystem-only / dev / CI flows continue to see everything.
    - ``tenant_id`` is set → return rows where
      ``tenant_id IS NULL OR tenant_id = <caller>``. Global entries (NULL)
      are always visible; tenant-specific entries are visible to their
      owning tenant only.

    Pagination:

    - ``limit`` — max rows to return. ``None`` (default) means no limit
      is applied — the DB returns every visible row. Callers that page
      (the GET /initiatives/catalog handler) pass an explicit limit.
    - ``offset`` — rows to skip from the start of the ordered set.
      Defaults to 0. Applied after ``ORDER BY name`` so pagination is
      stable across calls even without a cursor.
    """
    stmt = select(InitiativeRow).order_by(InitiativeRow.name)
    if tenant_id is not None:
        stmt = stmt.where(or_(InitiativeRow.tenant_id.is_(None), InitiativeRow.tenant_id == tenant_id))
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return [InitiativeRecord.from_row(row) for row in result.scalars()]


async def get_initiative(
    session: AsyncSession,
    name: str,
    *,
    tenant_id: str | None = None,
) -> InitiativeRecord | None:
    """Return a single DB-stored initiative visible to ``tenant_id`` or None.

    Same visibility rules as :func:`list_initiatives`. Returns None when
    the row exists but belongs to a different tenant — the router maps
    that to 404 to avoid leaking existence to an unauthorised tenant.
    """
    row = await session.get(InitiativeRow, name)
    if row is None:
        return None
    if tenant_id is not None and row.tenant_id is not None and row.tenant_id != tenant_id:
        # Cross-tenant access — pretend the row does not exist (404 at
        # the router). 403 would confirm the row's existence to a tenant
        # that has no business knowing it exists.
        return None
    return InitiativeRecord.from_row(row)


async def update_initiative(
    session: AsyncSession,
    *,
    name: str,
    yaml_body: str | None = None,
    description: str | None = None,
    tenant_id: str | None = None,
) -> InitiativeRecord | None:
    """Partial update of a DB-stored initiative. Returns None if not found.

    `yaml_body` and `description` are optional — pass only the fields you
    want to change. `created_at` / `created_by` / `tenant_id` are immutable.

    Tenant scoping: when ``tenant_id`` is set, updates against rows owned
    by a different tenant return None (router maps to 404). Global rows
    (``row.tenant_id IS NULL``) are NOT updatable by tenant callers —
    a tenant editing a global initiative would silently mutate state
    that other tenants see, which is wrong. Only the system tenant
    (called with ``tenant_id=None`` here) can update globals.
    """
    row = await session.get(InitiativeRow, name)
    if row is None:
        return None
    if tenant_id is not None and row.tenant_id != tenant_id:
        # Either the row is global (tenant_id IS NULL — not editable by
        # a tenant caller) or it's owned by a different tenant. Either
        # way, the response is "not found" so cross-tenant probes can't
        # distinguish "doesn't exist" from "exists but not yours".
        return None
    if yaml_body is not None:
        row.yaml_body = yaml_body
    if description is not None:
        row.description = description
    await session.flush()
    await session.refresh(row)
    return InitiativeRecord.from_row(row)


async def delete_initiative(
    session: AsyncSession,
    name: str,
    *,
    tenant_id: str | None = None,
) -> bool:
    """Delete a DB-stored initiative. Returns True if deleted, False if not found.

    Same tenant scoping rules as :func:`update_initiative`: a tenant
    caller cannot delete global rows or other tenants' rows; both
    return False (router maps to 404).
    """
    # Check-then-delete keeps the return-value clean without depending on
    # SQLAlchemy's rowcount attribute (typed as Result[Any], no rowcount in
    # the stub even though it exists at runtime).
    row = await session.get(InitiativeRow, name)
    if row is None:
        return False
    if tenant_id is not None and row.tenant_id != tenant_id:
        # Cross-tenant or global-row delete by tenant — refuse. The
        # router maps False to 404 so the caller cannot distinguish
        # "doesn't exist" from "exists but not yours".
        return False
    await session.execute(delete(InitiativeRow).where(InitiativeRow.name == name))
    return True
