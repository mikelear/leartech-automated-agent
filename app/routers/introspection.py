"""Introspection endpoints — what's in the platform, what's running, how it fits.

These power the operator CLI (`leartech-agent`) and will power the
future webCoder dashboard. Read-only — no state changes.

Endpoints:
- GET /mcps                         — list MCP catalog with current reachability
- GET /mcps/{name}                  — one MCP's detail
- GET /mcps/{name}/health           — active probe (sdk import / stdio PATH / http GET)
- GET /roles                        — list agent personas
- GET /roles/{name}                 — one role's prompt + MCP scope + tool allowlist
- GET /initiatives/{id}/timeline    — turn-by-turn decision log (MVP: derived from run record)
- GET /initiatives/{id}/why         — lessons matched at session start
- GET /topology                     — Mermaid source for the platform diagram
- GET /topology/feedback            — Mermaid for the three feedback rings
- GET /health/detail                — multi-cluster + ring status summary
"""

from __future__ import annotations

import os
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth import get_current_tenant_id
from app.state import get as get_record
from gate.agent.lessons.loader import Lesson, load_all_lessons
from gate.agent.mcp_catalog import (
    McpServer,
    McpStatus,
    Role,
    load_catalog,
    reachable_status,
)
from gate.introspection.mcp_status import probe_mcp
from gate.introspection.topology import TOPOLOGY_DESCRIPTIONS, render_topology

router = APIRouter()


def _service_version() -> str:
    """Report the deployed release tag.

    Order of precedence:
    1. `VERSION` env var — set by the Helm chart from `.Chart.Version`, which jx-promote
       bumps on every release (mqube org-wide standard, used in 10+ services).
    2. `importlib.metadata.version()` — works for editable + installed pip packages
       but the value is frozen at build time from pyproject.toml, so it lags by design.
    3. `'unknown'` — last-resort fallback for partial installs.
    """
    env_version = os.environ.get('VERSION')
    if env_version:
        return env_version
    try:
        return version('leartech-automated-agent')
    except PackageNotFoundError:
        return 'unknown'


# ─── /mcps ────────────────────────────────────────────────────────────────


class McpSummary(BaseModel):
    name: str
    type: str
    description: str
    status: McpStatus
    roles: list[str]


class McpDetail(BaseModel):
    name: str
    spec: McpServer
    status: McpStatus
    roles: list[str]


class McpHealthResponse(BaseModel):
    name: str
    status: McpStatus
    probe: Literal['sdk_import', 'stdio_path', 'http_healthz', 'static']


def _mcp_to_roles(catalog_mcps: list[str]) -> dict[str, list[str]]:
    """Reverse the role→mcps map into mcp→roles for easy lookup."""
    catalog = load_catalog()
    mcp_to_roles: dict[str, list[str]] = {name: [] for name in catalog.mcp_servers}
    for role_name, role in catalog.roles.items():
        for mcp_name in role.mcps:
            if mcp_name in mcp_to_roles:
                mcp_to_roles[mcp_name].append(role_name)
    return mcp_to_roles


def _probe_kind_for(mcp: McpServer) -> Literal['sdk_import', 'stdio_path', 'http_healthz', 'static']:
    if mcp.type == 'sdk':
        return 'sdk_import'
    if mcp.type == 'stdio':
        return 'stdio_path'
    if mcp.type in ('http_sse', 'remote'):
        return 'http_healthz'
    return 'static'


@router.get('/mcps', response_model=list[McpSummary])
async def list_mcps() -> list[McpSummary]:
    """List every MCP in the catalog with current reachability + which roles use it."""
    catalog = load_catalog()
    mcp_to_roles = _mcp_to_roles(list(catalog.mcp_servers))
    return [
        McpSummary(
            name=name,
            type=mcp.type,
            description=mcp.description,
            status=reachable_status(mcp),
            roles=sorted(mcp_to_roles.get(name, [])),
        )
        for name, mcp in catalog.mcp_servers.items()
    ]


@router.get('/mcps/{name}', response_model=McpDetail)
async def get_mcp_detail(name: str) -> McpDetail:
    """Return the full MCP config + reachability + role membership."""
    catalog = load_catalog()
    if name not in catalog.mcp_servers:
        available = sorted(catalog.mcp_servers)
        raise HTTPException(status_code=404, detail={'message': f'Unknown MCP {name!r}', 'available': available})
    mcp = catalog.mcp_servers[name]
    mcp_to_roles = _mcp_to_roles(list(catalog.mcp_servers))
    return McpDetail(name=name, spec=mcp, status=reachable_status(mcp), roles=sorted(mcp_to_roles.get(name, [])))


@router.get('/mcps/{name}/health', response_model=McpHealthResponse)
async def get_mcp_health(name: str) -> McpHealthResponse:
    """Active liveness probe for one MCP.

    Sdk-type MCPs do an in-process import check. Stdio-type MCPs check the
    binary is on PATH. http_sse / remote MCPs issue ``GET <url>/healthz``
    with a 2-second timeout. Result is the resolved status + a tag
    describing which probe ran (so dashboards can show
    `(probed via http_healthz)` vs `(static — not_built)`).
    """
    catalog = load_catalog()
    if name not in catalog.mcp_servers:
        available = sorted(catalog.mcp_servers)
        raise HTTPException(status_code=404, detail={'message': f'Unknown MCP {name!r}', 'available': available})
    mcp = catalog.mcp_servers[name]
    return McpHealthResponse(name=name, status=probe_mcp(mcp), probe=_probe_kind_for(mcp))


# ─── /roles ───────────────────────────────────────────────────────────────


class RoleSummary(BaseModel):
    name: str
    description: str
    mcp_count: int
    tool_count: int


class RoleDetail(BaseModel):
    name: str
    spec: Role
    lesson_count: int  # number of lessons applies_to this role


@router.get('/roles', response_model=list[RoleSummary])
async def list_roles() -> list[RoleSummary]:
    catalog = load_catalog()
    return [
        RoleSummary(
            name=name,
            description=role.description.strip().split('\n')[0],
            mcp_count=len(role.mcps),
            tool_count=len(role.tools),
        )
        for name, role in catalog.roles.items()
    ]


@router.get('/roles/{name}', response_model=RoleDetail)
async def get_role_detail(name: str) -> RoleDetail:
    catalog = load_catalog()
    if name not in catalog.roles:
        available = sorted(catalog.roles)
        raise HTTPException(status_code=404, detail={'message': f'Unknown role {name!r}', 'available': available})
    role = catalog.roles[name]
    lesson_count = sum(1 for lesson in load_all_lessons() if name in lesson.applies_to)
    return RoleDetail(name=name, spec=role, lesson_count=lesson_count)


# ─── /initiatives/{id}/timeline + /why ───────────────────────────────────


class TimelineEvent(BaseModel):
    """One observable event from a run's lifecycle.

    MVP shape — derived from the run record itself (registered → started_executing
    → finished/error/cancelled). Each event has an ``at`` timestamp and a
    free-form ``note``. Future versions will surface per-turn tool calls
    once the agent's per-turn telemetry is durable.
    """

    at: str
    kind: Literal['registered', 'first_turn', 'pr_opened', 'finished', 'errored', 'cancelled']
    note: str


class TimelineResponse(BaseModel):
    run_id: str
    initiative: str
    events: list[TimelineEvent]


class WhyResponse(BaseModel):
    """Which lessons the calibration loader injected into the run's session.

    MVP: returns every lesson whose ``applies_to`` covers ``initiative_agent``.
    Future versions will record the actual matched-lesson set per run (so
    a run fired with `applies_to: [review_agent]` would see review_agent's
    lessons even though the same agent code drove the loop). For now,
    runs are all initiative_agent so the projection is lossless.
    """

    run_id: str
    initiative: str
    matched_lessons: list[str]
    matched_count: int


@router.get('/initiatives/{initiative_id}/timeline', response_model=TimelineResponse)
async def get_initiative_timeline(initiative_id: str, request: Request) -> TimelineResponse:
    """Per-run timeline derived from the durable run record.

    v7-P1 step 5 — tenant-scoped: cross-tenant lookups return 404 so
    existence is not leaked.
    """
    tenant_id = get_current_tenant_id(request)
    record = await get_record(initiative_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    events: list[TimelineEvent] = [
        TimelineEvent(
            at=record.started_at.isoformat(),
            kind='registered',
            note=f'Run registered (status={record.status}, runtime={record.runtime})',
        ),
    ]
    if record.started_executing_at is not None:
        events.append(
            TimelineEvent(
                at=record.started_executing_at.isoformat(),
                kind='first_turn',
                note='Agent issued its first SDK turn',
            )
        )
    if record.pr_number is not None:
        events.append(
            TimelineEvent(
                # No dedicated pr_opened_at column today — anchor to first_turn
                # if known, else started_at. Same heuristic the catalog uses.
                at=(record.started_executing_at or record.started_at).isoformat(),
                kind='pr_opened',
                note=f'Opened PR #{record.pr_number} on {record.pr_repo or "(repo unknown)"}',
            )
        )
    if record.finished_at is not None:
        if record.status == 'cancelled':
            kind: Literal['cancelled', 'errored', 'finished'] = 'cancelled'
            note = 'Run cancelled by operator'
        elif record.error or record.status == 'failed':
            kind = 'errored'
            note = f'Run failed: {record.error or "(no error message)"}'
        else:
            kind = 'finished'
            note = f'Run finished status={record.status} turns={record.turns or 0} cost=${record.cost_usd or 0:.4f}'
        events.append(TimelineEvent(at=record.finished_at.isoformat(), kind=kind, note=note))
    return TimelineResponse(run_id=record.id, initiative=record.initiative, events=events)


@router.get('/initiatives/{initiative_id}/why', response_model=WhyResponse)
async def get_initiative_why(initiative_id: str, request: Request) -> WhyResponse:
    """Lessons injected at session start for this run.

    v7-P1 step 5 — tenant-scoped: cross-tenant lookups return 404.
    """
    tenant_id = get_current_tenant_id(request)
    record = await get_record(initiative_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')
    lessons: list[Lesson] = load_all_lessons()
    matched = [lesson.id for lesson in lessons if 'initiative_agent' in lesson.applies_to]
    return WhyResponse(
        run_id=record.id,
        initiative=record.initiative,
        matched_lessons=matched,
        matched_count=len(matched),
    )


# ─── /topology ────────────────────────────────────────────────────────────


class TopologyResponse(BaseModel):
    mermaid: str
    description: str


@router.get('/topology', response_model=TopologyResponse)
async def topology() -> TopologyResponse:
    """Mermaid diagram of the full platform — phases + agents + MCPs + feedback rings."""
    return TopologyResponse(mermaid=render_topology('full'), description=TOPOLOGY_DESCRIPTIONS['full'])


@router.get('/topology/feedback', response_model=TopologyResponse)
async def topology_feedback() -> TopologyResponse:
    """Mermaid diagram zoomed to the three feedback rings."""
    return TopologyResponse(mermaid=render_topology('feedback'), description=TOPOLOGY_DESCRIPTIONS['feedback'])


# ─── /health/detail ───────────────────────────────────────────────────────


class RingStatus(BaseModel):
    name: str
    status: Literal['active', 'pending', 'not_wired']
    note: str


class HealthDetailResponse(BaseModel):
    service: str
    version: str
    lessons_loaded: int
    lessons_by_category: dict[str, int]
    lessons_by_status: dict[str, int]
    mcps_total: int
    mcps_ready: int
    mcps_not_built: int
    mcps_missing_auth: int
    mcps_down: int
    roles: list[str]
    feedback_rings: list[RingStatus]


@router.get('/health/detail', response_model=HealthDetailResponse)
async def health_detail() -> HealthDetailResponse:
    """Rich health: lessons catalog stats + MCP catalog reachability + ring wiring state."""
    catalog = load_catalog()
    lessons = load_all_lessons()
    by_category = Counter(lesson.category for lesson in lessons)
    by_status = Counter(lesson.status for lesson in lessons)
    statuses = [reachable_status(mcp) for mcp in catalog.mcp_servers.values()]
    status_count = Counter(statuses)

    rings = [
        RingStatus(name='ring1_pr_gate', status='active', note='auto-captures agent_run + ci_failure source types'),
        RingStatus(
            name='ring2_staging',
            status='pending',
            note='qa-arch result store wires here when ready; POST /lessons endpoint is live',
        ),
        RingStatus(
            name='ring3_forensic',
            status='pending',
            note='qa-arch forensic engine wires here when ready; POST /lessons endpoint is live',
        ),
    ]

    return HealthDetailResponse(
        service='leartech-automated-agent',
        version=_service_version(),
        lessons_loaded=len(lessons),
        lessons_by_category=dict(by_category),
        lessons_by_status=dict(by_status),
        mcps_total=len(catalog.mcp_servers),
        mcps_ready=status_count.get('ready', 0),
        mcps_not_built=status_count.get('not_built', 0),
        mcps_missing_auth=status_count.get('missing_auth', 0),
        mcps_down=status_count.get('down', 0),
        roles=sorted(catalog.roles),
        feedback_rings=rings,
    )
