"""Initiative endpoints — start, status, logs, cancel.

Phase B v1: validates that the requested initiative YAML exists, returns the
parsed Initiative shape so callers can sanity-check before triggering. Actual
async execution wiring (background task, in-memory state, log streaming)
lands in v1.5.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from gate.initiatives.loader import Initiative, load_initiative

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


class InitiativeStatus(BaseModel):
    id: str
    initiative: str
    status: str = Field(..., description='queued | running | complete | failed | cancelled')
    pr_number: int | None = None
    turns: int | None = None
    cost_usd: float | None = None


@router.post('', response_model=InitiativeStatus, status_code=202)
async def start_initiative(request: StartInitiativeRequest) -> InitiativeStatus:
    """Validate the initiative YAML and queue it for execution.

    Phase B v1: validation works; execution returns 501 until v1.5 wires the
    async background task. This lets callers (Tekton task, CRD controller,
    dashboard) integrate against the contract before runtime is wired.
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
    except Exception as exc:  # noqa: BLE001 — surface any pydantic validation error
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc

    raise HTTPException(
        status_code=501,
        detail='Initiative validated successfully but runtime is not yet wired (phase B v1.5).',
    )


@router.get('/{initiative_id}', response_model=InitiativeStatus)
async def get_initiative_status(initiative_id: str) -> InitiativeStatus:
    """Get current status of a running or completed initiative."""
    raise HTTPException(status_code=501, detail='State store not yet wired — phase B v1.5')


@router.post('/{initiative_id}/cancel', response_model=InitiativeStatus)
async def cancel_initiative(initiative_id: str) -> InitiativeStatus:
    """Request cancellation of a running initiative."""
    raise HTTPException(status_code=501, detail='Cancellation not yet wired — phase B v1.5')


@router.get('/_validate/{initiative}', response_model=Initiative)
async def validate_initiative(initiative: str) -> Initiative:
    """Resolve and parse an initiative YAML, returning the validated model.

    Useful for callers to verify YAML correctness before POST. No side effects.
    """
    yaml_path = _initiatives_dir() / f'{initiative}.yaml'
    if not yaml_path.exists():
        raise HTTPException(status_code=404, detail=f'Initiative {initiative!r} not found')
    try:
        return load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
