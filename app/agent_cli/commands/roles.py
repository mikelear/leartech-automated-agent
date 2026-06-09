"""``leartech-agent roles list|describe`` — agent persona inspection."""

from __future__ import annotations

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.group()
def roles() -> None:
    """List + describe agent personas."""


@roles.command('list')
@click.pass_context
def roles_list(ctx: click.Context) -> None:
    response = client_from_ctx(ctx.obj).get('/roles')
    if response.status_code != 200:
        print_http_error(response)
        return
    table = Table(title='Agent roles', box=box.SIMPLE_HEAD)
    table.add_column('Role', style='bold')
    table.add_column('MCPs')
    table.add_column('Tools')
    table.add_column('Description')
    for role in response.json():
        table.add_row(role['name'], str(role['mcp_count']), str(role['tool_count']), role['description'])
    console.print(table)


@roles.command('describe')
@click.argument('name')
@click.pass_context
def roles_describe(ctx: click.Context, name: str) -> None:
    response = client_from_ctx(ctx.obj).get(f'/roles/{name}')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    spec = body['spec']
    lines = [
        f'[bold]Role:[/bold] {body["name"]}',
        f'[bold]Description:[/bold] {spec["description"].strip()}',
        f'[bold]MCPs ({len(spec["mcps"])}):[/bold] {", ".join(spec["mcps"])}',
        f'[bold]Tools ({len(spec["tools"])}):[/bold] {", ".join(spec["tools"])}',
        f'[bold]Lessons applying:[/bold] {body["lesson_count"]}',
    ]
    console.print(Panel('\n'.join(lines), title=f'Role — {body["name"]}', border_style='cyan'))
