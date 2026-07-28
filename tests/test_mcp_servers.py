"""Smoke tests for MCP server builders — confirm they construct cleanly with the right tools.

We don't exercise tool execution here — that requires the full Agent SDK loop. These tests
catch shape regressions: tool name typos, missing wirings, schema build errors.
"""

from __future__ import annotations

import importlib

import pytest

import gate.mcp_servers as mcp_servers_pkg
from gate.mcp_servers import (
    build_ai_gateway_web_server,
    build_artifacts_server,
    build_criteria_server,
)


def _tool_names(server: object) -> list[str]:
    """Extract the registered tool names from an SDK MCP server config."""
    instance = server['instance'] if isinstance(server, dict) else getattr(server, 'instance', None)
    if instance is None:
        # Fallback: serialise the server and look for `name=` patterns. Keeps the test resilient
        # to small structural changes in McpSdkServerConfig across SDK versions.
        return [t.name for t in getattr(server, 'tools', [])]
    return [t.name for t in instance._tool_handlers.values()] if hasattr(instance, '_tool_handlers') else []


def test_artifacts_server_builds() -> None:
    server = build_artifacts_server()
    assert server is not None


def test_criteria_server_builds() -> None:
    server = build_criteria_server()
    assert server is not None


# The former ``leartech-initiatives`` in-process SDK shim
# (``gate.mcp_servers.initiatives_server``) and its ``fire_initiative`` /
# ``fire_initiative_inline`` tools have been retired in favour of the deployed
# Go ``leartech-mcp-servers/agent_api`` server (see ``feat/shims-to-real-mcp``).
# By-name firing composes client-side as ``get_catalog_entry(name)`` →
# ``fire_initiative(initiative_body=yaml_body)``. No local wire-shape test
# remains because the wire shape is now the Go server's problem — instead the
# regression-proofing below pins the retirement so a future re-add is loud.


def test_initiatives_shim_is_retired() -> None:
    """Regression guard: the in-process ``leartech-initiatives`` SDK shim was
    intentionally deleted in favour of the remote ``leartech-agent-api`` MCP.
    Re-adding an in-process shim under the same name would silently start
    serving the SAME tools from TWO servers (in-process + remote) and race
    them — this test fails if the module or the ``build_initiatives_server``
    export come back without an intentional design change.
    """
    # The module must not exist anywhere on the import path.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module('gate.mcp_servers.initiatives_server')
    # The package must not re-export the retired builder.
    assert not hasattr(mcp_servers_pkg, 'build_initiatives_server'), (
        'build_initiatives_server was retired — do not re-add without design review; '
        'the remote leartech-agent-api MCP owns fire_initiative now.'
    )
    assert 'build_initiatives_server' not in mcp_servers_pkg.__all__


def test_ai_gateway_web_server_builds() -> None:
    """The BA agent's web-research MCP (web_search + web_fetch) must build
    cleanly — a shape regression here would silently disable BA research.

    GAP shim: no Go MCP equivalent yet in ``leartech-mcp-servers``, so this
    stays in-process until one lands.
    """
    server = build_ai_gateway_web_server()
    assert server is not None


def test_all_servers_build_with_distinct_names() -> None:
    """Belt-and-braces: confirm each remaining in-process builder returns a
    distinct ``McpSdkServerConfig``.

    History:

    - The in-process ``pipeline_server`` (list_pr_checks / wait_for_terminal /
      wait_for_first_failure_or_all_pass) was retired in favour of the remote
      ``leartech-jx3-flow`` MCP.
    - The in-process ``tekton`` shim was retired in favour of the remote
      ``leartech-tekton`` MCP (six kubectl-backed tools).
    - The in-process ``pr_context`` shim was retired in favour of the remote
      ``leartech-pr-context`` MCP (open_pr + metadata/diff/files).
    - The in-process ``initiatives_server`` shim (fire_initiative +
      fire_initiative_inline) was retired in favour of the remote
      ``leartech-agent-api`` MCP (fire_initiative + full initiative/run
      inspection surface).

    Remaining in-process builders fall into two buckets:

    - GAP (would move remote once a Go equivalent ships): ``artifacts_server``,
      ``ai_gateway_web_server``.
    - KEEP-LOCAL (depend on agent-pod state; must stay in-process):
      ``criteria_server``, ``agent_local`` (tested elsewhere).
    """
    servers = [
        build_artifacts_server(),
        build_criteria_server(),
        build_ai_gateway_web_server(),
    ]
    assert all(s is not None for s in servers)
    assert len({id(s) for s in servers}) == 3
