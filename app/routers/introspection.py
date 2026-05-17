"""Introspection endpoints — what's in the platform, what's running, how it fits.

These power the operator CLI (`leartech-agent`) and will power the
future webCoder dashboard. Read-only — no state changes.

Endpoints:
- GET /mcps                   — list MCP catalog with current reachability
- GET /mcps/{name}            — one MCP's detail
- GET /roles                  — list agent personas
- GET /roles/{name}           — one role's prompt + MCP scope + tool allowlist
- GET /topology               — Mermaid source for the platform diagram
- GET /topology/feedback      — Mermaid for the three feedback rings
- GET /health/detail          — multi-cluster + ring status summary
"""

from __future__ import annotations

import os
from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gate.agent.lessons.loader import load_all_lessons
from gate.agent.mcp_catalog import (
    McpServer,
    McpStatus,
    Role,
    load_catalog,
    reachable_status,
)

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


def _mcp_to_roles(catalog_mcps: list[str]) -> dict[str, list[str]]:
    """Reverse the role→mcps map into mcp→roles for easy lookup."""
    catalog = load_catalog()
    mcp_to_roles: dict[str, list[str]] = {name: [] for name in catalog.mcp_servers}
    for role_name, role in catalog.roles.items():
        for mcp_name in role.mcps:
            if mcp_name in mcp_to_roles:
                mcp_to_roles[mcp_name].append(role_name)
    return mcp_to_roles


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


# ─── /topology ────────────────────────────────────────────────────────────


class TopologyResponse(BaseModel):
    mermaid: str
    description: str


@router.get('/topology', response_model=TopologyResponse)
async def topology() -> TopologyResponse:
    """Mermaid diagram of the full platform — phases + agents + MCPs + feedback rings."""
    catalog = load_catalog()
    role_names = sorted(catalog.roles)
    role_to_mcps = {name: catalog.roles[name].mcps for name in role_names}

    lines = ['graph TB']
    lines.append('  subgraph "Phase 1 — BA"')
    lines.append('    Lovable[Lovable mockup]')
    lines.append('    Stitch[Stitch design system]')
    lines.append('    Docs[Docs / customer ask]')
    if 'ba_agent' in role_to_mcps:
        lines.append('    BA[BA agent]')
        for mcp in role_to_mcps['ba_agent']:
            if 'lovable' in mcp:
                lines.append('    Lovable -.->|MCP| BA')
            elif 'stitch' in mcp:
                lines.append('    Stitch -.->|MCP| BA')
        lines.append('    Docs -.->|reads| BA')
    lines.append('  end')

    lines.append('  subgraph "Phase 2 — Architecture"')
    lines.append('    InitSet[initiative-set YAML]')
    lines.append('    SignOff{{Two-track sign-off}}')
    lines.append('    BA -->|outputs| InitSet')
    lines.append('    InitSet --> SignOff')
    lines.append('  end')

    lines.append('  subgraph "Phase 3 — Build"')
    lines.append('    Orch[DAG Orchestrator]')
    lines.append('    Code[Code Agent]')
    lines.append('    SignOff -->|approved| Orch')
    lines.append('    Orch -->|POST /initiatives| Code')
    lines.append('    Code -->|PR| Repo[(consumer repo)]')
    lines.append('  end')

    lines.append('  subgraph "Phase 4 — Feedback"')
    lines.append('    Gate[PR-gate ring]')
    lines.append('    Staging[Staging ring qa-arch]')
    lines.append('    Forensic[Forensic ring qa-arch]')
    lines.append('    Lessons[(lessons catalog)]')
    lines.append('    Repo --> Gate --> Lessons')
    lines.append('    Repo --> Staging --> Lessons')
    lines.append('    Staging --> Forensic --> Lessons')
    lines.append('    Lessons -.->|injected at session start| Code')
    lines.append('    Lessons -.-> BA')
    lines.append('  end')

    mermaid = '\n'.join(lines)
    description = (
        'Full leartech platform — Phase 1 (BA) through Phase 4 (feedback rings) with agent roles + MCP wiring.'
    )
    return TopologyResponse(mermaid=mermaid, description=description)


@router.get('/topology/feedback', response_model=TopologyResponse)
async def topology_feedback() -> TopologyResponse:
    """Mermaid diagram zoomed to the three feedback rings."""
    mermaid = '\n'.join(
        [
            'graph LR',
            '  PR[PR push] --> Ring1{Ring 1<br/>PR-gate}',
            '  Merge[merge to main] --> Stage[staging deploy]',
            '  Stage --> Ring2{Ring 2<br/>Staging}',
            '  Stage --> Ring3{Ring 3<br/>Forensic}',
            '  Ring1 -->|agent_run, ci_failure| Lessons[(lessons catalog)]',
            '  Ring2 -->|staging_test| Lessons',
            '  Ring3 -->|prod_incident| Lessons',
            '  Manual[manual /lesson comment] -->|manual_review| Lessons',
            '  Lessons -.->|calibration injected| Agent[Next agent session]',
        ]
    )
    return TopologyResponse(
        mermaid=mermaid,
        description='The three concentric feedback rings — all converge on the lessons catalog, which calibrates the next agent session.',
    )


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
