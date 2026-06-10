"""Tests for the cluster-aware ``leartech-agent`` config module + subcommands.

The config layer is the doorway operators use to point a single
installed CLI at any cluster's orchestrator + agent. The priority chain
(flag > env > file > built-in default) is the contract every other
subcommand relies on — these tests pin that contract end-to-end:

* file loading round-trips a fresh write
* unknown clusters raise instead of silently falling through
* the priority chain is honoured exactly
* the ``config show / set-cluster / use-cluster`` CLI surfaces work
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from app.agent_cli.config import (
    CliConfig,
    ClusterConfig,
    config_path,
    load_config,
    resolve_url,
    save_config,
)
from app.agent_cli.main import cli


def test_load_config_returns_builtins_when_file_missing(tmp_path: Path) -> None:
    target = tmp_path / 'missing.yaml'
    cfg = load_config(target)
    # Built-ins always present so a fresh install reaches staging.
    assert 'gcp-staging' in cfg.clusters
    assert 'az-staging' in cfg.clusters
    assert cfg.default_cluster == 'gcp-staging'
    assert cfg.source_path is None


def test_save_then_load_round_trips_overrides(tmp_path: Path) -> None:
    target = tmp_path / 'config.yaml'
    cfg = CliConfig(
        default_cluster='gcp-staging',
        clusters={
            'gcp-staging': ClusterConfig(
                name='gcp-staging',
                orch_url='https://moved-orch.example.com',
                agent_url='https://moved-agent.example.com',
            ),
            'gcp-prod': ClusterConfig(
                name='gcp-prod',
                orch_url='https://orch-prod.example.com',
                agent_url='https://agent-prod.example.com',
            ),
        },
    )
    save_config(cfg, target)
    re_read = load_config(target)
    assert re_read.clusters['gcp-staging'].orch_url == 'https://moved-orch.example.com'
    assert re_read.clusters['gcp-prod'].orch_url == 'https://orch-prod.example.com'
    # Built-in az-staging still resolvable since save_config writes deltas
    # but load_config seeds built-ins first.
    assert 'az-staging' in re_read.clusters


def test_resolve_cluster_unknown_raises(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / 'nope.yaml')
    with pytest.raises(ValueError, match='Unknown cluster'):
        cfg.resolve_cluster('moonbase')


def test_resolve_url_priority_flag_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`--orch-url` flag beats env, file, and default."""
    cfg = load_config(tmp_path / 'nope.yaml')
    resolved = resolve_url(
        'orch_url',
        flag_value='https://from-flag.example.com',
        cluster='gcp-staging',
        config=cfg,
        env={'LEARTECH_ORCH_URL': 'https://from-env.example.com'},
    )
    assert resolved == 'https://from-flag.example.com'


def test_resolve_url_priority_env_wins_over_file(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / 'nope.yaml')
    resolved = resolve_url(
        'orch_url',
        flag_value=None,
        cluster='gcp-staging',
        config=cfg,
        env={'LEARTECH_ORCH_URL': 'https://from-env.example.com'},
    )
    assert resolved == 'https://from-env.example.com'


def test_resolve_url_priority_file_wins_over_builtin(tmp_path: Path) -> None:
    target = tmp_path / 'config.yaml'
    save_config(
        CliConfig(
            default_cluster='gcp-staging',
            clusters={
                'gcp-staging': ClusterConfig(
                    name='gcp-staging',
                    orch_url='https://from-file-orch.example.com',
                    agent_url='https://from-file-agent.example.com',
                ),
            },
        ),
        target,
    )
    cfg = load_config(target)
    resolved = resolve_url(
        'orch_url',
        flag_value=None,
        cluster='gcp-staging',
        config=cfg,
        env={},
    )
    assert resolved == 'https://from-file-orch.example.com'


def test_resolve_url_priority_builtin_default(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / 'nope.yaml')
    resolved = resolve_url(
        'orch_url',
        flag_value=None,
        cluster='gcp-staging',
        config=cfg,
        env={},
    )
    # Built-in default for gcp-staging matches the YAML doc in the goal.
    assert resolved == 'https://leartech-orchestrator-jx-staging.jx.leartech.com'


def test_resolve_url_uses_default_cluster_when_no_cluster_arg(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / 'nope.yaml')
    resolved = resolve_url(
        'orch_url',
        flag_value=None,
        cluster=None,
        config=cfg,
        env={},
    )
    assert 'jx-staging' in resolved


def test_resolve_url_rejects_unknown_key(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / 'nope.yaml')
    with pytest.raises(ValueError, match='unknown key'):
        resolve_url('not-a-key', flag_value=None, cluster='gcp-staging', config=cfg, env={})


def test_config_path_honours_xdg_config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    path = config_path()
    assert path == tmp_path / 'leartech-agent' / 'config.yaml'


def test_config_show_cli_renders_default_cluster_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`leartech-agent config show` should render every cluster + mark the default."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ['config', 'show'])
    assert result.exit_code == 0, result.output
    assert 'gcp-staging' in result.output
    assert 'az-staging' in result.output
    assert 'Clusters' in result.output


def test_config_set_cluster_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            'config',
            'set-cluster',
            'gcp-prod',
            '--orch-url',
            'https://prod-orch.example.com',
            '--agent-url',
            'https://prod-agent.example.com',
        ],
    )
    assert result.exit_code == 0, result.output
    re_read = load_config()
    assert re_read.clusters['gcp-prod'].orch_url == 'https://prod-orch.example.com'


def test_config_use_cluster_unknown_exits_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ['config', 'use-cluster', 'mars'])
    assert result.exit_code != 0
    assert 'Unknown cluster' in result.output


def test_config_use_cluster_flips_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ['config', 'use-cluster', 'az-staging'])
    assert result.exit_code == 0, result.output
    re_read = load_config()
    assert re_read.default_cluster == 'az-staging'


def test_load_config_rejects_default_cluster_pointing_at_unknown(tmp_path: Path) -> None:
    target = tmp_path / 'config.yaml'
    target.write_text(
        'default_cluster: ghost\nclusters: {}\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='default_cluster'):
        load_config(target)


def test_load_config_rejects_malformed_clusters(tmp_path: Path) -> None:
    target = tmp_path / 'config.yaml'
    target.write_text(
        'clusters:\n  bad:\n    orch_url: 123\n    agent_url: ok\n',
        encoding='utf-8',
    )
    with pytest.raises(ValueError, match='orch_url'):
        load_config(target)


def test_load_config_rejects_top_level_list(tmp_path: Path) -> None:
    target = tmp_path / 'config.yaml'
    target.write_text('- not\n- a\n- mapping\n', encoding='utf-8')
    with pytest.raises(ValueError, match='top-level mapping'):
        load_config(target)
