"""Initiative endpoints — start, status, list, cancel.

Phase B v2: async state ops wired through write-through-DB. Each
POST /initiatives spawns an asyncio.Task that runs
`gate.agent.initiative.run_initiative`. State lives in `app.state` —
durable in Postgres when `LEARTECH_INITIATIVE_DB_DSN` is set, in-memory
fallback when not (dev/CI/preview).

Catalog-first resolution (feat: catalog-fire-fallback):
  POST /initiatives checks the DB catalog FIRST (when `is_db_enabled()`),
  materialises the yaml_body to /tmp/agent-catalog/<name>.yaml, and passes
  that path to run_initiative. If not in DB, falls back to the baked-in
  filesystem initiatives/*.yaml. DB-stored entries WIN over same-named
  filesystem entries — DB is the live editable source of truth, filesystem
  is the "starter pack" from the current image.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from sqlite3 import ProgrammingError as SQLiteProgrammingError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError as SAProgrammingError

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_catalog import get_initiative
from app.db.initiative_catalog import list_initiatives as list_db_initiatives
from app.state import (
    InitiativeRecord,
    list_records,
    new_id,
    now,
    register,
    update,
)
from app.state import (
    cancel as cancel_initiative_task,
)
from app.state import (
    get as get_record,
)
from gate.agent.initiative import run_initiative
from gate.initiatives.loader import load_initiative

logger = logging.getLogger(__name__)

router = APIRouter()

# CLUSTER identifies which Kubernetes cluster this service instance runs on.
# Set via the chart's Deployment env — falls back to 'unknown' in local/CI runs.
_CLUSTER = os.environ.get('CLUSTER', 'unknown')

# Directory where DB-resolved initiatives are materialised so run_initiative
# can consume them as a plain Path.
_CATALOG_TMP_DIR = Path('/tmp/agent-catalog')  # noqa: S108 — intentional service-internal tmp dir


def _initiatives_dir() -> Path:
    """Where YAML initiatives live. Configurable via env in a later slice."""
    candidate = Path.cwd() / 'initiatives'
    if candidate.exists():
        return candidate
    raise HTTPException(
        status_code=500,
        detail=f'Initiatives directory not found at {candidate}. '
        'Set the working directory or wire LEARTECH_INITIATIVES_DIR (v1.5).',
    )


async def _resolve_yaml_path(name: str) -> Path | None:
    """Resolve an initiative by name — DB catalog first, filesystem fallback.

    DB-stored entries win over same-named filesystem entries: the DB is the
    live editable source of truth; the filesystem is the starter pack baked
    into each image release.

    When found in the DB the yaml_body is materialised to
    ``/tmp/agent-catalog/<name>.yaml`` (overwritten each call so a PUT via
    the catalog API is reflected on the next fire without any TTL games).

    Returns the Path to the YAML if found, or None if not found in either
    source. Never raises — callers map None to 404.
    """
    if is_db_enabled():
        try:
            async with db_session() as sess:
                record = await get_initiative(sess, name)
            if record is not None:
                _CATALOG_TMP_DIR.mkdir(parents=True, exist_ok=True)
                tmp_path = _CATALOG_TMP_DIR / f'{name}.yaml'
                tmp_path.write_text(record.yaml_body)
                return tmp_path
        except Exception:  # noqa: BLE001 — DB errors fall through to filesystem
            logger.warning('DB lookup for initiative %r failed; falling back to filesystem', name)

    # Filesystem fallback — also the only path when DB is disabled.
    try:
        fs_path = _initiatives_dir() / f'{name}.yaml'
    except HTTPException:
        return None
    return fs_path if fs_path.exists() else None


async def _available_names() -> list[str]:
    """Return all known initiative names — DB union filesystem, sorted.

    Used to populate the 404 detail so callers know what's available without
    a separate discovery call.
    """
    names: set[str] = set()
    if is_db_enabled():
        try:
            async with db_session() as sess:
                records = await list_db_initiatives(sess)
            names.update(r.name for r in records)
        except Exception:  # noqa: BLE001 — best-effort; don't crash a 404 response
            logger.warning('Could not list DB initiatives for 404 detail')
    try:
        fs_dir = _initiatives_dir()
        names.update(p.stem for p in fs_dir.glob('*.yaml') if not p.stem.startswith('_'))
    except HTTPException:
        pass
    return sorted(names)


class StartInitiativeRequest(BaseModel):
    initiative: str = Field(..., description='Initiative YAML name (without .yaml)')


async def _run_and_track(initiative_id: str, yaml_path: Path) -> None:
    """Background task body — runs the initiative, updates state on completion."""
    await update(initiative_id, status='running')
    try:
        summary = await run_initiative(yaml_path)
        await update(
            initiative_id,
            status='complete' if summary.exit_code == 0 else 'failed',
            finished_at=now(),
            turns=summary.turns,
            cost_usd=summary.cost_usd,
            pr_number=summary.pr_number,
        )
        logger.info('initiative %s finished with exit_code=%d', initiative_id, summary.exit_code)
    except asyncio.CancelledError:
        # Best-effort: if the engine is gone (pod shutdown raced with cancellation),
        # the next pod's orphan-detection on startup will mark this run terminal —
        # don't crash here. Two disposal surfaces are tolerated:
        #   * RuntimeError — session factory was cleared (PR #35).
        #   * (SA|SQLite)ProgrammingError — engine connection is closed mid-cleanup
        #     under cluster contention (GCP release tb8t6, 2026-05-27).
        try:
            await update(initiative_id, status='cancelled', finished_at=now())
        except (RuntimeError, SAProgrammingError, SQLiteProgrammingError) as exc:
            logger.warning(
                'cancel-cleanup skipped — DB engine already disposed (will be marked orphaned on next pod start): %s',
                exc,
            )
        logger.info('initiative %s cancelled', initiative_id)
        raise
    except Exception as exc:  # noqa: BLE001 — we want to surface any agent failure to the consumer
        await update(initiative_id, status='failed', error=str(exc), finished_at=now())
        logger.exception('initiative %s failed', initiative_id)


@router.post('', response_model=InitiativeRecord, status_code=202)
async def start_initiative(request: StartInitiativeRequest) -> InitiativeRecord:
    """Validate the initiative YAML and spawn a background task to execute it.

    Resolution order: DB catalog first (when LEARTECH_INITIATIVE_DB_DSN is set),
    filesystem fallback. DB-stored entries win over same-named filesystem entries.

    Returns 202 with the initial record (status=queued). Poll
    GET /initiatives/{id} for terminal status.
    """
    yaml_path = await _resolve_yaml_path(request.initiative)
    if yaml_path is None:
        available = await _available_names()
        raise HTTPException(
            status_code=404,
            detail={'message': f'Initiative {request.initiative!r} not found', 'available': available},
        )

    try:
        loaded = load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc

    initiative_id = new_id()
    record = InitiativeRecord(
        id=initiative_id,
        initiative=request.initiative,
        status='queued',
        started_at=now(),
        pr_repo=loaded.primary.qualified_repo,
        cluster=_CLUSTER,
    )
    task = asyncio.create_task(_run_and_track(initiative_id, yaml_path))
    await register(record, task)
    logger.info('initiative %s queued: %s', initiative_id, request.initiative)
    return record


@router.get('', response_model=list[InitiativeRecord])
async def list_initiatives() -> list[InitiativeRecord]:
    """List all initiatives this process has seen — running, complete, or terminal."""
    return await list_records()


@router.get('/{initiative_id}', response_model=InitiativeRecord)
async def get_initiative_status(initiative_id: str) -> InitiativeRecord:
    """Get current status of a queued / running / completed initiative."""
    record = await get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')
    return record


@router.post('/{initiative_id}/cancel', response_model=InitiativeRecord)
async def cancel_initiative(initiative_id: str) -> InitiativeRecord:
    """Request cancellation of a running initiative. Idempotent for terminal records."""
    record = await get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    cancelled = await cancel_initiative_task(initiative_id)
    if not cancelled and record.status not in {'cancelled', 'complete', 'failed'}:
        raise HTTPException(
            status_code=409,
            detail=f'Initiative {initiative_id!r} is in status {record.status!r}; cannot cancel',
        )

    refreshed = await get_record(initiative_id)
    if refreshed is None:  # pragma: no cover — record was just confirmed above
        raise HTTPException(status_code=500, detail='Record disappeared between get and refresh')
    return refreshed


@router.get('/_validate/{initiative}')
async def validate_initiative(initiative: str) -> dict[str, object]:
    """Resolve and parse an initiative YAML, returning a summary dict.

    No side effects. Useful for callers (Tekton task, CRD controller) to
    verify YAML correctness before POST. Returns a plain dict rather than
    the Initiative model itself because the model carries both new
    (`repos: [...]`) and legacy (`repo`, `branch`, `base`) shapes after
    normalization, which trips re-validation when re-serialized.
    """
    yaml_path = await _resolve_yaml_path(initiative)
    if yaml_path is None:
        raise HTTPException(status_code=404, detail=f'Initiative {initiative!r} not found')
    try:
        loaded = load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
    primary = loaded.primary
    return {
        'name': loaded.name,
        'description': loaded.description,
        'repos': [{'repo': r.repo, 'branch': r.branch, 'base': r.base} for r in loaded.repos],
        'primary': {'repo': primary.repo, 'branch': primary.branch, 'base': primary.base},
        'gate_marks': loaded.gate_marks,
        'max_iterations': loaded.max_iterations,
    }
