#!/usr/bin/env python3
"""Scenario runner — exercises the mock pipeline MCP without spawning an agent.

Demonstrates that a scenario YAML drives the same tool returns the real agent
would see. No Anthropic API call, no GitHub, no Tekton — just the mock server's
tools called directly.

Usage:
    uv run python scripts/run_mock_scenario.py gate/mcp_servers/mock_scenarios/lint-fail-fast.yaml

Or via Makefile:
    make mock-scenario SCENARIO=lint-fail-fast

Use this to:
- Verify a scenario YAML is well-formed before pointing the agent at it
- Visually see what the agent's MCP tools would return as the scenario advances
- Sanity-check the fail-fast vs full-terminal semantics
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from gate.mcp_servers.pipeline_server_mock import (
    _list_pr_checks,
    _wait_for_first_failure_or_all_pass,
    reset_scenario_for_tests,
)


def _print_table(label: str, payload: list[dict[str, object]]) -> None:
    print(f'\n--- {label} ---')
    if not payload:
        print('  (no checks)')
        return
    cluster_w = max(len(str(c['cluster'])) for c in payload)
    check_w = max(len(str(c['check'])) for c in payload)
    for c in payload:
        marker = '✓' if c['passed'] else ('✗' if c['failed'] else '·')
        print(f"  {marker} {str(c['cluster']):<{cluster_w}}  {str(c['check']):<{check_w}}  {c['state']}")


async def main(scenario_path: Path) -> int:
    if not scenario_path.exists():
        print(f'ERROR: scenario not found at {scenario_path}', file=sys.stderr)
        return 2

    os.environ['LEARTECH_MOCK_PIPELINE_SCENARIO'] = str(scenario_path.resolve())
    reset_scenario_for_tests()  # safety in case module-scoped state lingered

    print(f'Scenario: {scenario_path.name}')
    print(f'Path:     {scenario_path.resolve()}')

    # Step 1: snapshot at t=0 via list_pr_checks
    print('\n=== Snapshot 1 (t≈0) — what the agent sees on first poll ===')
    result = await _list_pr_checks.handler({'repo': 'demo', 'pr_number': 1})
    payload = json.loads(result['content'][0]['text'])
    _print_table('list_pr_checks @ t=0', payload)

    # Step 2: drive wait_for_first_failure_or_all_pass — exits on first FAIL or all PASS
    print('\n=== Step 2 — wait_for_first_failure_or_all_pass ===')
    print('  (polling every 1s, max 60s — the same primitive the agent uses)')
    start = time.monotonic()
    result = await _wait_for_first_failure_or_all_pass.handler({
        'repo': 'demo', 'pr_number': 1, 'timeout_seconds': 60, 'poll_seconds': 1,
    })
    elapsed = time.monotonic() - start
    body = json.loads(result['content'][0]['text'])

    print(f'\n  ← returned in {elapsed:.1f}s with status={body["status"]!r}')
    if body['first_failure']:
        ff = body['first_failure']
        print(f'  first_failure: {ff["cluster"]}/{ff["check"]} → {ff["state"]}')

    _print_table('final checks', body['checks'])

    # Interpret outcome
    print()
    if body['status'] == 'first_failure':
        ff = body['first_failure']
        print(f'⚡ FAIL-FAST: agent would now read {ff["cluster"]}/{ff["check"]} logs,')
        print('   classify the failure, then iterate (or /test if transient).')
        print('   Other checks STILL RUNNING in the real world were short-circuited here.')
    elif body['status'] == 'all_passed':
        print('✓ ALL PASSED: agent would proceed to post the final sticky.')
    else:
        print(f'⏱  TIMEOUT: scenario did not settle within 60s — adjust at_seconds in the YAML.')

    return 0


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('Usage: run_mock_scenario.py <path/to/scenario.yaml>', file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(Path(sys.argv[1]))))
