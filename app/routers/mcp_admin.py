"""MCP admin endpoints — manage the platform's MCP catalog via GitOps PRs.

These endpoints do NOT mutate in-memory state. Every change opens a PR back to
leartech-automated-agent with the requested modification to gate/agent/mcp_catalog.yaml.
The change becomes live only after PR merge + release + redeploy, preserving the GitOps flow.

Endpoints:
- POST   /mcps                  — register a new MCP server
- DELETE /mcps/{name}           — deregister an MCP server (detach all roles first)
- PUT    /mcps/{name}/roles     — grant or revoke role membership for an MCP

Phase 0 of the conductor architecture (see memory/project_conductor_architecture.md).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from gate.agent.mcp_catalog import CATALOG_PATH, load_catalog
from gate.tools.pr_back import open_yaml_change_pr

router = APIRouter()

_REPO = 'leartech-automated-agent'
_BASE_BRANCH = 'main'
_CATALOG_FILE = 'gate/agent/mcp_catalog.yaml'


def _read_raw_catalog() -> dict[str, Any]:
    """Read the YAML catalog file directly, bypassing the lru_cache."""
    loaded = yaml.safe_load(CATALOG_PATH.read_text())
    if not isinstance(loaded, dict):
        raise TypeError(f'mcp_catalog.yaml must parse as a dict, got {type(loaded).__name__}')
    return loaded


def _dump_catalog(raw: dict[str, Any]) -> str:
    """Serialize a catalog dict back to YAML (no comments, preserves key order)."""
    return yaml.safe_dump(raw, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _unique_branch(prefix: str) -> str:
    """Return a branch name with a random 8-char hex suffix."""
    return f'{prefix}-{uuid.uuid4().hex[:8]}'


# ─── Shared response model ────────────────────────────────────────────────────


class McpAdminResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    pr_url: str = Field(description='GitHub PR URL where the change can be reviewed + merged')
    pr_number: int
    branch: str
    change_summary: str = Field(description='One-line description of the change, e.g. "Added MCP \'foo\' (type=stdio)"')


# ─── POST /mcps ──────────────────────────────────────────────────────────────


class McpRegisterRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Unique MCP server name')
    type: Literal['sdk', 'stdio', 'http_sse', 'remote']
    description: str = Field(description='One-line human-readable description')
    builder: str | None = Field(default=None, description='module:function ref (required if type==sdk)')
    command: str | None = Field(default=None, description='subprocess command (required if type==stdio)')
    args: list[str] | None = Field(default=None, description='subprocess args (only relevant for type==stdio)')
    url: str | None = Field(default=None, description='endpoint URL (required if type in http_sse, remote)')
    auth: dict[str, Any] | None = Field(default=None, description='auth config (optional)')
    status: Literal['ready', 'not_built'] = Field(default='ready')

    @model_validator(mode='after')
    def _check_type_fields(self) -> McpRegisterRequest:
        if self.type == 'sdk' and not self.builder:
            raise ValueError('sdk-type MCP requires `builder` (module:function)')
        if self.type == 'stdio' and not self.command:
            raise ValueError('stdio-type MCP requires `command`')
        if self.type in ('http_sse', 'remote') and not self.url:
            raise ValueError(f'{self.type}-type MCP requires `url`')
        return self


@router.post('/mcps', response_model=McpAdminResponse, status_code=201)
async def register_mcp(req: McpRegisterRequest) -> McpAdminResponse:
    """Register a new MCP server. Opens a PR with the change; does not mutate runtime state."""
    catalog = load_catalog()
    if req.name in catalog.mcp_servers:
        raise HTTPException(status_code=409, detail=f'MCP {req.name!r} already exists in catalog')

    raw = _read_raw_catalog()

    # Build the new server entry — only include non-default / non-None fields
    server_entry: dict[str, Any] = {
        'type': req.type,
        'description': req.description,
    }
    if req.builder is not None:
        server_entry['builder'] = req.builder
    if req.command is not None:
        server_entry['command'] = req.command
    if req.args is not None:
        server_entry['args'] = req.args
    if req.url is not None:
        server_entry['url'] = req.url
    if req.auth is not None:
        server_entry['auth'] = req.auth
    if req.status != 'ready':
        server_entry['status'] = req.status

    raw['mcp_servers'][req.name] = server_entry
    new_content = _dump_catalog(raw)

    branch = _unique_branch(f'agent/mcp-add-{req.name}')
    result = await open_yaml_change_pr(
        repo=_REPO,
        base_branch=_BASE_BRANCH,
        new_branch=branch,
        file_path=_CATALOG_FILE,
        new_yaml_content=new_content,
        commit_message=f'feat(mcps): add {req.name} (type={req.type})',
        pr_title=f'feat(mcps): add {req.name}',
        pr_body=f'Adds MCP `{req.name}` (type=`{req.type}`) to `{_CATALOG_FILE}`.\n\n> Generated by POST /mcps',
    )

    return McpAdminResponse(
        pr_url=result['pr_url'],
        pr_number=result['pr_number'],
        branch=result['branch'],
        change_summary=f"Added MCP '{req.name}' (type={req.type})",
    )


# ─── DELETE /mcps/{name} ─────────────────────────────────────────────────────


@router.delete('/mcps/{name}', response_model=McpAdminResponse)
async def deregister_mcp(name: str) -> McpAdminResponse:
    """Remove an MCP server. Rejects if any role still references it — caller must detach first."""
    catalog = load_catalog()
    if name not in catalog.mcp_servers:
        raise HTTPException(status_code=404, detail=f'MCP {name!r} not found in catalog')

    referencing_roles = sorted(role_name for role_name, role in catalog.roles.items() if name in role.mcps)
    if referencing_roles:
        raise HTTPException(
            status_code=409,
            detail=f'MCP {name!r} is still referenced by roles {referencing_roles!r}; detach those roles first',
        )

    raw = _read_raw_catalog()
    del raw['mcp_servers'][name]
    new_content = _dump_catalog(raw)

    branch = _unique_branch(f'agent/mcp-remove-{name}')
    result = await open_yaml_change_pr(
        repo=_REPO,
        base_branch=_BASE_BRANCH,
        new_branch=branch,
        file_path=_CATALOG_FILE,
        new_yaml_content=new_content,
        commit_message=f'feat(mcps): remove {name}',
        pr_title=f'feat(mcps): remove {name}',
        pr_body=f'Removes MCP `{name}` from `{_CATALOG_FILE}`.\n\n> Generated by DELETE /mcps/{name}',
    )

    return McpAdminResponse(
        pr_url=result['pr_url'],
        pr_number=result['pr_number'],
        branch=result['branch'],
        change_summary=f"Removed MCP '{name}'",
    )


# ─── PUT /mcps/{name}/roles ───────────────────────────────────────────────────


class McpRolesUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    grant: list[str] = Field(default_factory=list, description='Roles to ADD this MCP to (must exist in catalog)')
    revoke: list[str] = Field(default_factory=list, description='Roles to REMOVE this MCP from')


@router.put('/mcps/{name}/roles', response_model=McpAdminResponse)
async def update_mcp_roles(name: str, req: McpRolesUpdate) -> McpAdminResponse:
    """Grant or revoke role membership for an MCP. Opens a PR with the resulting YAML diff."""
    catalog = load_catalog()
    if name not in catalog.mcp_servers:
        raise HTTPException(status_code=404, detail=f'MCP {name!r} not found in catalog')

    # Validate grant requests before touching anything
    for role_name in req.grant:
        if role_name not in catalog.roles:
            raise HTTPException(status_code=400, detail=f'Role {role_name!r} does not exist in catalog')
        if name in catalog.roles[role_name].mcps:
            raise HTTPException(
                status_code=400,
                detail=f'MCP {name!r} is already in role {role_name!r}; cannot grant again',
            )

    # Validate revoke requests before touching anything
    for role_name in req.revoke:
        if role_name not in catalog.roles:
            raise HTTPException(status_code=400, detail=f'Role {role_name!r} does not exist in catalog')
        if name not in catalog.roles[role_name].mcps:
            raise HTTPException(
                status_code=400,
                detail=f'MCP {name!r} is not in role {role_name!r}; cannot revoke',
            )

    raw = _read_raw_catalog()
    for role_name in req.grant:
        raw['roles'][role_name].setdefault('mcps', []).append(name)
    for role_name in req.revoke:
        raw['roles'][role_name]['mcps'].remove(name)

    new_content = _dump_catalog(raw)

    ops: list[str] = []
    if req.grant:
        ops.append(f'grant to {req.grant}')
    if req.revoke:
        ops.append(f'revoke from {req.revoke}')
    change_desc = '; '.join(ops)

    branch = _unique_branch(f'agent/mcp-roles-{name}')
    result = await open_yaml_change_pr(
        repo=_REPO,
        base_branch=_BASE_BRANCH,
        new_branch=branch,
        file_path=_CATALOG_FILE,
        new_yaml_content=new_content,
        commit_message=f'feat(mcps): update roles for {name}: {change_desc}',
        pr_title=f'feat(mcps): update roles for {name}',
        pr_body=f'Updates role membership for `{name}`: {change_desc}.\n\n> Generated by PUT /mcps/{name}/roles',
    )

    return McpAdminResponse(
        pr_url=result['pr_url'],
        pr_number=result['pr_number'],
        branch=result['branch'],
        change_summary=f"Updated roles for MCP '{name}': {change_desc}",
    )
