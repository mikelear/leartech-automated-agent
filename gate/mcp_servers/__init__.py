"""In-process MCP server wrappers around `gate/tools/`.

Each builder returns an `McpSdkServerConfig` ready to pass into `ClaudeAgentOptions(mcp_servers=...)`.
External agents (webCoder Phase 3, Claude Code sessions, future products) consume the same
primitives this gate uses internally — same code, two consumption modes.

The Tekton tool surface (list_pipelineruns_for_pr / step_status / step_logs /
cancel_pipelinerun / cancel_superseded_for_pr / wait_first_failure) now lives
in the Go ``leartech-mcp-servers/tekton`` deployment at
``${LEARTECH_MCP_URL}/mcp/tekton`` and is wired through
:mod:`gate.mcp_servers.remote`. The two tools that couldn't move remote —
``classify_step_failure`` and ``rebase_branch_on_base`` — moved to
:mod:`gate.mcp_servers.agent_local` under the ``leartech-agent-local`` MCP.

The PR-check status surface (list_pr_checks / wait_for_terminal /
wait_for_first_failure_or_all_pass) — previously served by an in-process
``gate.mcp_servers.pipeline_server`` shim — was ported to the Go
``leartech-mcp-servers/jx3_flow`` deployment at
``${LEARTECH_MCP_URL}/mcp/jx3_flow`` and is wired through the same remote-MCP
registry. The in-process shim (and its mock counterpart used only for
local integration harnesses) has been removed; the agent now consumes the
remote server via authed Streamable-HTTP alongside pr_context + tekton.
"""

from gate.mcp_servers.agent_local import build_agent_local_server
from gate.mcp_servers.artifacts_server import build_artifacts_server
from gate.mcp_servers.criteria_server import build_criteria_server
from gate.mcp_servers.initiatives_server import build_initiatives_server
from gate.mcp_servers.remote import build_remote_mcp_servers

__all__ = [
    'build_agent_local_server',
    'build_artifacts_server',
    'build_criteria_server',
    'build_initiatives_server',
    'build_remote_mcp_servers',
]
