"""leartech-initiatives-mcp — fire initiatives against the deployed agent service.

Two firing modes, mirroring ``POST /initiatives``:

- ``fire_initiative(name)`` — fire a catalog-resolved initiative by name.
- ``fire_initiative_inline(body)`` — fire a one-shot initiative whose YAML
  body is supplied verbatim, no catalog write needed first.

The inline path exists because some callers (e.g. orchestrator firing a
single fix-up iteration on a failing PR) build the goal at call time —
the body embeds the failing PR's verdict comment and would pollute the
catalog with throwaways. The body is parsed server-side via the same
loader the catalog uses, so the two paths share validation.

Service URL resolves from ``LEARTECH_AGENT_URL`` (env), defaulting to
``http://localhost:8080`` for local dev. The agent SDK + operator CLI
already use this convention.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

DEFAULT_URL = 'http://localhost:8080'
DEFAULT_TIMEOUT = 30.0


def _service_url() -> str:
    """Resolve the deployed agent service base URL.

    Reads ``LEARTECH_AGENT_URL`` (same env the operator CLI uses), falling
    back to ``http://localhost:8080`` for laptop / in-cluster sibling
    services. Mirrors :mod:`app.agent_cli.main` so operators / agents
    share one wiring convention.
    """
    return os.environ.get('LEARTECH_AGENT_URL', DEFAULT_URL)


def _format_response(resp: httpx.Response) -> dict[str, Any]:
    """Turn an httpx Response into the MCP tool reply envelope.

    Returns ``{"content": [{"type": "text", "text": "<json>"}]}``. On
    non-2xx we still return the body as text so the calling agent can
    read the FastAPI ``detail`` field — no exceptions cross the tool
    boundary.
    """
    try:
        payload: Any = resp.json()
    except (ValueError, json.JSONDecodeError):
        payload = {'status_code': resp.status_code, 'text': resp.text}
    else:
        # Tag the HTTP status alongside the JSON body so callers can
        # distinguish 202 (spawn accepted) from 4xx (validation refused).
        payload = {'status_code': resp.status_code, 'body': payload}
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'fire_initiative',
    'Fire a catalog-resolved initiative by name. POSTs `{"initiative": name}` to '
    'the deployed agent service. Returns the initiative record (id, status, job_name, '
    'branch, pr_repo) on 202 — same shape as the HTTP response.',
    {'name': str},
)
async def _fire_initiative(args: dict[str, Any]) -> dict[str, Any]:
    name = str(args['name'])
    async with httpx.AsyncClient(base_url=_service_url(), timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post('/initiatives', json={'initiative': name})
    return _format_response(resp)


@tool(
    'fire_initiative_inline',
    'Fire a one-shot initiative whose YAML body is supplied inline (no catalog write '
    'required first). POSTs `{"initiative_body": body}` to the deployed agent service. '
    'Used when the caller builds the goal at call time — e.g. an orchestrator firing a '
    'single fix-up iteration on a failing PR. Same response shape as fire_initiative.',
    {'body': str},
)
async def _fire_initiative_inline(args: dict[str, Any]) -> dict[str, Any]:
    body = str(args['body'])
    async with httpx.AsyncClient(base_url=_service_url(), timeout=DEFAULT_TIMEOUT) as client:
        resp = await client.post('/initiatives', json={'initiative_body': body})
    return _format_response(resp)


def build_initiatives_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-initiatives',
        version='0.1.0',
        tools=[_fire_initiative, _fire_initiative_inline],
    )
