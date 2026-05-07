"""leartech-pr-context-mcp — PR metadata + diff via gh CLI / REST API."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.tools import added_files, fetch_pr_diff, load_pr_context


@tool(
    'get_pr_metadata',
    'Load canonical PR context: head SHA, base SHA, title, body, state (OPEN|CLOSED|MERGED), '
    'and the list of changed file paths.',
    {'repo': str, 'pr_number': int},
)
async def _get_pr_metadata(args: dict[str, Any]) -> dict[str, Any]:
    ctx = load_pr_context(str(args['repo']), int(args['pr_number']))
    payload = {
        'repo': ctx.repo,
        'number': ctx.number,
        'head_sha': ctx.head_sha,
        'base_sha': ctx.base_sha,
        'title': ctx.title,
        'body': ctx.body,
        'state': ctx.state,
        'changed_files': list(ctx.changed_files),
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'get_pr_diff',
    'Return the unified diff for a PR as a single string. Pass `pattern` (e.g. ".spec.ts") '
    'to instead return only the list of changed file paths matching that suffix.',
    {'repo': str, 'pr_number': int, 'pattern': str},
)
async def _get_pr_diff(args: dict[str, Any]) -> dict[str, Any]:
    repo = str(args['repo'])
    pr_number = int(args['pr_number'])
    pattern = str(args.get('pattern') or '')
    diff = fetch_pr_diff(repo, pr_number)
    if pattern:
        files = added_files(diff, pattern=pattern)
        return {'content': [{'type': 'text', 'text': json.dumps({'files': files}, indent=2)}]}
    return {'content': [{'type': 'text', 'text': diff}]}


def build_pr_context_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-pr-context',
        version='0.1.0',
        tools=[_get_pr_metadata, _get_pr_diff],
    )
