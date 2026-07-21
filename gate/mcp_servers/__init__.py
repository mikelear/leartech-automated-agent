"""In-process MCP server wrappers around `gate/tools/`.

Each builder returns an `McpSdkServerConfig` ready to pass into `ClaudeAgentOptions(mcp_servers=...)`.
External agents (webCoder Phase 3, Claude Code sessions, future products) consume the same
primitives this gate uses internally — same code, two consumption modes.

`build_pipeline_server()` swaps to the mock implementation when
`LEARTECH_MOCK_PIPELINE_SCENARIO` is set — the swap is opaque to the agent
(same tool names, same return shape). See `pipeline_server_mock.py` for
local-integration-test usage.
"""

import os

from claude_agent_sdk.types import McpSdkServerConfig

from gate.mcp_servers.artifacts_server import build_artifacts_server
from gate.mcp_servers.criteria_server import build_criteria_server
from gate.mcp_servers.initiatives_server import build_initiatives_server
from gate.mcp_servers.pipeline_server import build_pipeline_server as _build_real_pipeline_server
from gate.mcp_servers.tekton import build_tekton_server


def build_pipeline_server() -> McpSdkServerConfig:
    """Build the pipeline MCP server.

    Returns the real Tekton-backed server normally; returns the mock
    server when `LEARTECH_MOCK_PIPELINE_SCENARIO` env var points at a
    scenario YAML file. Production paths must NEVER set that env var.
    """
    if os.environ.get('LEARTECH_MOCK_PIPELINE_SCENARIO'):
        # Lazy import — production never pays the YAML / scenario module cost.
        from gate.mcp_servers.pipeline_server_mock import build_mock_pipeline_server

        return build_mock_pipeline_server()
    return _build_real_pipeline_server()


__all__ = [
    'build_pipeline_server',
    'build_artifacts_server',
    'build_criteria_server',
    'build_initiatives_server',
    'build_tekton_server',
]
