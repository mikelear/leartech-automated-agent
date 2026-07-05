"""Smoke tests for the leartech-agent CLI.

These run against the FastAPI app via TestClient (not real HTTP) by
monkey-patching the CLI's httpx.Client construction. They verify the
CLI's HTTP wiring + output handling for the operationally important
commands (fire, runs cancel, runs status).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

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
        patch('gate.agent.agentrun_client.ensure_agent_type', new_callable=AsyncMock),
        patch('gate.agent.agentrun_client.create_agent_run', side_effect=fake_spawn),
    ):
        # `webcoder-ui-add-about-page` was retired in PR #103; switch to a
        # still-shipped catalog entry so the assert pins the success path
        # rather than the unknown-initiative branch.
        result = runner.invoke(cli, ['fire', 'automated-agent-add-changelog-stub'])
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


def test_mcps_list_shows_catalog() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['mcps', 'list'])
    assert result.exit_code == 0
    assert 'MCP catalog' in result.output
    # CliRunner uses an 80-col terminal which truncates the per-row name;
    # the summary footer is stable across widths.
    assert 'catalogued' in result.output
    assert 'ready' in result.output


def test_roles_list_shows_personas() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['roles', 'list'])
    assert result.exit_code == 0
    assert 'initiative_agent' in result.output


def test_topology_renders_mermaid_to_stdout() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['topology'])
    assert result.exit_code == 0
    assert 'Phase 1' in result.output


def test_probe_returns_status_for_sdk_mcp() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['probe', 'leartech-pipeline'])
    assert result.exit_code == 0
    assert 'leartech-pipeline' in result.output
    assert 'sdk_import' in result.output


def test_cluster_flag_picks_known_ingress_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The --cluster flag must resolve to a real URL via the config map.

    Resolution now goes through ``app.agent_cli.config`` (the same map
    ``config show`` renders); the legacy ``product-first.com`` /
    ``modern-burro.com`` URLs in transport.py were stale and produced
    DNS-resolution errors. Both short (``gcp``) and full
    (``gcp-staging``) forms must work.
    """
    from app.agent_cli.transport import resolve_base_url

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('LEARTECH_AGENT_URL', raising=False)
    gcp_url = resolve_base_url(url=None, cluster='gcp')
    az_url = resolve_base_url(url=None, cluster='az')
    assert 'jx-staging.jx.leartech.com' in gcp_url
    assert 'jx-staging.az.leartech.com' in az_url


def test_cluster_flag_unknown_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from app.agent_cli.transport import resolve_base_url

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    try:
        resolve_base_url(url=None, cluster='moonbase')
    except ValueError as exc:
        assert 'moonbase' in str(exc)
    else:
        raise AssertionError('expected ValueError for unknown cluster')


def test_url_flag_overrides_env_and_cluster(monkeypatch: pytest.MonkeyPatch) -> None:
    """--url wins over both env and --cluster."""
    from app.agent_cli.transport import resolve_base_url

    monkeypatch.setenv('LEARTECH_AGENT_URL', 'http://from-env:9999')
    resolved = resolve_base_url(url='http://from-flag:1', cluster='gcp')
    assert resolved == 'http://from-flag:1'


def test_cluster_flag_beats_leartech_agent_url_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """When ``--cluster`` is supplied, the un-suffixed ``LEARTECH_AGENT_URL`` env
    is ignored — operators reach for ``--cluster`` precisely to override the
    ambient env on a one-off invocation.
    """
    from app.agent_cli.transport import resolve_base_url

    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setenv('LEARTECH_AGENT_URL', 'http://ambient:9999')
    resolved = resolve_base_url(url=None, cluster='gcp-staging')
    assert 'ambient' not in resolved
    assert 'jx-staging.jx.leartech.com' in resolved


def test_topology_copy_fallback_when_pbcopy_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """On a Linux box pbcopy doesn't exist; we expect a friendly message, not a traceback."""
    import subprocess as _subprocess

    def fake_run(*_args: object, **_kwargs: object) -> _subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError('pbcopy not installed')

    monkeypatch.setattr('app.agent_cli.commands.topology.subprocess.run', fake_run)
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['topology', '--render', 'copy'])
    assert result.exit_code == 0
    assert 'pbcopy' in result.output


def test_topology_png_fallback_when_mmdc_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No mmdc on the test image — the command must surface a friendly install hint."""
    import subprocess as _subprocess

    def fake_run(*_args: object, **_kwargs: object) -> _subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError('mmdc not installed')

    monkeypatch.setattr('app.agent_cli.commands.topology.subprocess.run', fake_run)
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['topology', '--render', 'png'])
    assert result.exit_code == 0
    assert 'mmdc' in result.output


def test_runs_list_works_against_empty_state() -> None:
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['runs', 'list'])
    assert result.exit_code == 0
