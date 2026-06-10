"""``leartech-agent config show|set-cluster|use-cluster`` — local config CRUD.

Operators install the CLI once (via ``pipx``) and need to point it at any
cluster's orchestrator + agent URLs without editing source. The
`config` subgroup is the official entry-point — it writes
``~/.config/leartech-agent/config.yaml`` directly so the same file is
reusable by adjacent tooling (e.g. the future MCP-for-Claude wrapper).

Commands:

* ``config show`` — render the merged view (built-ins + file overrides)
* ``config set-cluster <name> --orch-url ... --agent-url ...`` — write/rebind
* ``config use-cluster <name>`` — flip ``default_cluster:`` to ``name``

The file path is shown in every render so operators can ``cat`` it
themselves when they want to share / diff.
"""

from __future__ import annotations

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.config import (
    APP_NAME,
    CONFIG_FILENAME,
    CliConfig,
    ClusterConfig,
    config_path,
    load_config,
    save_config,
)
from app.agent_cli.render import console


@click.group('config')
def config_group() -> None:
    """Read/edit ``~/.config/leartech-agent/config.yaml``."""


@config_group.command('show')
def config_show() -> None:
    """Render the merged config (built-ins ⊕ on-disk overrides)."""
    cfg = load_config()
    source = str(cfg.source_path) if cfg.source_path else '(none — using built-in defaults)'
    console.print(
        Panel(
            f'[bold]Default cluster:[/bold] {cfg.default_cluster}\n'
            f'[bold]Source file:[/bold] {source}\n'
            f'[bold]Path on disk:[/bold] {config_path()}',
            title=f'{APP_NAME} config',
            border_style='cyan',
        )
    )

    table = Table(title='Clusters', box=box.SIMPLE_HEAD)
    table.add_column('Name', style='bold')
    table.add_column('Orchestrator URL')
    table.add_column('Agent URL')
    table.add_column('Default', justify='center')
    for name in sorted(cfg.clusters):
        cluster = cfg.clusters[name]
        marker = '[green]✓[/green]' if name == cfg.default_cluster else ''
        table.add_row(name, cluster.orch_url, cluster.agent_url, marker)
    console.print(table)


@config_group.command('set-cluster')
@click.argument('name')
@click.option('--orch-url', required=True, help='Orchestrator base URL (POST /chat lives here).')
@click.option('--agent-url', required=True, help='Automated-agent base URL.')
def config_set_cluster(name: str, orch_url: str, agent_url: str) -> None:
    """Add (or rebind) a cluster entry.

    Re-running with the same ``name`` overwrites the URLs in-place — that
    is the operator's intended workflow when an ingress hostname moves
    or a new cluster comes online.
    """
    cfg = load_config()
    cfg.clusters[name] = ClusterConfig(name=name, orch_url=orch_url, agent_url=agent_url)
    target = save_config(cfg)
    console.print(
        f'[green]✓[/green] Set cluster [bold]{name}[/bold] (orch={orch_url}, agent={agent_url}) → written to {target}'
    )


@config_group.command('use-cluster')
@click.argument('name')
def config_use_cluster(name: str) -> None:
    """Make ``name`` the default cluster for subsequent invocations.

    The cluster must already exist (either built-in or previously
    written via ``set-cluster``); otherwise we raise immediately rather
    than write a config that points at a non-existent cluster.
    """
    cfg = load_config()
    if name not in cfg.clusters:
        available = sorted(cfg.clusters)
        console.print(
            f'[red]Unknown cluster {name!r}.[/red] '
            f'Add it first with `leartech-agent config set-cluster {name} --orch-url ... --agent-url ...` '
            f'or pick from: {available}'
        )
        raise SystemExit(1)
    new_config = CliConfig(
        default_cluster=name,
        clusters=cfg.clusters,
        source_path=cfg.source_path,
    )
    target = save_config(new_config)
    console.print(
        f'[green]✓[/green] Default cluster now [bold]{name}[/bold] '
        f'(orch={cfg.clusters[name].orch_url}) → written to {target}'
    )


# Alias for shorter import surface; the module name avoids shadowing the
# ``config`` module/symbol inside the package.
__all__ = ['config_group']

# Convenience for callers that want a tip on the on-disk path.
DEFAULT_CONFIG_HINT = f'~/.config/{APP_NAME}/{CONFIG_FILENAME}'
