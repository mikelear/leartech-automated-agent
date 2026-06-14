"""POST/GET/PUT/DELETE /initiatives/catalog — DB-backed initiative CRUD.

Reads/writes the `initiative_catalog` table. The fire endpoint
(`POST /initiatives`) is separately wired to look up names in DB+FS
via the loader merge — that's the consumer of these CRUD operations.

If `LEARTECH_INITIATIVE_DB_DSN` is unset (filesystem-only mode), all
endpoints in this router return 503 with a clear message. Production
deployments have the DSN; dev/CI can run without Postgres at all.

Auth deferred — endpoints are unauthenticated for now. Mike's call:
add auth in a later phase once the platform-auth pattern lands.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from app.auth import get_current_tenant_id
from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_catalog import (
    InitiativeRecord,
    create_initiative,
    delete_initiative,
    get_initiative,
    list_initiatives,
    update_initiative,
)
from gate.initiatives.loader import Initiative, load_initiative_from_yaml

router = APIRouter()


# ─── Request / Response models ─────────────────────────────────────────────


class CreateInitiativeRequest(BaseModel):
    name: str = Field(..., min_length=1, description='Kebab-case unique identifier — same shape as YAML stem')
    yaml_body: str = Field(
        ..., min_length=1, description='Full initiative YAML body — parsed by the same loader as filesystem initiatives'
    )
    description: str | None = Field(
        default=None, description='Human-readable rationale; not seen by the agent. Optional.'
    )


class UpdateInitiativeRequest(BaseModel):
    yaml_body: str | None = Field(default=None, description='New YAML body. Omit to leave unchanged.')
    description: str | None = Field(default=None, description='New description. Omit to leave unchanged.')


class InitiativeResponse(BaseModel):
    name: str
    yaml_body: str
    description: str | None
    created_at: datetime
    updated_at: datetime
    created_by: str | None
    # v7-P1 step 5 — exposed so clients (orchestrator, operators) can see
    # which tenant owns the row. None means "global" (visible to all
    # tenants).
    tenant_id: str | None = None

    @classmethod
    def from_record(cls, rec: InitiativeRecord) -> InitiativeResponse:
        return cls(
            name=rec.name,
            yaml_body=rec.yaml_body,
            description=rec.description,
            created_at=rec.created_at,
            updated_at=rec.updated_at,
            created_by=rec.created_by,
            tenant_id=rec.tenant_id,
        )


# ─── Helpers ───────────────────────────────────────────────────────────────


def _require_db() -> None:
    """Raise 503 if running in filesystem-only mode (no DSN configured)."""
    if not is_db_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                'DB-backed initiative catalog is not configured on this deployment. '
                'Set LEARTECH_INITIATIVE_DB_DSN to enable. Filesystem initiatives '
                '(initiatives/*.yaml) are still available via POST /initiatives.'
            ),
        )


def _validate_yaml(yaml_body: str) -> Initiative:
    """Parse + validate the YAML using the canonical loader. Raise 422 on bad shape."""
    try:
        return load_initiative_from_yaml(yaml_body)
    except Exception as exc:  # noqa: BLE001 — pydantic / yaml errors both surface as 422
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Invalid initiative YAML: {exc}',
        ) from exc


# ─── Endpoints ─────────────────────────────────────────────────────────────


@router.get('', response_model=list[InitiativeResponse])
async def list_db_initiatives(request: Request) -> list[InitiativeResponse]:
    """List DB-stored initiatives visible to the caller.

    Filesystem-backed initiatives are NOT included — for the merged
    catalog used by the fire endpoint, see `GET /initiatives` (the
    existing list-available endpoint).

    v7-P1 step 5 — tenant-scoped: returns the caller's own initiatives
    plus the global set (rows with ``tenant_id IS NULL``). Unauthenticated
    / system-tenant callers see every row, mirroring pre-tenancy behaviour.
    """
    _require_db()
    tenant_id = get_current_tenant_id(request)
    async with db_session() as sess:
        records = await list_initiatives(sess, tenant_id=tenant_id)
    return [InitiativeResponse.from_record(r) for r in records]


@router.get('/{name}', response_model=InitiativeResponse)
async def get_db_initiative(name: str, request: Request) -> InitiativeResponse:
    """Get a single DB-stored initiative by name.

    v7-P1 step 5 — tenant-scoped: returns 404 (not 403) when the row
    is owned by a different tenant. 403 would leak existence to a
    caller that has no business knowing the row exists.
    """
    _require_db()
    tenant_id = get_current_tenant_id(request)
    async with db_session() as sess:
        record = await get_initiative(sess, name, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No DB-stored initiative {name!r}')
    return InitiativeResponse.from_record(record)


@router.post('', response_model=InitiativeResponse, status_code=status.HTTP_201_CREATED)
async def create_db_initiative(body: CreateInitiativeRequest, request: Request) -> InitiativeResponse:
    """Create a new DB-stored initiative.

    Validates the YAML through the canonical loader before persisting —
    rejects malformed YAML with 422. Rejects names that already exist
    (in DB) with 409. Filesystem names are NOT checked here — the loader
    merge handles conflicts at fire-time (filesystem wins).

    v7-P1 step 5 — the row's ``tenant_id`` is stamped from the caller's
    authenticated tenant_id. Unauthenticated / system-tenant callers
    (``tenant_id is None``) create global rows; tenant callers create
    tenant-scoped rows visible only to themselves + global readers.
    """
    _require_db()
    parsed = _validate_yaml(body.yaml_body)
    # Defensive: ensure the YAML's `name:` matches the API-supplied name.
    if parsed.name != body.name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"YAML body's `name:` is {parsed.name!r} but request specified {body.name!r}",
        )

    tenant_id = get_current_tenant_id(request)
    async with db_session() as sess:
        try:
            record = await create_initiative(
                sess,
                name=body.name,
                yaml_body=body.yaml_body,
                description=body.description,
                tenant_id=tenant_id,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f'DB-stored initiative {body.name!r} already exists',
            ) from exc

    return InitiativeResponse.from_record(record)


@router.put('/{name}', response_model=InitiativeResponse)
async def update_db_initiative(name: str, body: UpdateInitiativeRequest, request: Request) -> InitiativeResponse:
    """Update a DB-stored initiative.

    v7-P1 step 5 — tenant-scoped: cross-tenant updates return 404 (not
    403) so existence isn't leaked. Tenant callers cannot edit global
    rows (``row.tenant_id IS NULL``) — those are only editable by the
    system tenant (which the middleware represents as
    ``tenant_id is None`` here after the X-Tenant-Id relay check).
    """
    _require_db()
    if body.yaml_body is not None:
        parsed = _validate_yaml(body.yaml_body)
        if parsed.name != name:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"YAML body's `name:` is {parsed.name!r} but path specified {name!r}",
            )

    tenant_id = get_current_tenant_id(request)
    async with db_session() as sess:
        record = await update_initiative(
            sess,
            name=name,
            yaml_body=body.yaml_body,
            description=body.description,
            tenant_id=tenant_id,
        )
    if record is None:
        raise HTTPException(status_code=404, detail=f'No DB-stored initiative {name!r}')
    return InitiativeResponse.from_record(record)


@router.delete('/{name}', status_code=status.HTTP_204_NO_CONTENT)
async def delete_db_initiative(name: str, request: Request) -> None:
    """Delete a DB-stored initiative.

    Cannot delete filesystem-backed initiatives via this endpoint — those
    are managed via PR to the repo's `initiatives/` directory.

    v7-P1 step 5 — tenant-scoped: cross-tenant deletes return 404. A
    tenant cannot delete global rows (``tenant_id IS NULL``); only the
    system tenant can.
    """
    _require_db()
    tenant_id = get_current_tenant_id(request)
    async with db_session() as sess:
        deleted = await delete_initiative(sess, name, tenant_id=tenant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f'No DB-stored initiative {name!r}')
