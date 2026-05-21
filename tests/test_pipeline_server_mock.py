"""Tests for the mock pipeline MCP server (`gate.mcp_servers.pipeline_server_mock`).

The mock exists to support local agent integration testing. Same tool names +
signatures as the real server, scripted responses driven by elapsed wall-clock.
These tests verify the scenario loader, the time-based event lookup, and the
end-to-end tool behaviour (list_pr_checks / wait_for_terminal /
wait_for_first_failure_or_all_pass) against synthetic scenarios.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from gate.mcp_servers import pipeline_server_mock as mock_mod
from gate.mcp_servers.pipeline_server_mock import (
    MockCheck,
    Scenario,
    ScenarioEvent,
    _list_pr_checks,
    _wait_for_first_failure_or_all_pass,
    _wait_for_terminal,
    reset_scenario_for_tests,
)

_Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]
_list_handler: _Handler = cast(_Handler, _list_pr_checks.handler)
_wait_terminal: _Handler = cast(_Handler, _wait_for_terminal.handler)
_wait_first: _Handler = cast(_Handler, _wait_for_first_failure_or_all_pass.handler)


@pytest.fixture(autouse=True)
def _clean_scenario_state() -> Iterator[None]:
    """Each test starts with no scenario loaded."""
    reset_scenario_for_tests()
    yield
    reset_scenario_for_tests()


def _payload(result: dict[str, Any]) -> Any:
    return json.loads(result['content'][0]['text'])


# ─── Scenario loading ─────────────────────────────────────────────────────


def test_scenario_loads_real_scenarios_from_disk() -> None:
    """All scenario YAMLs in mock_scenarios/ parse cleanly."""
    scenarios_dir = Path(mock_mod.__file__).parent / 'mock_scenarios'
    files = sorted(scenarios_dir.glob('*.yaml'))
    assert len(files) >= 3, f'Expected at least 3 example scenarios, found {len(files)}'

    for path in files:
        scenario = Scenario.from_yaml(path)
        assert scenario.name, f'{path.name}: empty name'
        assert scenario._events, f'{path.name}: no events'  # noqa: SLF001 — test internal access
        # Every event should have well-typed checks
        for event in scenario._events:  # noqa: SLF001
            for c in event.checks:
                assert c.state in {'PENDING', 'IN_PROGRESS', 'SUCCESS', 'FAILURE', 'ERROR'}, (
                    f'{path.name}: invalid state {c.state!r} on {c.cluster}/{c.check}'
                )


def test_scenario_rejects_empty_events_list() -> None:
    with pytest.raises(ValueError, match='no events'):
        Scenario(name='empty', description='', events=[])


def test_scenario_returns_first_event_before_elapsed_zero() -> None:
    """Before any time has passed, the scenario returns the first event's checks."""
    events = [
        ScenarioEvent(at_seconds=0, checks=(MockCheck('az', 'lint', 'PENDING'),)),
        ScenarioEvent(at_seconds=60, checks=(MockCheck('az', 'lint', 'SUCCESS'),)),
    ]
    scenario = Scenario(name='s', description='', events=events)
    checks = scenario.current_checks()
    assert checks[0].state == 'PENDING'


# ─── Tool wrappers — bind env var, then exercise ─────────────────────────


def _write_scenario(tmp_path: Path, body: dict[str, Any]) -> Path:
    import yaml

    path = tmp_path / 'scenario.yaml'
    path.write_text(yaml.safe_dump(body))
    return path


def test_list_pr_checks_returns_active_event(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _write_scenario(
        tmp_path,
        {
            'name': 'static-failure',
            'events': [
                {'at_seconds': 0, 'checks': [{'cluster': 'az', 'check': 'lint', 'state': 'FAILURE'}]},
            ],
        },
    )
    monkeypatch.setenv('LEARTECH_MOCK_PIPELINE_SCENARIO', str(path))

    result = asyncio.run(_list_handler({'repo': 'leartech-x', 'pr_number': 1}))
    payload = _payload(result)
    assert len(payload) == 1
    assert payload[0]['cluster'] == 'az'
    assert payload[0]['check'] == 'lint'
    assert payload[0]['state'] == 'FAILURE'
    assert payload[0]['failed'] is True
    assert payload[0]['terminal'] is True
    assert payload[0]['passed'] is False


def test_wait_for_first_failure_short_circuits(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """fail-fast tool returns first_failure as soon as a check is FAILURE."""
    path = _write_scenario(
        tmp_path,
        {
            'name': 'immediate-fail',
            'events': [
                {
                    'at_seconds': 0,
                    'checks': [
                        {'cluster': 'az', 'check': 'lint', 'state': 'FAILURE'},
                        {'cluster': 'gcp', 'check': 'lint', 'state': 'PENDING'},
                    ],
                },
            ],
        },
    )
    monkeypatch.setenv('LEARTECH_MOCK_PIPELINE_SCENARIO', str(path))

    result = asyncio.run(
        _wait_first(
            {
                'repo': 'leartech-x',
                'pr_number': 1,
                'timeout_seconds': 60,
                'poll_seconds': 1,
            }
        )
    )
    payload = _payload(result)
    assert payload['status'] == 'first_failure'
    assert payload['first_failure']['cluster'] == 'az'
    assert payload['first_failure']['check'] == 'lint'


def test_wait_for_first_failure_all_pass_when_everything_terminal_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = _write_scenario(
        tmp_path,
        {
            'name': 'all-pass',
            'events': [
                {
                    'at_seconds': 0,
                    'checks': [
                        {'cluster': 'az', 'check': 'lint', 'state': 'SUCCESS'},
                        {'cluster': 'gcp', 'check': 'lint', 'state': 'SUCCESS'},
                    ],
                },
            ],
        },
    )
    monkeypatch.setenv('LEARTECH_MOCK_PIPELINE_SCENARIO', str(path))

    result = asyncio.run(
        _wait_first(
            {
                'repo': 'leartech-x',
                'pr_number': 1,
                'timeout_seconds': 60,
                'poll_seconds': 1,
            }
        )
    )
    payload = _payload(result)
    assert payload['status'] == 'all_passed'
    assert payload['first_failure'] is None


def test_wait_for_terminal_blocks_through_pending_then_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A scenario that starts PENDING and transitions to SUCCESS within the wait window."""
    path = _write_scenario(
        tmp_path,
        {
            'name': 'pending-then-pass',
            'events': [
                {'at_seconds': 0, 'checks': [{'cluster': 'az', 'check': 'lint', 'state': 'PENDING'}]},
                {'at_seconds': 2, 'checks': [{'cluster': 'az', 'check': 'lint', 'state': 'SUCCESS'}]},
            ],
        },
    )
    monkeypatch.setenv('LEARTECH_MOCK_PIPELINE_SCENARIO', str(path))

    result = asyncio.run(
        _wait_terminal(
            {
                'repo': 'leartech-x',
                'pr_number': 1,
                'timeout_seconds': 10,
            }
        )
    )
    payload = _payload(result)
    assert payload['status'] == 'all_passed'


def test_module_raises_clear_error_if_env_var_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_MOCK_PIPELINE_SCENARIO', raising=False)
    with pytest.raises(RuntimeError, match='LEARTECH_MOCK_PIPELINE_SCENARIO'):
        asyncio.run(_list_handler({'repo': 'leartech-x', 'pr_number': 1}))
