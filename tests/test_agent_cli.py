"""Smoke tests for the leartech-agent CLI.

These run against the FastAPI app via TestClient (not real HTTP) by
monkey-patching the CLI's httpx.Client construction. They verify the
CLI's HTTP wiring + output handling for the operationally important
commands (fire, runs cancel, runs status).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from app.agent_cli.main import cli
from app.main import app

test_client = TestClient(app)


class _MockHttpxClient:
    """Stand-in for httpx.Client that proxies to FastAPI's TestClient."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._test_client = test_client

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self._test_client.get(url, **kwargs)  # type: ignore[arg-type]

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self._test_client.post(url, **kwargs)  # type: ignore[arg-type]


def test_fire_unknown_initiative_lists_available() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['fire', 'does-not-exist-xyz'])
    assert result.exit_code == 0  # CLI exits cleanly even on 404
    assert 'Unknown initiative' in result.output
    assert 'Available initiatives' in result.output


def test_fire_valid_initiative_returns_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """fire a real initiative; expect 202 + a run ID returned.

    Phase F: POST /initiatives spawns a K8s Job, so we need POD_NAMESPACE
    set and the spawn call mocked.
    """
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    async def fake_spawn(**kwargs: Any) -> tuple[str, str]:
        return kwargs['run_id'], kwargs['namespace']

    runner = CliRunner()
    with (
        patch('app.agent_cli.main.httpx.Client', _MockHttpxClient),
        patch('gate.agent.job_runner.spawn_initiative_job', side_effect=fake_spawn),
    ):
        result = runner.invoke(cli, ['fire', 'webcoder-ui-add-about-page'])
    assert result.exit_code == 0
    assert 'Fired' in result.output
    assert 'Run ID' in result.output


def test_runs_status_unknown_id_shows_404() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['runs', 'status', 'never-was-xyz'])
    assert result.exit_code == 0
    assert '404' in result.output


def test_health_renders_panel() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['health'])
    assert result.exit_code == 0
    assert 'Platform health' in result.output
    assert 'Lessons catalog' in result.output
