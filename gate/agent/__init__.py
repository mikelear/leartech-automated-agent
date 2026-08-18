"""Agent SDK loop wrapping the gate.

Reads a PR via the MCP servers (`gate.mcp_servers`), drives Claude through review,
returns the verdict. Read-only in v1; write-driven initiative loop comes next.
"""

from gate.agent.gcp_credentials import materialize_gcp_credentials

materialize_gcp_credentials()

from gate.agent.initiative import run_initiative  # noqa: E402  (after the credential bootstrap, intentionally)
from gate.agent.main import review_pr  # noqa: E402

__all__ = ['materialize_gcp_credentials', 'review_pr', 'run_initiative']
