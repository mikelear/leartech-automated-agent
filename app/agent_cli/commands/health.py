"""``leartech-agent health`` — rich platform health panel."""

from __future__ import annotations

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Rich platform health — lessons, MCPs, feedback rings."""
    response = client_from_ctx(ctx.obj).get('/health/detail')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()

    panel_text = (
        f'[bold]{body["service"]}[/bold] v{body["version"]}\n\n'
        f'[bold]Lessons catalog:[/bold] {body["lessons_loaded"]} loaded\n'
        f'  by category: {", ".join(f"{c}={n}" for c, n in body["lessons_by_category"].items())}\n'
        f'  by status:   {", ".join(f"{s}={n}" for s, n in body["lessons_by_status"].items())}\n\n'
        f'[bold]MCPs:[/bold] {body["mcps_total"]} catalogued — '
        f'[green]{body["mcps_ready"]} ready[/green] · '
        f'[yellow]{body["mcps_not_built"]} not_built[/yellow] · '
        f'[yellow]{body["mcps_missing_auth"]} missing_auth[/yellow] · '
        f'[red]{body["mcps_down"]} down[/red]\n\n'
        f'[bold]Roles:[/bold] {", ".join(body["roles"])}'
    )
    console.print(Panel(panel_text, title='Platform health', border_style='cyan'))

    rings_table = Table(title='Feedback rings', box=box.SIMPLE, show_header=True)
    rings_table.add_column('Ring')
    rings_table.add_column('Status')
    rings_table.add_column('Note')
    status_colour = {'active': 'green', 'pending': 'yellow', 'not_wired': 'red'}
    for ring in body['feedback_rings']:
        colour = status_colour.get(ring['status'], 'white')
        rings_table.add_row(ring['name'], f'[{colour}]{ring["status"]}[/{colour}]', ring['note'])
    console.print(rings_table)


@click.command('mcp')
@click.argument('name')
@click.pass_context
def health_mcp(ctx: click.Context, name: str) -> None:
    """Active probe one MCP. Renders status + which probe ran."""
    response = client_from_ctx(ctx.obj).get(f'/mcps/{name}/health')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    status_glyph = {
        'ready': '[green]✓ ready[/green]',
        'not_built': '[yellow]· not_built[/yellow]',
        'missing_auth': '[yellow]⚠ missing_auth[/yellow]',
        'down': '[red]✗ down[/red]',
    }
    console.print(
        Panel(
            f'[bold]MCP:[/bold] {body["name"]}\n'
            f'[bold]Status:[/bold] {status_glyph.get(body["status"], body["status"])}\n'
            f'[bold]Probe:[/bold] {body["probe"]}',
            title='MCP health',
            border_style='cyan',
        )
    )
