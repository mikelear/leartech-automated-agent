"""leartech-ai-gateway-web-mcp — web search / fetch backed by leartech-ai-gateway.

An in-process SDK MCP server exposing two tools:

  * ``web_search(query, max_results?)`` — hit ``<gateway>/v1/search``
  * ``web_fetch(url)`` — hit ``<gateway>/v1/fetch``

Wired specifically for the BA agent (``gate.agent.ba_agent``), which needs to
research the state of the world before authoring a Plan CRD (e.g. "has the
upstream fixed X?", "what's the current release cadence for Y?"). Rather than
give the agent raw HTTP + credential handling, we surface the ai-gateway's
tool-agnostic web layer as MCP tools — same governance / metering as LLM turns.

**Bucket: GAP** (in-process only). The underlying HTTP is in
:mod:`gate.tools.ai_gateway_web`; this module is just the SDK-MCP wrapper.
There is currently no Go equivalent in ``leartech-mcp-servers``, so per the
Go-first rule the shim stays until one lands. When a Go server ships (name
TBD; likely ``ai_gateway_web`` or similar), add it to ``WANTED_MCP_SERVERS``
in :mod:`gate.mcp_servers.remote`, delete this module, and remove the
``leartech-ai-gateway-web`` entry from ``gate/agent/mcp_catalog.yaml``.

Portability note: the wrapper is deliberately thin — an httpx client + a
small JSON schema — so a runtime swap doesn't strand the tool. The
ai-gateway itself is the network boundary the BA agent's search/fetch
traffic crosses; this module just gives the LLM a typed MCP surface over it.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.tools.ai_gateway_web import (
    DEFAULT_MAX_RESULTS,
    AIGatewayWebUnavailableError,
    web_fetch,
    web_search,
)


def _envelope(payload: object) -> dict[str, Any]:
    """Wrap the tool payload in the SDK's ``content`` envelope."""
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


def _error(message: str) -> dict[str, Any]:
    """Uniform error envelope — the calling LLM sees a structured shape it can reason about.

    Kept deliberately minimal (``{"error": "..."}``) so the agent can `except`-style
    check for the error key without parsing a variety of shapes.
    """
    return _envelope({'error': message})


@tool(
    'web_search',
    'Search the web via the ai-gateway. POSTs to <gateway>/v1/search. Returns '
    '{"results": [{title, url, snippet}, ...]}. Use for reconnaissance before '
    'authoring a Plan — e.g. "has upstream fixed <crash class>?", "what is the '
    'current release cadence for <repo>?". Requires ANTHROPIC_BASE_URL + '
    'AI_GATEWAY_API_KEY (or ANTHROPIC_API_KEY) — returns {"error": ...} when '
    'gateway env is unset so the agent can degrade cleanly.',
    {'query': str, 'max_results': int},
)
async def _web_search(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get('query', ''))
    if not query.strip():
        return _error('web_search called with empty query — supply a non-empty query string')
    raw_max = args.get('max_results')
    max_results = (
        int(raw_max) if isinstance(raw_max, (int, float, str)) and str(raw_max).strip() else DEFAULT_MAX_RESULTS
    )
    try:
        hits = web_search(query, max_results=max_results)
    except AIGatewayWebUnavailableError as exc:
        return _error(f'ai-gateway web unavailable: {exc}')
    return _envelope(
        {
            'query': query,
            'max_results': max_results,
            'results': [{'title': hit.title, 'url': hit.url, 'snippet': hit.snippet} for hit in hits],
        }
    )


@tool(
    'web_fetch',
    'Fetch and extract the text content of a URL via the ai-gateway. POSTs to '
    '<gateway>/v1/fetch. Returns {url, title, content, truncated}. Use to read '
    'the body of a search hit before deciding whether the finding is relevant. '
    'Requires ANTHROPIC_BASE_URL + AI_GATEWAY_API_KEY (or ANTHROPIC_API_KEY).',
    {'url': str},
)
async def _web_fetch(args: dict[str, Any]) -> dict[str, Any]:
    url = str(args.get('url', ''))
    if not url.strip():
        return _error('web_fetch called with empty url — supply a full https:// URL')
    try:
        page = web_fetch(url)
    except AIGatewayWebUnavailableError as exc:
        return _error(f'ai-gateway web unavailable: {exc}')
    return _envelope(
        {
            'url': page.url,
            'title': page.title,
            'content': page.content,
            'truncated': page.truncated,
        }
    )


def build_ai_gateway_web_server() -> McpSdkServerConfig:
    """Return the in-process SDK MCP server config for the BA agent's web layer."""
    return create_sdk_mcp_server(
        name='leartech-ai-gateway-web',
        version='0.1.0',
        tools=[_web_search, _web_fetch],
    )
