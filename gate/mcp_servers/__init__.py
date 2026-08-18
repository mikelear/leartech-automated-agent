"""MCP wiring for the agent.

Every tool the agent calls is served by the Go ``leartech-mcp-servers`` deployment.
This package holds only the transport: :mod:`gate.mcp_servers.remote` discovers the
live server set and wires each one, and :mod:`gate.mcp_servers.stdio_bridge` holds
the authenticated connection (the Claude Code CLI does not forward a static
Authorization header for ``type: http`` MCPs, and the aud=leartech-mcp token is
short-lived, so the bridge mints a fresh one per call).
"""

from gate.mcp_servers.remote import build_remote_mcp_servers

__all__ = ['build_remote_mcp_servers']
