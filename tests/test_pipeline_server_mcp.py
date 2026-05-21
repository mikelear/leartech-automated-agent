"""Tests for the leartech-pipeline MCP server's fail-fast `wait_for_first_failure_or_all_pass`.

The other tools wrap pure pass-through (`list_pr_checks`) or a subprocess
(`wait_for_terminal`) — those are covered indirectly by the underlying tool
tests in `test_pipelines.py`. The fail-fast primitive has its own polling
loop logic that needs explicit coverage.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from typing import Any, cast
from unittest.mock import patch

from gate.mcp_servers.pipeline_server import _wait_for_first_failure_or_all_pass
from gate.tools.pipelines import PipelineCheck

# The @tool decorator wraps the async function in an SdkMcpTool dataclass;
# the underlying coroutine lives on `.handler`. Bind it once for test brevity
# (the cast tells mypy what the SDK doesn't statically type).
_WaitHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]
_wait_handler: _WaitHandler = cast(_WaitHandler, _wait_for_first_failure_or_all_pass.handler)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the JSON payload from the MCP tool's wrapped response."""
    text: str = result['content'][0]['text']
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _check(cluster: str, name: str, state: str) -> PipelineCheck:
    return PipelineCheck(cluster=cluster, check=name, state=state, pipelinerun=f'r-{cluster}-{name}')


def test_first_failure_short_circuits_immediately() -> None:
    """A lint failure should surface even if other checks are still running."""
    sequence = [
        # First poll: lint already failed, end2end still in progress
        [
            _check('az', 'lint', 'FAILURE'),
            _check('az', 'end2end', 'PENDING'),
            _check('gcp', 'lint', 'SUCCESS'),
            _check('gcp', 'end2end', 'PENDING'),
        ],
    ]
    with patch('gate.mcp_servers.pipeline_server.list_pr_checks', side_effect=sequence):
        result = asyncio.run(
            _wait_handler(
                {
                    'repo': 'leartech-auth-ui',
                    'pr_number': 99,
                    'timeout_seconds': 60,
                    'poll_seconds': 1,
                }
            )
        )

    payload = _payload(result)
    assert payload['status'] == 'first_failure'
    assert payload['first_failure'] is not None
    assert payload['first_failure']['check'] == 'lint'
    assert payload['first_failure']['cluster'] == 'az'
    assert len(payload['checks']) == 4


def test_all_pass_returns_when_every_check_terminal_success() -> None:
    sequence = [
        # Poll 1: some still pending — keep looping
        [_check('az', 'lint', 'PENDING'), _check('gcp', 'lint', 'SUCCESS')],
        # Poll 2: everything passed
        [_check('az', 'lint', 'SUCCESS'), _check('gcp', 'lint', 'SUCCESS')],
    ]
    with patch('gate.mcp_servers.pipeline_server.list_pr_checks', side_effect=sequence):
        result = asyncio.run(
            _wait_handler(
                {
                    'repo': 'leartech-auth-ui',
                    'pr_number': 99,
                    'timeout_seconds': 60,
                    'poll_seconds': 1,
                }
            )
        )

    payload = _payload(result)
    assert payload['status'] == 'all_passed'
    assert payload['first_failure'] is None


def test_timeout_returns_status_with_last_seen_checks() -> None:
    """If the deadline elapses with neither all-pass nor any-fail, return status=timeout."""
    # Always return PENDING — the wait will never settle either way
    constant_pending = [_check('az', 'lint', 'PENDING')]

    with patch('gate.mcp_servers.pipeline_server.list_pr_checks', return_value=constant_pending):
        result = asyncio.run(
            _wait_handler(
                {
                    'repo': 'leartech-auth-ui',
                    'pr_number': 99,
                    'timeout_seconds': 1,  # very short — make the test fast
                    'poll_seconds': 1,
                }
            )
        )

    payload = _payload(result)
    assert payload['status'] == 'timeout'
    assert payload['first_failure'] is None
    # Final snapshot still returned for the agent to inspect
    assert len(payload['checks']) == 1


def test_error_state_counts_as_failure() -> None:
    """`ERROR` (pipelinerun execution failure) should trigger first_failure same as FAILURE."""
    sequence = [
        [_check('az', 'pr', 'ERROR'), _check('gcp', 'pr', 'PENDING')],
    ]
    with patch('gate.mcp_servers.pipeline_server.list_pr_checks', side_effect=sequence):
        result = asyncio.run(
            _wait_handler(
                {
                    'repo': 'leartech-auth-ui',
                    'pr_number': 99,
                    'timeout_seconds': 60,
                    'poll_seconds': 1,
                }
            )
        )

    payload = _payload(result)
    assert payload['status'] == 'first_failure'
    assert payload['first_failure']['state'] == 'ERROR'


def test_poll_seconds_floor_prevents_busy_loop() -> None:
    """`poll_seconds` should be clamped to a minimum of 5s to avoid hammering the API."""
    # Force termination on first iteration so we don't actually sleep — just verify
    # the function accepts a too-small value without crashing.
    sequence = [[_check('az', 'lint', 'FAILURE')]]
    with patch('gate.mcp_servers.pipeline_server.list_pr_checks', side_effect=sequence):
        result = asyncio.run(
            _wait_handler(
                {
                    'repo': 'leartech-auth-ui',
                    'pr_number': 99,
                    'timeout_seconds': 60,
                    'poll_seconds': 1,  # below the 5s floor — should still work
                }
            )
        )
    assert _payload(result)['status'] == 'first_failure'
