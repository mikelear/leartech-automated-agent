"""Tests for gate.agent.mcp_catalog."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gate.agent.mcp_catalog import (
    Catalog,
    LlmConfig,
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
    # Core MCPs shipped in the runtime today. leartech-jx3-flow is the
    # remote replacement for the retired in-process leartech-pipeline shim
    # (list_pr_checks / wait_for_terminal / wait_for_first_failure_or_all_pass).
    for name in (
        'leartech-jx3-flow',
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
    assert 'leartech-jx3-flow' in role.mcps
    assert 'Bash' in role.tools


def test_get_role_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match='Unknown role'):
        get_role('does-not-exist')


def test_get_mcp_returns_typed_config() -> None:
    # leartech-criteria is a stable in-process SDK MCP; leartech-jx3-flow (the
    # remote replacement for the retired pipeline_server shim) is exercised by
    # test_platform_mcps_entries_load_as_http_sse below.
    mcp = get_mcp('leartech-criteria')
    assert isinstance(mcp, McpServer)
    assert mcp.type == 'sdk'
    assert mcp.builder == 'gate.mcp_servers.criteria_server:build_criteria_server'


def test_platform_mcps_entries_load_as_http_sse() -> None:
    """leartech-jx3-flow / leartech-tekton / leartech-pr-context are http_sse-hosted
    via platform-mcps.

    Catches a regression where the catalog reverts an entry to `type: sdk`
    (e.g. an accidental git revert) — operators reading /mcps would see the
    in-process builder rather than the URL deployment they expect to probe.
    """
    load_catalog.cache_clear()
    catalog = load_catalog()

    for name, sse_suffix in (
        ('leartech-tekton', '/mcp/tekton/sse'),
        ('leartech-pr-context', '/mcp/pr-context/sse'),
        ('leartech-jx3-flow', '/mcp/jx3_flow/sse'),
    ):
        assert name in catalog.mcp_servers, f'{name} missing from catalog'
        mcp = catalog.mcp_servers[name]
        assert mcp.type == 'http_sse', f'{name}: expected http_sse, got {mcp.type}'
        assert mcp.url is not None, f'{name}: http_sse MCP must declare url'
        assert mcp.url.endswith(sse_suffix), f'{name}: url {mcp.url!r} does not end with {sse_suffix!r}'
        # The url is env-templated `${LEARTECH_PLATFORM_MCPS_URL:-<dev-fallback>}`
        # and the loader resolves it. With the env var unset (test/dev), it must
        # resolve to the dev-only localhost fallback — NO hardcoded
        # `*.jx.leartech.com` cluster URL in source (leartech convention;
        # ai-review flagged the old staging default). In cluster the chart sets
        # the env var so it resolves to the cluster-local platform-mcps URL.
        assert 'jx.leartech.com' not in mcp.url, (
            f'{name}: no hardcoded cluster URL in the source fallback — got {mcp.url!r}'
        )
        assert 'localhost' in mcp.url, f'{name}: dev fallback should be localhost — got {mcp.url!r}'


def test_platform_mcps_url_overridable_via_env_var(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`LEARTECH_PLATFORM_MCPS_URL` env var rewrites the catalog default URL.

    The chart sets this per-cluster so the same catalog YAML routes to the
    cluster-local platform-mcps deployment instead of going via the staging
    URL. Verifies the `${VAR:-default}` POSIX-shell substitution actually
    resolves at load time, not at lookup.
    """
    monkeypatch.setenv('LEARTECH_PLATFORM_MCPS_URL', 'https://platform-mcps.internal.test')

    # Write a minimal catalog that uses the same env-var pattern as production.
    catalog_yaml = """
mcp_servers:
  leartech-tekton:
    type: http_sse
    url: ${LEARTECH_PLATFORM_MCPS_URL:-https://default.example.com}/mcp/tekton/sse
    description: test
roles:
  initiative_agent:
    description: test
    mcps:
      - leartech-tekton
"""
    path = tmp_path / 'cat.yaml'
    path.write_text(catalog_yaml)
    load_catalog.cache_clear()
    try:
        catalog = load_catalog(path)
    finally:
        load_catalog.cache_clear()
    assert catalog.mcp_servers['leartech-tekton'].url == 'https://platform-mcps.internal.test/mcp/tekton/sse'


def test_platform_mcps_url_falls_back_to_default_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With env var unset, `${VAR:-default}` resolves to the literal default."""
    monkeypatch.delenv('LEARTECH_PLATFORM_MCPS_URL', raising=False)

    catalog_yaml = """
mcp_servers:
  leartech-tekton:
    type: http_sse
    url: ${LEARTECH_PLATFORM_MCPS_URL:-https://fallback.example.com}/mcp/tekton/sse
    description: test
roles:
  initiative_agent:
    description: test
    mcps:
      - leartech-tekton
"""
    path = tmp_path / 'cat.yaml'
    path.write_text(catalog_yaml)
    load_catalog.cache_clear()
    try:
        catalog = load_catalog(path)
    finally:
        load_catalog.cache_clear()
    assert catalog.mcp_servers['leartech-tekton'].url == 'https://fallback.example.com/mcp/tekton/sse'


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
    # leartech-criteria still ships as an in-process SDK MCP (leartech-pipeline
    # was ported to remote leartech-jx3-flow).
    mcp = get_mcp('leartech-criteria')
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


# ─── LlmConfig field tests ────────────────────────────────────────────────────


def _synthetic_role(llm: object = None) -> dict[str, object]:
    """Helper: minimal valid role dict for Catalog-level parse tests."""
    role: dict[str, object] = {'description': 'synthetic role for testing'}
    if llm is not None:
        role['llm'] = llm
    return role


def test_llm_config_optional_field_defaults_to_none() -> None:
    """A role with no `llm:` key must parse with role.llm == None."""
    role = Role.model_validate({'description': 'no llm block here'})
    assert role.llm is None


def test_llm_config_parses_with_explicit_block() -> None:
    """A role with a full `llm:` block must parse into an LlmConfig instance."""
    role = Role.model_validate(
        {
            'description': 'role with llm block',
            'llm': {'backend': 'claude', 'model': 'claude-opus-4-7', 'max_turns': 100},
        }
    )
    assert role.llm is not None
    assert role.llm.backend == 'claude'
    assert role.llm.model == 'claude-opus-4-7'
    assert role.llm.max_turns == 100


def test_llm_config_max_turns_must_be_positive() -> None:
    """`max_turns: 0` must raise ValidationError (ge=1 constraint)."""
    with pytest.raises(ValidationError):
        LlmConfig.model_validate({'max_turns': 0})


def test_llm_config_stop_on_tool_defaults_empty() -> None:
    """An `llm:` block without `stop_on_tool` must yield an empty list."""
    role = Role.model_validate(
        {
            'description': 'role with partial llm block',
            'llm': {'backend': 'claude'},
        }
    )
    assert role.llm is not None
    assert role.llm.stop_on_tool == []


def test_llm_config_extra_field_forbidden() -> None:
    """An unknown field inside `llm:` must raise ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        LlmConfig.model_validate({'backend': 'claude', 'unknown_field': 'oops'})


def test_existing_roles_still_parse() -> None:
    """All four real roles load cleanly; initiative_agent has llm populated; others are None."""
    load_catalog.cache_clear()
    catalog = load_catalog()

    for role_name in ('initiative_agent', 'review_agent', 'ba_agent', 'forensic_agent'):
        assert role_name in catalog.roles, f'expected {role_name!r} in catalog'

    initiative = catalog.roles['initiative_agent']
    assert initiative.llm is not None, 'initiative_agent.llm should be populated'
    assert initiative.llm.backend == 'claude'
    assert initiative.llm.model == 'claude-opus-4-7'
    assert initiative.llm.max_turns == 1000

    # ba_agent pins Opus 4.8 explicitly (NOT "auto") so the gateway's
    # auto-router can't downgrade a reasoning-heavy authoring pass to GLM.
    # This is enforced by test_ba_role_pins_opus_not_auto in test_ba_agent.py.
    ba = catalog.roles['ba_agent']
    assert ba.llm is not None, 'ba_agent.llm should be populated (Opus 4.8 pinned)'
    assert ba.llm.backend == 'claude'
    assert ba.llm.model is not None and 'opus' in ba.llm.model.lower()

    for role_name in ('review_agent', 'forensic_agent'):
        role = catalog.roles[role_name]
        assert role.llm is None, f'{role_name}.llm should be None (no llm block in yaml)'
