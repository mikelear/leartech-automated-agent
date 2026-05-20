"""CRUD operations for the DB-backed initiative catalogue.

Thin async wrappers around the SQLAlchemy model. Keeps the router thin and
makes the operations independently testable against an in-memory SQLite
engine (real production uses Postgres via asyncpg).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
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

    @classmethod
    def from_row(cls, row: InitiativeRow) -> InitiativeRecord:
        return cls(
            name=row.name,
            yaml_body=row.yaml_body,
            description=row.description,
            created_at=row.created_at,
            updated_at=row.updated_at,
            created_by=row.created_by,
        )


async def create_initiative(
    session: AsyncSession,
    *,
    name: str,
    yaml_body: str,
    description: str | None = None,
    created_by: str | None = None,
) -> InitiativeRecord:
    """Create a new DB-stored initiative. Raises if `name` already exists.

    Caller is responsible for catching IntegrityError → 409 conflict mapping
    at the router layer (kept out of this module to avoid HTTP coupling).
    """
    row = InitiativeRow(
        name=name,
        yaml_body=yaml_body,
        description=description,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return InitiativeRecord.from_row(row)


async def list_initiatives(session: AsyncSession) -> list[InitiativeRecord]:
    """Return all DB-stored initiatives ordered by name."""
    result = await session.execute(select(InitiativeRow).order_by(InitiativeRow.name))
    return [InitiativeRecord.from_row(row) for row in result.scalars()]


async def get_initiative(session: AsyncSession, name: str) -> InitiativeRecord | None:
    """Return a single DB-stored initiative or None if not found."""
    row = await session.get(InitiativeRow, name)
    return InitiativeRecord.from_row(row) if row is not None else None


async def update_initiative(
    session: AsyncSession,
    *,
    name: str,
    yaml_body: str | None = None,
    description: str | None = None,
) -> InitiativeRecord | None:
    """Partial update of a DB-stored initiative. Returns None if not found.

    `yaml_body` and `description` are optional — pass only the fields you
    want to change. `created_at` / `created_by` are immutable.
    """
    row = await session.get(InitiativeRow, name)
    if row is None:
        return None
    if yaml_body is not None:
        row.yaml_body = yaml_body
    if description is not None:
        row.description = description
    await session.flush()
    await session.refresh(row)
    return InitiativeRecord.from_row(row)


async def delete_initiative(session: AsyncSession, name: str) -> bool:
    """Delete a DB-stored initiative. Returns True if deleted, False if not found."""
    # Check-then-delete keeps the return-value clean without depending on
    # SQLAlchemy's rowcount attribute (typed as Result[Any], no rowcount in
    # the stub even though it exists at runtime).
    row = await session.get(InitiativeRow, name)
    if row is None:
        return False
    await session.execute(delete(InitiativeRow).where(InitiativeRow.name == name))
    return True
