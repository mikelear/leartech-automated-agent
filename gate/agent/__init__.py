"""Agent SDK loop wrapping the gate.

Reads a PR via the MCP servers (`gate.mcp_servers`), drives Claude through review,
returns the verdict. Read-only in v1; write-driven initiative loop comes next.
"""

from gate.agent.initiative import run_initiative
from gate.agent.main import review_pr

__all__ = ['review_pr', 'run_initiative']
