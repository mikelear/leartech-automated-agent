"""MCP catalog — loader for `mcp_catalog.yaml`.

The catalog file describes every MCP server and the per-role MCP/tool
allowlists. This loader validates the file structure via Pydantic and
exposes typed accessors:

- `load_catalog()` → full catalog
- `get_role(name)` → Role config (raises KeyError if unknown)
- `get_mcp(name)` → McpServer config

The catalog is intentionally separate from the agent runtime so it can
be edited without touching code. Future hot-reload would watch this file.

See:
- project_mcp_catalog_pattern.md for the convention this implements
- mcp_catalog.yaml for the catalog itself
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CATALOG_PATH = Path(__file__).parent / 'mcp_catalog.yaml'

McpType = Literal['sdk', 'stdio', 'http_sse', 'remote']
McpStatus = Literal['ready', 'not_built', 'down', 'missing_auth']

# Match $ENV_VAR placeholders in catalog values.
_ENV_REF_RE = re.compile(r'\$(\w+)')


class McpAuth(BaseModel):
    """Auth config for http_sse / remote MCP servers."""

    model_config = ConfigDict(extra='forbid')

    type: Literal['bearer']
    token_env: str = Field(description='Environment variable name holding the bearer token.')


class McpServer(BaseModel):
    """One MCP server's deployment + connection details."""

    model_config = ConfigDict(extra='forbid')

    type: McpType
    description: str = Field(min_length=1)
    status: McpStatus = Field(default='ready')

    # type=sdk
    builder: str | None = Field(default=None, description='module:function reference returning McpSdkServerConfig')

    # type=stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    # type=http_sse / remote
    url: str | None = None
    auth: McpAuth | None = None

    @model_validator(mode='after')
    def _check_shape(self) -> McpServer:
        if self.type == 'sdk' and not self.builder:
            raise ValueError('sdk-type MCP requires `builder` (module:function)')
        if self.type == 'stdio' and not self.command:
            raise ValueError('stdio-type MCP requires `command`')
        if self.type in ('http_sse', 'remote') and not self.url:
            raise ValueError(f'{self.type}-type MCP requires `url`')
        return self


class Role(BaseModel):
    """One agent persona's MCP + tool allowlist."""

    model_config = ConfigDict(extra='forbid')

    description: str = Field(min_length=1)
    mcps: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)


class Catalog(BaseModel):
    """The full mcp_catalog.yaml as a typed model."""

    model_config = ConfigDict(extra='forbid')

    mcp_servers: dict[str, McpServer]
    roles: dict[str, Role]

    @model_validator(mode='after')
    def _check_role_mcps_exist(self) -> Catalog:
        for role_name, role in self.roles.items():
            unknown = [m for m in role.mcps if m not in self.mcp_servers]
            if unknown:
                raise ValueError(
                    f'Role {role_name!r} references unknown MCP(s) {unknown!r}; declare them in mcp_servers first.'
                )
        return self


@lru_cache(maxsize=1)
def load_catalog(path: Path | None = None) -> Catalog:
    """Load and validate the catalog. Memoised; call `load_catalog.cache_clear()` to reload."""
    file_path = path or CATALOG_PATH
    raw = yaml.safe_load(file_path.read_text())
    return Catalog.model_validate(raw)


def get_role(name: str) -> Role:
    """Resolve a role's config. Raises KeyError if the role isn't defined."""
    catalog = load_catalog()
    if name not in catalog.roles:
        available = ', '.join(sorted(catalog.roles))
        raise KeyError(f'Unknown role {name!r}. Available roles: {available}')
    return catalog.roles[name]


def get_mcp(name: str) -> McpServer:
    """Resolve one MCP server's config. Raises KeyError if not catalogued."""
    catalog = load_catalog()
    if name not in catalog.mcp_servers:
        available = ', '.join(sorted(catalog.mcp_servers))
        raise KeyError(f'Unknown MCP {name!r}. Available: {available}')
    return catalog.mcp_servers[name]


def reachable_status(mcp: McpServer) -> McpStatus:
    """Best-effort liveness check for an MCP — used by /mcps endpoint.

    For type=sdk: ready if the builder can be imported.
    For others: ready if declared status is 'ready' AND required env vars present;
                missing_auth if env vars unset; not_built/down passed through.
    """
    if mcp.status != 'ready':
        return mcp.status

    if mcp.type == 'sdk':
        # Defer import here so a broken builder doesn't break catalog load.
        try:
            module_name, func_name = (mcp.builder or '').split(':', 1)
            mod = __import__(module_name, fromlist=[func_name])
            getattr(mod, func_name)
            return 'ready'
        except (ImportError, AttributeError, ValueError):
            return 'down'

    # Non-sdk MCPs: check that required env vars are populated.
    missing_envs: list[str] = []
    for value in mcp.env.values():
        for env_var in _ENV_REF_RE.findall(value):
            if not os.environ.get(env_var):
                missing_envs.append(env_var)
    if mcp.auth and not os.environ.get(mcp.auth.token_env):
        missing_envs.append(mcp.auth.token_env)
    if missing_envs:
        return 'missing_auth'
    return 'ready'
