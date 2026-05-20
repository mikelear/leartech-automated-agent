"""leartech-pipeline-mcp — Tekton pipeline status across both clusters.

Tools:

- `list_pr_checks` — one-shot status snapshot
- `wait_for_terminal` — block until every required check reaches a terminal state.
  Use this when you want to know the final state of every check. Slowest-check-wins.
- `wait_for_first_failure_or_all_pass` — block until ANY check FAILS or ALL checks
  pass. Fail-fast primitive: returns within ~15 s of the first failure (lint failure
  comes back fast even while end2end is still running). Use this between push and
  the next decision point so the agent can iterate as soon as it knows the diff is
  wrong, instead of waiting another 10+ min for slow checks.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from gate.tools import list_pr_checks


@tool(
    'list_pr_checks',
    'List Tekton pipeline checks for a PR across both clusters. '
    'Each check returns: cluster (gcp|az), check (name), state (SUCCESS|FAILURE|ERROR|PENDING|IN_PROGRESS), '
    'pipelinerun (Tekton resource name), passed (bool), failed (bool), terminal (bool).',
    {'repo': str, 'pr_number': int},
)
async def _list_pr_checks(args: dict[str, Any]) -> dict[str, Any]:
    checks = list_pr_checks(str(args['repo']), int(args['pr_number']))
    payload = [
        {
            'cluster': c.cluster,
            'check': c.check,
            'state': c.state,
            'pipelinerun': c.pipelinerun,
            'passed': c.passed,
            'failed': c.failed,
            'terminal': c.terminal,
        }
        for c in checks
    ]
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'wait_for_terminal',
    'Block until every required check on a PR reaches a terminal state (SUCCESS/FAILURE/ERROR). '
    'Use this instead of polling `list_pr_checks` in a loop — the wait happens in a single '
    'subprocess (~zero token cost), not via repeated agent turns. '
    'Returns: {"status": "all_passed"|"some_failed"|"timeout", "exit_code": int, '
    '"checks": [...same shape as list_pr_checks...]}. '
    'Default timeout is 900 seconds (15 min); after timeout, retrigger via '
    '`gh pr comment <pr> --body "/test <check>"` (chatops recovery lesson).',
    {'repo': str, 'pr_number': int, 'timeout_seconds': int},
)
async def _wait_for_terminal(args: dict[str, Any]) -> dict[str, Any]:
    repo = str(args['repo'])
    pr_number = int(args['pr_number'])
    timeout = int(args.get('timeout_seconds') or 900)
    qualified = repo if '/' in repo else f'mikelear/{repo}'

    # `gh pr checks --watch` exits when checks reach terminal state:
    # exit 0  → all required checks passed
    # exit 8  → at least one required check failed
    # other   → gh error (bad PR, auth issue, etc.)
    cmd = ['gh', 'pr', 'checks', str(pr_number), '-R', qualified, '--watch', '--required', '--interval', '30']
    timed_out = False
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        exit_code = result.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        exit_code = 124  # POSIX timeout convention

    # After the wait (or timeout), capture the final state via list_pr_checks for the response payload.
    final_checks = list_pr_checks(repo, pr_number)
    checks_payload = [
        {
            'cluster': c.cluster,
            'check': c.check,
            'state': c.state,
            'pipelinerun': c.pipelinerun,
            'passed': c.passed,
            'failed': c.failed,
            'terminal': c.terminal,
        }
        for c in final_checks
    ]

    if timed_out:
        status = 'timeout'
    elif exit_code == 0:
        status = 'all_passed'
    else:
        status = 'some_failed'

    payload = {
        'status': status,
        'exit_code': exit_code,
        'checks': checks_payload,
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'wait_for_first_failure_or_all_pass',
    'Block until ANY check fails OR every check passes — the fail-fast counterpart of '
    '`wait_for_terminal`. Polls `list_pr_checks` every `poll_seconds` (default 15s) '
    'and exits within one poll of the first FAILURE — so a lint failure surfaces in ~15s '
    'even while end2end is still running. The agent can then iterate immediately on '
    'a fresh commit instead of waiting another 10+ min for slow checks. '
    'Returns: {"status": "first_failure"|"all_passed"|"timeout", "first_failure": '
    '{...one check entry...} | None, "checks": [...full list_pr_checks shape...]}. '
    'Default timeout is 1800 s (30 min — longer than wait_for_terminal because we wait '
    'for true completion, not just terminal state).',
    {'repo': str, 'pr_number': int, 'timeout_seconds': int, 'poll_seconds': int},
)
async def _wait_for_first_failure_or_all_pass(args: dict[str, Any]) -> dict[str, Any]:
    repo = str(args['repo'])
    pr_number = int(args['pr_number'])
    timeout = int(args.get('timeout_seconds') or 1800)
    poll_seconds = max(5, int(args.get('poll_seconds') or 15))

    deadline = time.monotonic() + timeout
    status = 'timeout'
    first_failure: dict[str, Any] | None = None
    final_checks: list[Any] = []

    while time.monotonic() < deadline:
        final_checks = list_pr_checks(repo, pr_number)

        # First-failure short-circuit: any check that reached FAILURE/ERROR
        failed = next((c for c in final_checks if c.failed), None)
        if failed is not None:
            status = 'first_failure'
            first_failure = {
                'cluster': failed.cluster,
                'check': failed.check,
                'state': failed.state,
                'pipelinerun': failed.pipelinerun,
            }
            break

        # All-pass exit: every check is terminal AND passed
        if final_checks and all(c.terminal and c.passed for c in final_checks):
            status = 'all_passed'
            break

        await asyncio.sleep(poll_seconds)

    checks_payload = [
        {
            'cluster': c.cluster,
            'check': c.check,
            'state': c.state,
            'pipelinerun': c.pipelinerun,
            'passed': c.passed,
            'failed': c.failed,
            'terminal': c.terminal,
        }
        for c in final_checks
    ]

    payload = {
        'status': status,
        'first_failure': first_failure,
        'checks': checks_payload,
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


def build_pipeline_server() -> McpSdkServerConfig:
    return create_sdk_mcp_server(
        name='leartech-pipeline',
        version='0.1.0',
        tools=[_list_pr_checks, _wait_for_terminal, _wait_for_first_failure_or_all_pass],
    )
