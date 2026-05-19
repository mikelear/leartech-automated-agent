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

from fastapi import FastAPI

from app.routers import health, initiatives, introspection, lessons, mcp_admin

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

app = FastAPI(
    title='leartech-automated-agent',
    description='Criteria-driven agent runtime exposed as a long-running service.',
    version='0.2.0',
)

app.include_router(health.router, tags=['health'])
app.include_router(initiatives.router, prefix='/initiatives', tags=['initiatives'])
app.include_router(lessons.router, prefix='/lessons', tags=['lessons'])
app.include_router(introspection.router, tags=['introspection'])
app.include_router(mcp_admin.router, tags=['mcp-admin'])
