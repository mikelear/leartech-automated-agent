"""In-process MCP server wrappers around `gate/tools/`.

Each builder returns an `McpSdkServerConfig` ready to pass into `ClaudeAgentOptions(mcp_servers=...)`.
External agents (webCoder Phase 3, Claude Code sessions, future products) consume the same
primitives this gate uses internally — same code, two consumption modes.
"""

from gate.mcp_servers.artifacts_server import build_artifacts_server
from gate.mcp_servers.criteria_server import build_criteria_server
from gate.mcp_servers.pipeline_server import build_pipeline_server
from gate.mcp_servers.pr_context_server import build_pr_context_server

__all__ = [
    'build_pipeline_server',
    'build_pr_context_server',
    'build_artifacts_server',
    'build_criteria_server',
]
