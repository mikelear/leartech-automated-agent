"""MCP catalog reachability probes.

The shipped ``reachable_status`` in :mod:`gate.agent.mcp_catalog` covers
sdk-import liveness + env-var presence (used by the platform's MCP
gateway). This module wraps that with the operator-facing concept of an
*active probe* — a "would this MCP answer if I asked it now?" call:

- sdk MCPs are always probed via in-process import (cheap; same as
  ``reachable_status``).
- stdio MCPs are probed by checking the command is on PATH (a real
  handshake would require spawning the process, which is too expensive
  for an interactive ``leartech-agent health`` call).
- http_sse / remote MCPs are probed by issuing GET ``<url>/healthz``
  with a 2-second timeout — same convention every leartech FastAPI
  service ships.

The probe is best-effort and intentionally fast; operators reading the
result get a "ready / not_built / missing_auth / down" verdict, not a
deep diagnostic. The deep dive is ``leartech-agent mcps describe`` +
direct kubectl.
"""

from __future__ import annotations

import shutil

import httpx

from gate.agent.mcp_catalog import McpServer, McpStatus, reachable_status

_PROBE_TIMEOUT_SECONDS = 2.0


def probe_mcp(mcp: McpServer) -> McpStatus:
    """Active liveness probe for one MCP catalog entry.

    Returns one of the four :data:`gate.agent.mcp_catalog.McpStatus` values.
    Never raises — network failures, missing tools, and bad URLs all
    collapse to ``'down'``.
    """
    base = reachable_status(mcp)
    # If the static check already says non-ready, trust it. The active
    # probe only refines the 'ready' verdict.
    if base != 'ready':
        return base

    if mcp.type == 'sdk':
        # reachable_status() already imported the builder; we're done.
        return 'ready'

    if mcp.type == 'stdio':
        if not mcp.command:
            return 'down'
        return 'ready' if shutil.which(mcp.command) else 'down'

    if mcp.type in ('http_sse', 'remote'):
        if not mcp.url:
            return 'down'
        try:
            with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                healthz_url = mcp.url.rstrip('/') + '/healthz'
                response = client.get(healthz_url)
                return 'ready' if response.status_code < 500 else 'down'
        except (httpx.HTTPError, OSError):
            return 'down'

    # Unreachable: McpType is a Literal of the four cases above.
    return 'down'
