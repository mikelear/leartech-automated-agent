"""leartech-agent — operator CLI for the deployed agent service.

A thin Click + rich wrapper over the introspection endpoints exposed by
``app.routers.introspection``. No business logic; the service is the
source of truth.

URL resolution (highest precedence first):

1. ``--url`` flag                           — explicit override
2. ``--cluster gcp|az`` flag                — known ingress
3. ``LEARTECH_AGENT_URL`` env var           — operator-set default
4. ``http://localhost:8080``                — laptop fallback

Common workflows:

    leartech-agent health
    leartech-agent mcps list
    leartech-agent lessons list --category criteria_gap
    leartech-agent runs status <id>
    leartech-agent runs timeline <id>
    leartech-agent runs follow <id>
    leartech-agent topology --render browser

The CLI is split per-feature under ``app/agent_cli/commands/`` so each
command's surface stays a manageable size. ``transport.py`` owns URL
resolution + httpx-client construction; ``render.py`` owns the shared
rich console + standard HTTP-error rendering. New commands are added by
creating a sibling module under ``commands/`` and wiring the click
``command`` / ``group`` into ``cli`` below.
"""

from __future__ import annotations

import click
import httpx

from app.agent_cli.commands.chat import chat as chat_cmd
from app.agent_cli.commands.config_cmd import config_group as config_cmd
from app.agent_cli.commands.fire import fire as fire_cmd
from app.agent_cli.commands.health import health as health_cmd
from app.agent_cli.commands.health import health_mcp as health_mcp_cmd
from app.agent_cli.commands.lessons import lessons as lessons_cmd
from app.agent_cli.commands.mcps import mcps as mcps_cmd
from app.agent_cli.commands.ops import ops as ops_cmd
from app.agent_cli.commands.roles import roles as roles_cmd
from app.agent_cli.commands.runs import runs as runs_cmd
from app.agent_cli.commands.topology import topology as topology_cmd
from app.agent_cli.transport import DEFAULT_URL, resolve_base_url


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.option(
    '--url',
    default=None,
    show_default='env LEARTECH_AGENT_URL or http://localhost:8080',
    help='Base URL of the deployed automated-agent service.',
)
@click.option(
    '--cluster',
    type=str,
    default=None,
    help=(
        'Pick a configured cluster (canonical name or unambiguous prefix, '
        "e.g. 'gcp-staging' or 'gcp'). Resolved against ~/.config/leartech-agent/"
        'config.yaml — overrides env LEARTECH_AGENT_URL, lower priority than --url.'
    ),
)
@click.pass_context
def cli(ctx: click.Context, url: str | None, cluster: str | None) -> None:
    """leartech-agent — operator view of the deployed automated-agent platform."""
    ctx.ensure_object(dict)
    resolved = resolve_base_url(url, cluster)
    # Tests monkey-patch ``httpx.Client`` (e.g. to redirect into a
    # FastAPI TestClient). Constructing through this module-level symbol
    # lets the patch take effect without reaching into every command.
    ctx.obj['client'] = httpx.Client(base_url=resolved, timeout=30.0)


# Top-level commands
cli.add_command(health_cmd)
cli.add_command(fire_cmd)
cli.add_command(topology_cmd)
cli.add_command(chat_cmd)

# Sub-groups
cli.add_command(mcps_cmd)
cli.add_command(roles_cmd)
cli.add_command(lessons_cmd)
cli.add_command(runs_cmd)
cli.add_command(ops_cmd)
cli.add_command(config_cmd)

# Nested: `leartech-agent health mcp <name>` lives under the health group's
# command list when invoked as a chain, but Click only allows nesting via
# a Group. We expose it as a sibling top-level command for MVP — the
# `mcps describe` command already covers the static view of one MCP, this
# adds the active-probe variant.
cli.add_command(health_mcp_cmd, name='probe')


# Re-export so existing ``app.agent_cli.main:cli`` console-script entry
# point keeps working and tests can keep patching
# ``app.agent_cli.main.httpx.Client``.
__all__ = ['DEFAULT_URL', 'cli']


if __name__ == '__main__':
    # ``leartech-agent`` may be invoked from a pod with no env or flags;
    # the resolve_base_url() default keeps it pointing at localhost.
    cli()
