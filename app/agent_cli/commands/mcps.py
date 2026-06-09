"""``leartech-agent mcps list|describe`` — MCP catalog inspection."""

from __future__ import annotations

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.group()
def mcps() -> None:
    """List + describe MCP servers in the catalog."""


@mcps.command('list')
@click.pass_context
def mcps_list(ctx: click.Context) -> None:
    """Catalogue with reachability + per-role usage."""
    response = client_from_ctx(ctx.obj).get('/mcps')
    if response.status_code != 200:
        print_http_error(response)
        return
    data = response.json()
    table = Table(title='MCP catalog', box=box.SIMPLE_HEAD)
    table.add_column('Name', style='bold')
    table.add_column('Type')
    table.add_column('Status')
    table.add_column('Roles')
    table.add_column('Description', overflow='fold')

    status_glyph = {
        'ready': '[green]✓ ready[/green]',
        'not_built': '[yellow]· not_built[/yellow]',
        'missing_auth': '[yellow]⚠ no auth[/yellow]',
        'down': '[red]✗ down[/red]',
    }
    for mcp in data:
        table.add_row(
            mcp['name'],
            mcp['type'],
            status_glyph.get(mcp['status'], mcp['status']),
            ', '.join(mcp['roles']) or '-',
            mcp['description'].split('\n')[0][:80],
        )
    console.print(table)
    summary = (
        f'{len(data)} catalogued — '
        f'{sum(1 for m in data if m["status"] == "ready")} ready · '
        f'{sum(1 for m in data if m["status"] == "not_built")} not_built · '
        f'{sum(1 for m in data if m["status"] == "missing_auth")} missing auth · '
        f'{sum(1 for m in data if m["status"] == "down")} down'
    )
    console.print(f'\n{summary}')


@mcps.command('describe')
@click.argument('name')
@click.pass_context
def mcps_describe(ctx: click.Context, name: str) -> None:
    """Full detail on one MCP."""
    response = client_from_ctx(ctx.obj).get(f'/mcps/{name}')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    spec = body['spec']
    lines = [
        f'[bold]Name:[/bold] {body["name"]}',
        f'[bold]Type:[/bold] {spec["type"]}',
        f'[bold]Status:[/bold] {body["status"]}',
        f'[bold]Description:[/bold] {spec["description"]}',
        f'[bold]Roles:[/bold] {", ".join(body["roles"]) or "-"}',
    ]
    if spec['type'] == 'sdk':
        lines.append(f'[bold]Builder:[/bold] {spec.get("builder")}')
    elif spec['type'] == 'stdio':
        lines.append(f'[bold]Command:[/bold] {spec.get("command")} {" ".join(spec.get("args", []))}')
        if spec.get('env'):
            lines.append(f'[bold]Env:[/bold] {", ".join(spec["env"])}')
    elif spec['type'] in ('http_sse', 'remote'):
        lines.append(f'[bold]URL:[/bold] {spec.get("url")}')
        if spec.get('auth'):
            lines.append(f'[bold]Auth:[/bold] {spec["auth"]["type"]} via ${spec["auth"]["token_env"]}')
    console.print(Panel('\n'.join(lines), title=f'MCP — {body["name"]}', border_style='cyan'))
