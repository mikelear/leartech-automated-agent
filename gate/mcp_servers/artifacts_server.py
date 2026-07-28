"""leartech-test-artifacts-mcp — Playwright run + artifact discovery from end2end-ui sticky comments.

**Bucket: GAP** (in-process only). This is a shim over
:mod:`gate.tools.playwright_artifacts` (GitHub PR-sticky parsing) and
:mod:`gate.tools.head_artifact` (HTTP HEAD to public GCS URLs). Both
operations are pure network I/O that a Go MCP could serve just as well,
but ``leartech-mcp-servers`` doesn't (yet) advertise a matching server
— so per the Go-first rule the shim stays in place until a Go
``test_artifacts`` server lands. When it does, add ``test_artifacts``
to ``WANTED_MCP_SERVERS`` in :mod:`gate.mcp_servers.remote`, delete
this module, and drop the ``leartech-test-artifacts`` entry from
``gate/agent/mcp_catalog.yaml`` (mirroring the pr_context / tekton /
jx3_flow / initiatives retirements).
"""

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
