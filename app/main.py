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

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError

from app.db import dispose_engine, init_engine, is_db_enabled
from app.db import session as db_session
from app.db.initiative_catalog import create_initiative, get_initiative
from app.routers import health, initiative_catalog, initiatives, introspection, lessons, mcp_admin
from app.state import reconcile_orphaned_runs

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
_logger = logging.getLogger(__name__)


async def seed_catalog_from_filesystem() -> None:
    """Seed the DB catalog from the pod's baked-in initiatives/*.yaml files.

    Called once during FastAPI startup (after init_engine + reconcile). Only
    runs when is_db_enabled() is True — callers must guard before calling.

    For each YAML file found in <cwd>/initiatives/:
      - If the DB already has an entry with that name, SKIP (don't overwrite —
        preserves any edits made via PUT after startup).
      - Otherwise INSERT the YAML body verbatim.

    Idempotent: subsequent pod startups against the same DB are no-ops because
    the rows already exist. First-boot populates the catalog so that every YAML
    shipped with the image is fireable by name immediately.

    Startup continues even when seeding fails (best-effort log, no raise) so
    that a bad YAML in the filesystem doesn't block the whole service.
    """
    initiatives_dir = Path.cwd() / 'initiatives'
    if not initiatives_dir.exists():
        _logger.warning('seed_catalog_from_filesystem: initiatives/ directory not found; skipping')
        return

    yaml_files = sorted(p for p in initiatives_dir.glob('*.yaml') if not p.stem.startswith('_'))
    seeded = 0
    skipped = 0

    for yaml_path in yaml_files:
        name = yaml_path.stem
        try:
            async with db_session() as sess:
                existing = await get_initiative(sess, name)
                if existing is not None:
                    skipped += 1
                    continue
                try:
                    await create_initiative(sess, name=name, yaml_body=yaml_path.read_text())
                    seeded += 1
                except IntegrityError:
                    # Race: another pod seeded it between our get and insert.
                    skipped += 1
        except Exception:  # noqa: BLE001 — don't let one bad YAML block startup
            _logger.exception('seed_catalog_from_filesystem: failed to seed %r', name)

    _logger.info(
        'seed_catalog_from_filesystem: seeded=%d skipped=%d total=%d',
        seeded,
        skipped,
        len(yaml_files),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan — bring up + tear down the DB engine when configured.

    `is_db_enabled()` reads `LEARTECH_INITIATIVE_DB_DSN`. Production sets it
    via the chart's ExternalSecret; dev/CI runs without it and the
    initiative-catalog endpoints return 503.

    When `LEARTECH_INITIATIVE_RUNTIME=job`, a background reconciler task
    polls K8s for finished runner Jobs and patches the corresponding
    `initiative_runs` rows (D.5). Without it, Job-mode runs stay 'queued'
    in the DB forever even after the agent has completed cleanly.
    """
    reconciler_task: asyncio.Task[None] | None = None
    if is_db_enabled():
        _logger.info('initialising DB engine (LEARTECH_INITIATIVE_DB_DSN set)')
        init_engine()
        count = await reconcile_orphaned_runs()
        if count > 0:
            _logger.warning('marked %d orphaned runs from prior pod lifecycle', count)
        await seed_catalog_from_filesystem()
    else:
        _logger.info('DB DSN not configured — running in filesystem-only mode')

    if os.environ.get('LEARTECH_INITIATIVE_RUNTIME', '').lower() == 'job':
        namespace = os.environ.get('POD_NAMESPACE')
        if namespace and is_db_enabled():
            from gate.agent.job_reconciler import reconciler_loop
            reconciler_task = asyncio.create_task(reconciler_loop(namespace))
            _logger.info('job reconciler launched (namespace=%s)', namespace)
        else:
            _logger.warning(
                'LEARTECH_INITIATIVE_RUNTIME=job but reconciler not launched '
                '(POD_NAMESPACE=%s, db_enabled=%s)', namespace, is_db_enabled(),
            )
    try:
        yield
    finally:
        if reconciler_task is not None:
            reconciler_task.cancel()
            try:
                await reconciler_task
            except asyncio.CancelledError:
                pass
            _logger.info('job reconciler stopped')
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
