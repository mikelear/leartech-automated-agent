"""Initiative endpoints — start, status, list, cancel.

Phase B v1.5: actual async execution wired. Each POST /initiatives spawns
an asyncio.Task that runs `gate.agent.initiative.run_initiative`. State
lives in `app.state` (in-memory, lost on restart — see that module's
docstring for the v2 plan).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


class StartInitiativeRequest(BaseModel):
    initiative: str = Field(..., description='Initiative YAML name (without .yaml)')


async def _run_and_track(initiative_id: str, yaml_path: Path) -> None:
    """Background task body — runs the initiative, updates state on completion."""
    update(initiative_id, status='running')
    try:
        summary = await run_initiative(yaml_path)
        update(
            initiative_id,
            status='complete' if summary.exit_code == 0 else 'failed',
            finished_at=now(),
            turns=summary.turns,
            cost_usd=summary.cost_usd,
            pr_number=summary.pr_number,
        )
        logger.info('initiative %s finished with exit_code=%d', initiative_id, summary.exit_code)
    except asyncio.CancelledError:
        update(initiative_id, status='cancelled', finished_at=now())
        logger.info('initiative %s cancelled', initiative_id)
        raise
    except Exception as exc:  # noqa: BLE001 — we want to surface any agent failure to the consumer
        update(initiative_id, status='failed', error=str(exc), finished_at=now())
        logger.exception('initiative %s failed', initiative_id)


@router.post('', response_model=InitiativeRecord, status_code=202)
async def start_initiative(request: StartInitiativeRequest) -> InitiativeRecord:
    """Validate the initiative YAML and spawn a background task to execute it.

    Returns 202 with the initial record (status=queued). Poll
    GET /initiatives/{id} for terminal status.
    """
    yaml_path = _initiatives_dir() / f'{request.initiative}.yaml'
    if not yaml_path.exists():
        available = sorted(p.stem for p in _initiatives_dir().glob('*.yaml') if not p.stem.startswith('_'))
        raise HTTPException(
            status_code=404,
            detail={'message': f'Initiative {request.initiative!r} not found', 'available': available},
        )

    try:
        load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc

    initiative_id = new_id()
    record = InitiativeRecord(
        id=initiative_id,
        initiative=request.initiative,
        status='queued',
        started_at=now(),
    )
    task = asyncio.create_task(_run_and_track(initiative_id, yaml_path))
    register(record, task)
    logger.info('initiative %s queued: %s', initiative_id, request.initiative)
    return record


@router.get('', response_model=list[InitiativeRecord])
async def list_initiatives() -> list[InitiativeRecord]:
    """List all initiatives this process has seen — running, complete, or terminal."""
    return list_records()


@router.get('/{initiative_id}', response_model=InitiativeRecord)
async def get_initiative_status(initiative_id: str) -> InitiativeRecord:
    """Get current status of a queued / running / completed initiative."""
    record = get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')
    return record


@router.post('/{initiative_id}/cancel', response_model=InitiativeRecord)
async def cancel_initiative(initiative_id: str) -> InitiativeRecord:
    """Request cancellation of a running initiative. Idempotent for terminal records."""
    record = get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    cancelled = cancel_initiative_task(initiative_id)
    if not cancelled and record.status not in {'cancelled', 'complete', 'failed'}:
        raise HTTPException(
            status_code=409,
            detail=f'Initiative {initiative_id!r} is in status {record.status!r}; cannot cancel',
        )

    refreshed = get_record(initiative_id)
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
    yaml_path = _initiatives_dir() / f'{initiative}.yaml'
    if not yaml_path.exists():
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
