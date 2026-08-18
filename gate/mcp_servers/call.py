"""Deterministic (no-LLM) invocation of a single Go MCP tool.

Used by callers that need a tool result directly rather than through the SDK loop —
``gate/agent/pr_handoff.py``'s per-turn PR checkpoint being the current one. Shares
the bridge's fresh-token / fresh-connection-per-op primitive so token minting and
discovery have one implementation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from gate.mcp_servers.remote import discover_mounts, mint_mcp_token
from gate.mcp_servers.stdio_bridge import _with_downstream

CALL_TIMEOUT = float(os.environ.get('LEARTECH_MCP_CALL_TIMEOUT', '60'))

ToolCaller = Callable[[str, str, str, dict[str, Any]], Awaitable[tuple[dict[str, Any], 'str | None']]]


def _content_text(result: Any) -> str:
    """Concatenate the text of a CallToolResult's content blocks."""
    parts: list[str] = []
    for block in getattr(result, 'content', None) or []:
        text = getattr(block, 'text', None)
        if isinstance(text, str):
            parts.append(text)
    return '\n'.join(parts)


async def call_mcp_tool(
    base_url: str, server: str, tool: str, args: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Call one Go MCP tool and return its structured result.

    Returns ``(structuredContent-or-parsed-JSON, None)`` on success and ``({}, error)``
    on any failure — never raises, so a caller degrades cleanly instead of crashing.
    """
    base = base_url.rstrip('/')
    if not base:
        return {}, 'no MCP base URL (LEARTECH_MCP_URL unset)'
    token = mint_mcp_token()
    if not token:
        return {}, 'could not mint aud=leartech-mcp token (check LEARTECH_AUTH_* env)'
    mounts = discover_mounts(base, token)
    if not mounts:
        return {}, f'MCP discovery failed against {base}'
    path = mounts.get(server)
    if not path:
        return {}, f'MCP host {base} does not advertise server {server!r}'
    try:
        result = await _with_downstream(f'{base}{path}', CALL_TIMEOUT, lambda s: s.call_tool(tool, args))
    except Exception as exc:  # noqa: BLE001 — surface as a clean error, never hang/crash
        return {}, f'{type(exc).__name__}: {exc}'
    if getattr(result, 'isError', False):
        return {}, _content_text(result) or 'tool returned isError'
    structured = getattr(result, 'structuredContent', None)
    if isinstance(structured, dict) and structured:
        return structured, None
    text = _content_text(result)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}, f'tool returned no structured content: {text[:200]}'
    return (parsed if isinstance(parsed, dict) else {'result': parsed}), None
