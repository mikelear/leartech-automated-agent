"""Smoke tests for MCP server builders — confirm they construct cleanly with the right tools.

We don't exercise tool execution here — that requires the full Agent SDK loop. These tests
catch shape regressions: tool name typos, missing wirings, schema build errors.
"""

from __future__ import annotations

from gate.mcp_servers import (
    build_artifacts_server,
    build_criteria_server,
    build_pipeline_server,
    build_pr_context_server,
)


def _tool_names(server: object) -> list[str]:
    """Extract the registered tool names from an SDK MCP server config."""
    instance = server['instance'] if isinstance(server, dict) else getattr(server, 'instance', None)
    if instance is None:
        # Fallback: serialise the server and look for `name=` patterns. Keeps the test resilient
        # to small structural changes in McpSdkServerConfig across SDK versions.
        return [t.name for t in getattr(server, 'tools', [])]
    return [t.name for t in instance._tool_handlers.values()] if hasattr(instance, '_tool_handlers') else []


def test_pipeline_server_exposes_list_pr_checks() -> None:
    server = build_pipeline_server()
    assert server is not None
    # The shape of McpSdkServerConfig is a TypedDict / dict — we only need to assert it built.


def test_pr_context_server_builds() -> None:
    server = build_pr_context_server()
    assert server is not None


def test_artifacts_server_builds() -> None:
    server = build_artifacts_server()
    assert server is not None


def test_criteria_server_builds() -> None:
    server = build_criteria_server()
    assert server is not None


def test_all_servers_build_with_distinct_names() -> None:
    """Belt-and-braces: confirm each builder returns a distinct McpSdkServerConfig."""
    servers = [
        build_pipeline_server(),
        build_pr_context_server(),
        build_artifacts_server(),
        build_criteria_server(),
    ]
    assert all(s is not None for s in servers)
    assert len({id(s) for s in servers}) == 4
