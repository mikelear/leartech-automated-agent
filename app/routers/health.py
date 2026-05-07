"""Health probes — liveness + readiness.

Both currently return the same shape; readiness will diverge once the service
has dependencies to verify (catalog loaded, anthropic key present, etc.).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


_RESPONSE = HealthResponse(status='ok', service='leartech-automated-agent', version='0.1.0')


@router.get('/health', response_model=HealthResponse)
async def health() -> HealthResponse:
    return _RESPONSE


@router.get('/healthz', response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return _RESPONSE


@router.get('/readyz', response_model=HealthResponse)
async def readyz() -> HealthResponse:
    return _RESPONSE
