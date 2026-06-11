"""``--cluster <short-name>`` prefix-matches a configured cluster name.

Pins the operator-ergonomic short form: ``--cluster gcp`` resolves to
``gcp-staging`` (the only configured cluster name starting with
``gcp``), matching the behaviour documented in the CLI's ``--cluster``
help text.

The prefix match is implemented in
``app.agent_cli.config.CliConfig.resolve_cluster`` and surfaces through
``app.agent_cli.transport.resolve_base_url``; we test both layers so a
future refactor that bypasses one can't silently break the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_cli.config import load_config
from app.agent_cli.transport import resolve_base_url


def test_cluster_short_name_gcp_prefix_matches_gcp_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--cluster gcp`` resolves to the gcp-staging agent URL."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('LEARTECH_AGENT_URL', raising=False)
    resolved = resolve_base_url(url=None, cluster='gcp')
    assert resolved == 'https://leartech-automated-agent-jx-staging.jx.leartech.com'


def test_cluster_short_name_az_prefix_matches_az_staging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--cluster az`` resolves to the az-staging agent URL."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.delenv('LEARTECH_AGENT_URL', raising=False)
    resolved = resolve_base_url(url=None, cluster='az')
    assert resolved == 'https://leartech-automated-agent-jx-staging.az.leartech.com'


def test_resolve_cluster_prefix_matches_at_config_layer(tmp_path: Path) -> None:
    """The prefix match happens in ``CliConfig.resolve_cluster`` itself."""
    cfg = load_config(tmp_path / 'missing.yaml')
    assert cfg.resolve_cluster('gcp').name == 'gcp-staging'
    assert cfg.resolve_cluster('az').name == 'az-staging'
