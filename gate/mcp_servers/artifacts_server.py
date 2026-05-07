"""leartech-test-artifacts-mcp — Playwright run + artifact discovery from end2end-ui sticky comments."""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.tools import head_artifact, read_playwright_runs


@tool(
    'list_playwright_runs',
    'Read end2end-ui sticky comments and return one PlaywrightRun per cluster: '
    'cluster, emoji (white_check_mark|x|warning), verdict, passed_all (bool), passed/total counts, '
    'and the list of artifacts (each: spec_name, kind=screenshot|video|trace, url, cluster).',
    {'repo': str, 'pr_number': int},
)
async def _list_playwright_runs(args: dict[str, Any]) -> dict[str, Any]:
    runs = read_playwright_runs(str(args['repo']), int(args['pr_number']))
    payload = [
        {
            'cluster': run.cluster,
            'emoji': run.emoji,
            'verdict': run.verdict,
            'passed_all': run.passed_all,
            'passed': run.passed,
            'total': run.total,
            'artifacts': [
                {
                    'spec_name': a.spec_name,
                    'kind': a.kind,
                    'url': a.url,
                    'cluster': a.cluster,
                }
                for a in run.artifacts
            ],
        }
        for run in runs
    ]
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'head_artifact',
    'HTTP HEAD a public GCS artifact URL. Returns the status code so callers can verify '
    'an artifact is reachable without downloading. URL must start with https://.',
    {'url': str},
)
async def _head_artifact(args: dict[str, Any]) -> dict[str, Any]:
    status = head_artifact(str(args['url']))
    return {'content': [{'type': 'text', 'text': json.dumps({'status': status, 'reachable': status == 200})}]}


def build_artifacts_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-test-artifacts',
        version='0.1.0',
        tools=[_list_playwright_runs, _head_artifact],
    )
