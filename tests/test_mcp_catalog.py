"""Tests for gate.agent.mcp_catalog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from gate.agent.mcp_catalog import (
    Catalog,
    McpServer,
    Role,
    get_mcp,
    get_role,
    load_catalog,
    reachable_status,
)


def test_load_real_catalog_validates() -> None:
    """The committed mcp_catalog.yaml is valid + parses into the schema."""
    load_catalog.cache_clear()
    catalog = load_catalog()
    # We expect the four in-process MCPs we know about
    for name in (
        'leartech-pipeline',
        'leartech-criteria',
        'leartech-pr-context',
        'leartech-test-artifacts',
    ):
        assert name in catalog.mcp_servers, f'expected {name!r} catalogued'
    # And the four agent roles
    for role in ('initiative_agent', 'review_agent', 'ba_agent', 'forensic_agent'):
        assert role in catalog.roles, f'expected {role!r} role defined'


def test_role_mcps_resolve_in_real_catalog() -> None:
    """No role references an MCP that isn't catalogued."""
    load_catalog.cache_clear()
    catalog = load_catalog()
    for role_name, role in catalog.roles.items():
        for mcp_name in role.mcps:
            assert mcp_name in catalog.mcp_servers, f'role {role_name!r} → unknown MCP {mcp_name!r}'


def test_get_role_returns_typed_config() -> None:
    role = get_role('initiative_agent')
    assert isinstance(role, Role)
    assert 'leartech-pipeline' in role.mcps
    assert 'Bash' in role.tools


def test_get_role_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match='Unknown role'):
        get_role('does-not-exist')


def test_get_mcp_returns_typed_config() -> None:
    mcp = get_mcp('leartech-pipeline')
    assert isinstance(mcp, McpServer)
    assert mcp.type == 'sdk'
    assert mcp.builder == 'gate.mcp_servers.pipeline_server:build_pipeline_server'


def test_invalid_role_referencing_unknown_mcp_fails_validation(tmp_path: Path) -> None:
    bad_catalog = {
        'mcp_servers': {
            'real-mcp': {'type': 'sdk', 'builder': 'mod:fn', 'description': 'd'},
        },
        'roles': {
            'bad_role': {'description': 'd', 'mcps': ['nonexistent-mcp']},
        },
    }
    path = tmp_path / 'bad.yaml'
    path.write_text(yaml.safe_dump(bad_catalog))
    with pytest.raises(Exception, match='unknown MCP'):
        Catalog.model_validate(yaml.safe_load(path.read_text()))


def test_sdk_mcp_requires_builder() -> None:
    with pytest.raises(Exception, match='requires `builder`'):
        McpServer.model_validate({'type': 'sdk', 'description': 'd'})


def test_stdio_mcp_requires_command() -> None:
    with pytest.raises(Exception, match='requires `command`'):
        McpServer.model_validate({'type': 'stdio', 'description': 'd'})


def test_http_sse_mcp_requires_url() -> None:
    with pytest.raises(Exception, match='requires `url`'):
        McpServer.model_validate({'type': 'http_sse', 'description': 'd'})


def test_reachable_status_for_sdk_returns_ready_for_real_builder() -> None:
    mcp = get_mcp('leartech-pipeline')
    assert reachable_status(mcp) == 'ready'


def test_reachable_status_for_unbuilt_mcp_reports_not_built() -> None:
    mcp = get_mcp('stitch')
    assert reachable_status(mcp) == 'not_built'


def test_reachable_status_missing_auth_for_remote_mcp() -> None:
    """If a remote MCP needs an env var that isn't set, status reports missing_auth.

    Patches the status to 'ready' to simulate the post-build state; the env
    var lookup is what we're testing.
    """
    mcp = McpServer.model_validate(
        {
            'type': 'remote',
            'url': 'https://example.com/mcp',
            'description': 'test',
            'status': 'ready',
            'auth': {'type': 'bearer', 'token_env': 'NEVER_SET_TEST_VAR_X9Z'},
        },
    )
    # Make sure the env var really isn't set
    os.environ.pop('NEVER_SET_TEST_VAR_X9Z', None)
    assert reachable_status(mcp) == 'missing_auth'
