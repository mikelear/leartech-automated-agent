"""leartech-pipeline-mcp-mock — Tekton pipeline mock for local agent integration testing.

Drop-in replacement for `pipeline_server.py`. Same `@tool` decorators with
**identical names and signatures** so the agent's loop runs the same code path
end-to-end — only the source of check-state responses differs. The agent's
prompt, tool dispatch, MCP wrapping, and decision logic are unchanged.

## How the swap works

`gate/agent/initiative.py` calls `build_pipeline_server()` once when constructing
the Claude SDK options. That function (or its `__init__.py` re-export) gates on
the `LEARTECH_MOCK_PIPELINE_SCENARIO` env var:

- env var set → `build_mock_pipeline_server(scenario_path=...)` (this module)
- env var unset → `build_pipeline_server()` (real module)

Production never sets the env var. Local tests + dev runs do. Same agent code,
same prompt, same tool calls — only the responses lie.

## Scenario file format

YAML, ordered list of `events` — each event specifies the check states valid
**from `at_seconds` onwards** (relative to the first call). The mock looks up
the most-recent event whose `at_seconds` is `<= elapsed` on each call.

    name: lint-fail-then-pass
    description: |
      Lint fails fast; after enough elapsed time (modelling an agent fix +
      push + new pipeline run), all checks pass.
    events:
      - at_seconds: 0
        checks:
          - {cluster: az,  check: lint,    state: PENDING}
          - {cluster: az,  check: end2end, state: PENDING}
          - {cluster: gcp, check: lint,    state: PENDING}
          - {cluster: gcp, check: end2end, state: PENDING}
      - at_seconds: 20
        checks:
          - {cluster: az,  check: lint,    state: FAILURE}
          - {cluster: az,  check: end2end, state: IN_PROGRESS}
          - {cluster: gcp, check: lint,    state: SUCCESS}
          - {cluster: gcp, check: end2end, state: IN_PROGRESS}
      - at_seconds: 90
        checks:
          - {cluster: az,  check: lint,    state: SUCCESS}
          - {cluster: az,  check: end2end, state: SUCCESS}
          - {cluster: gcp, check: lint,    state: SUCCESS}
          - {cluster: gcp, check: end2end, state: SUCCESS}

States accepted: `PENDING`, `IN_PROGRESS`, `SUCCESS`, `FAILURE`, `ERROR` —
same vocabulary as the real `PipelineCheck.state`.

## Scope today

This is the Phase-0 mock per the conductor-architecture plan: scripted
responses driven by elapsed time. The mock CAN'T know when the agent has
"committed a fix" — it just times-out events based on wall clock. That's
sufficient to test the loop primitives (`wait_for_first_failure_or_all_pass`,
`wait_for_terminal`) and the agent's classify-then-iterate behaviour.

A richer Phase-1 mock would expose webhooks the agent's mocked Bash tool
can call to advance the scenario state machine. Not yet — keep it simple.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

# ─── Scenario state ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MockCheck:
    cluster: str
    check: str
    state: str

    @property
    def terminal(self) -> bool:
        return self.state in ('SUCCESS', 'FAILURE', 'ERROR')

    @property
    def passed(self) -> bool:
        return self.state == 'SUCCESS'

    @property
    def failed(self) -> bool:
        return self.state in ('FAILURE', 'ERROR')

    def as_payload(self) -> dict[str, Any]:
        return {
            'cluster': self.cluster,
            'check': self.check,
            'state': self.state,
            'pipelinerun': f'mock-{self.cluster}-{self.check}',
            'passed': self.passed,
            'failed': self.failed,
            'terminal': self.terminal,
        }


@dataclass(frozen=True)
class ScenarioEvent:
    at_seconds: float
    checks: tuple[MockCheck, ...]


class Scenario:
    """Holds a loaded scenario file plus the wall-clock anchor for elapsed time.

    First call to `current_checks()` sets `started_at`. Subsequent calls return
    the most-recent event whose `at_seconds` is `<= now - started_at`.
    """

    def __init__(self, name: str, description: str, events: list[ScenarioEvent]) -> None:
        if not events:
            raise ValueError(f'Scenario {name!r} has no events')
        # Sort defensively in case the YAML wasn't ordered
        self._events = sorted(events, key=lambda e: e.at_seconds)
        self.name = name
        self.description = description
        self._started_at: float | None = None

    @classmethod
    def from_yaml(cls, path: Path) -> Scenario:
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f'Scenario file {path} must contain a YAML mapping')
        name = str(data.get('name', path.stem))
        description = str(data.get('description', ''))
        raw_events = data.get('events') or []
        events: list[ScenarioEvent] = []
        for i, raw in enumerate(raw_events):
            if not isinstance(raw, dict):
                raise ValueError(f'Scenario {name!r} event {i} is not a mapping')
            at_seconds = float(raw.get('at_seconds', 0))
            checks_raw = raw.get('checks') or []
            checks = tuple(
                MockCheck(
                    cluster=str(c['cluster']),
                    check=str(c['check']),
                    state=str(c['state']),
                )
                for c in checks_raw
            )
            events.append(ScenarioEvent(at_seconds=at_seconds, checks=checks))
        return cls(name=name, description=description, events=events)

    def current_checks(self) -> tuple[MockCheck, ...]:
        """Return the checks valid right now based on elapsed wall-clock time."""
        if self._started_at is None:
            self._started_at = time.monotonic()
        elapsed = time.monotonic() - self._started_at
        # Find the latest event whose at_seconds <= elapsed
        applicable = [e for e in self._events if e.at_seconds <= elapsed]
        if not applicable:
            # Before the first event — return its checks (or empty)
            return self._events[0].checks
        return applicable[-1].checks


# ─── Module-level scenario singleton ──────────────────────────────────────


_scenario: Scenario | None = None


def _get_scenario() -> Scenario:
    """Load (lazily) and return the active scenario.

    Reads `LEARTECH_MOCK_PIPELINE_SCENARIO` (path to a YAML file) the first
    time it's called. Subsequent calls reuse the loaded scenario.
    """
    global _scenario
    if _scenario is None:
        path_env = os.environ.get('LEARTECH_MOCK_PIPELINE_SCENARIO')
        if not path_env:
            raise RuntimeError(
                'pipeline_server_mock loaded but LEARTECH_MOCK_PIPELINE_SCENARIO '
                'is not set. Set it to the path of a scenario YAML file.'
            )
        _scenario = Scenario.from_yaml(Path(path_env))
    return _scenario


def reset_scenario_for_tests() -> None:
    """Test-only: drop the loaded scenario so the next call re-reads the env var.

    Lets a single pytest session swap scenarios between tests via monkeypatch.
    """
    global _scenario
    _scenario = None


# ─── MCP tool definitions — mirror pipeline_server.py exactly ─────────────


@tool(
    'list_pr_checks',
    'MOCK — same signature as the real list_pr_checks but driven by the active scenario.',
    {'repo': str, 'pr_number': int},
)
async def _list_pr_checks(args: dict[str, Any]) -> dict[str, Any]:
    checks = _get_scenario().current_checks()
    payload = [c.as_payload() for c in checks]
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'wait_for_terminal',
    'MOCK — blocks until the active scenario reports all checks terminal, or timeout.',
    {'repo': str, 'pr_number': int, 'timeout_seconds': int},
)
async def _wait_for_terminal(args: dict[str, Any]) -> dict[str, Any]:
    timeout = int(args.get('timeout_seconds') or 900)
    scenario = _get_scenario()
    deadline = time.monotonic() + timeout
    poll_seconds = 1.0  # mock can poll fast — no real API to hammer

    final_checks: tuple[MockCheck, ...] = ()
    status = 'timeout'
    while time.monotonic() < deadline:
        final_checks = scenario.current_checks()
        if final_checks and all(c.terminal for c in final_checks):
            if all(c.passed for c in final_checks):
                status = 'all_passed'
            else:
                status = 'some_failed'
            break
        await asyncio.sleep(poll_seconds)

    payload = {
        'status': status,
        'exit_code': 0 if status == 'all_passed' else (8 if status == 'some_failed' else 124),
        'checks': [c.as_payload() for c in final_checks],
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


@tool(
    'wait_for_first_failure_or_all_pass',
    'MOCK — same fail-fast semantics as the real tool, driven by the active scenario.',
    {'repo': str, 'pr_number': int, 'timeout_seconds': int, 'poll_seconds': int},
)
async def _wait_for_first_failure_or_all_pass(args: dict[str, Any]) -> dict[str, Any]:
    timeout = int(args.get('timeout_seconds') or 1800)
    # Mock can poll faster than prod — scenarios are seconds-scale, not minutes.
    poll_seconds = max(0.5, min(float(args.get('poll_seconds') or 1), 5.0))
    scenario = _get_scenario()
    deadline = time.monotonic() + timeout

    status = 'timeout'
    first_failure: dict[str, Any] | None = None
    final_checks: tuple[MockCheck, ...] = ()

    while time.monotonic() < deadline:
        final_checks = scenario.current_checks()

        failed = next((c for c in final_checks if c.failed), None)
        if failed is not None:
            status = 'first_failure'
            first_failure = {
                'cluster': failed.cluster,
                'check': failed.check,
                'state': failed.state,
                'pipelinerun': f'mock-{failed.cluster}-{failed.check}',
            }
            break

        if final_checks and all(c.terminal and c.passed for c in final_checks):
            status = 'all_passed'
            break

        await asyncio.sleep(poll_seconds)

    payload = {
        'status': status,
        'first_failure': first_failure,
        'checks': [c.as_payload() for c in final_checks],
    }
    return {'content': [{'type': 'text', 'text': json.dumps(payload, indent=2)}]}


def build_mock_pipeline_server() -> McpSdkServerConfig:
    """Construct the mock pipeline MCP server.

    Caller is responsible for ensuring `LEARTECH_MOCK_PIPELINE_SCENARIO` is set
    before any tool invocation. The scenario is loaded lazily on the first call
    so import-time errors don't surface in production paths.
    """
    return create_sdk_mcp_server(
        name='leartech-pipeline',  # SAME name as real server — agent sees no difference
        version='0.1.0-mock',
        tools=[_list_pr_checks, _wait_for_terminal, _wait_for_first_failure_or_all_pass],
    )
