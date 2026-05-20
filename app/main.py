"""FastAPI application for leartech-automated-agent.

Mirrors the leartech-ai-classifier shape: long-running service exposing the
agent runtime via HTTP. Three trigger surfaces compose against the same
endpoints:

1. CRD + controller (production) — webCoder dashboard creates AgentInitiative
   resources; controller spawns Job; Job calls POST /initiatives.
2. Tekton chatops task (slice E evolved) — `/agent run <name>` PR comment
   triggers a thin Tekton task that calls POST /initiatives.
3. Direct HTTP (testing/debug) — curl POST /initiatives, no orchestration.

Cluster-side resource diagnosis lives in the runner Job (surface #1), not in
this service. The agent service stays small and trusts the verdict it was
invoked with.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import dispose_engine, init_engine, is_db_enabled
from app.routers import health, initiative_catalog, initiatives, introspection, lessons, mcp_admin
from app.state import reconcile_orphaned_runs

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — bring up + tear down the DB engine when configured.

    `is_db_enabled()` reads `LEARTECH_INITIATIVE_DB_DSN`. Production sets it
    via the chart's ExternalSecret; dev/CI runs without it and the
    initiative-catalog endpoints return 503.
    """
    if is_db_enabled():
        _logger.info('initialising DB engine (LEARTECH_INITIATIVE_DB_DSN set)')
        init_engine()
        count = await reconcile_orphaned_runs()
        if count > 0:
            _logger.warning('marked %d orphaned runs from prior pod lifecycle', count)
    else:
        _logger.info('DB DSN not configured — running in filesystem-only mode')
    try:
        yield
    finally:
        if is_db_enabled():
            await dispose_engine()
            _logger.info('DB engine disposed')


app = FastAPI(
    title='leartech-automated-agent',
    description='Criteria-driven agent runtime exposed as a long-running service.',
    version='0.2.0',
    lifespan=lifespan,
)

app.include_router(health.router, tags=['health'])
# initiative_catalog must register BEFORE initiatives — the former's `/initiatives/catalog`
# paths would otherwise be shadowed by initiatives' `/initiatives/{initiative_id}`.
app.include_router(initiative_catalog.router, prefix='/initiatives/catalog', tags=['initiative-catalog'])
app.include_router(initiatives.router, prefix='/initiatives', tags=['initiatives'])
app.include_router(lessons.router, prefix='/lessons', tags=['lessons'])
app.include_router(introspection.router, tags=['introspection'])
app.include_router(mcp_admin.router, tags=['mcp-admin'])
