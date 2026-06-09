"""``leartech-agent fire <initiative>`` — start a baked-in initiative."""

from __future__ import annotations

import click
from rich.panel import Panel

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.command()
@click.argument('initiative_name')
@click.pass_context
def fire(ctx: click.Context, initiative_name: str) -> None:
    """Fire a baked-in initiative on the deployed service. Returns the new run ID.

    The initiative YAML must already exist in the deployed image (run
    `leartech-agent runs list` or look at the service's bundled
    initiatives/ directory). Adding a new YAML requires a release + auto-promote.
    """
    response = client_from_ctx(ctx.obj).post('/initiatives', json={'initiative': initiative_name})
    if response.status_code != 202:
        if response.status_code == 404:
            try:
                detail = response.json()['detail']
                console.print(f'[red]Unknown initiative {initiative_name!r}.[/red]')
                available = detail.get('available', []) if isinstance(detail, dict) else []
                if available:
                    console.print('\nAvailable initiatives on this service:')
                    for name in available:
                        console.print(f'  • {name}')
                return
            except (KeyError, ValueError):
                pass
        print_http_error(response)
        return
    body = response.json()
    console.print(
        Panel(
            f'[bold green]Fired[/bold green] {body["initiative"]}\n\n'
            f'[bold]Run ID:[/bold] {body["id"]}\n'
            f'[bold]Status:[/bold] {body["status"]} (queued, agent will spawn shortly)\n'
            f'[bold]Started:[/bold] {body["started_at"]}\n\n'
            f'Track progress:\n'
            f'  leartech-agent runs status {body["id"]}\n'
            f'  leartech-agent runs cancel {body["id"]}  # if needed',
            title='Initiative queued',
            border_style='green',
        )
    )
