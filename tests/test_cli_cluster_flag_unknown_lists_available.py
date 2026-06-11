"""Unknown ``--cluster`` names produce an error that lists what's available.

The flag was previously a ``click.Choice(['gcp', 'az'])`` that listed
the (incorrect) values inline. Now any string is accepted, so the
error path is the only thing pointing operators at the configured set
— it must enumerate the configured cluster names so a typo (e.g.
``--cluster gcp-prood``) is debuggable from the terminal output alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from app.agent_cli.config import load_config
from app.agent_cli.main import cli
from app.agent_cli.transport import resolve_base_url


def test_unknown_cluster_error_lists_configured_names(tmp_path: Path) -> None:
    """The ``ValueError`` from the config layer lists what *is* available."""
    cfg = load_config(tmp_path / 'missing.yaml')
    with pytest.raises(ValueError) as exc:
        cfg.resolve_cluster('moonbase')
    msg = str(exc.value)
    assert 'moonbase' in msg
    assert 'gcp-staging' in msg
    assert 'az-staging' in msg


def test_resolve_base_url_unknown_cluster_propagates_listing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """transport.resolve_base_url surfaces the same listing-rich error."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    with pytest.raises(ValueError) as exc:
        resolve_base_url(url=None, cluster='ghost-cluster')
    msg = str(exc.value)
    assert 'ghost-cluster' in msg
    assert 'gcp-staging' in msg


def test_cli_unknown_cluster_renders_available_to_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End-to-end through the Click CLI: bad name → operator sees the list."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    # Any subcommand exercises the top-level --cluster resolution.
    # ``--cluster`` is now ``type=str`` so Click won't reject it inline;
    # the resolution happens inside the callback, surfacing the listing.
    result = runner.invoke(cli, ['--cluster', 'ghost-cluster', 'health'])
    # The Click invocation should fail with a non-zero exit code and the
    # available-clusters message in the output (covering exception+stderr).
    combined = (result.output or '') + (str(result.exception) if result.exception else '')
    assert 'ghost-cluster' in combined
    assert 'gcp-staging' in combined
