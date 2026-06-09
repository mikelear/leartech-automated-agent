"""``leartech-agent runs list|status|timeline|why|cancel|follow`` — initiative-run inspection."""

from __future__ import annotations

import time

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.group()
def runs() -> None:
    """List + inspect initiative runs."""


@runs.command('list')
@click.pass_context
def runs_list(ctx: click.Context) -> None:
    response = client_from_ctx(ctx.obj).get('/initiatives')
    if response.status_code != 200:
        print_http_error(response)
        return
    items = response.json()
    if not items:
        console.print('No initiative runs yet on this service.')
        return
    table = Table(title=f'Initiative runs ({len(items)})', box=box.SIMPLE_HEAD)
    table.add_column('ID', style='bold')
    table.add_column('Initiative')
    table.add_column('Status')
    table.add_column('Turns')
    table.add_column('Cost ($)')
    for r in items:
        table.add_row(
            r['id'],
            r['initiative'],
            r['status'],
            str(r.get('turns') or '-'),
            f'{r["cost_usd"]:.4f}' if r.get('cost_usd') is not None else '-',
        )
    console.print(table)


@runs.command('status')
@click.argument('run_id')
@click.pass_context
def runs_status(ctx: click.Context, run_id: str) -> None:
    response = client_from_ctx(ctx.obj).get(f'/initiatives/{run_id}')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    lines = [
        f'[bold]ID:[/bold] {body["id"]}',
        f'[bold]Initiative:[/bold] {body["initiative"]}',
        f'[bold]Status:[/bold] {body["status"]}',
        f'[bold]Started:[/bold] {body["started_at"]}',
    ]
    if body.get('finished_at'):
        lines.append(f'[bold]Finished:[/bold] {body["finished_at"]}')
    if body.get('pr_number'):
        lines.append(f'[bold]PR:[/bold] #{body["pr_number"]}')
    if body.get('turns'):
        lines.append(f'[bold]Turns:[/bold] {body["turns"]}')
    if body.get('cost_usd') is not None:
        lines.append(f'[bold]Cost:[/bold] ${body["cost_usd"]:.4f}')
    if body.get('error'):
        lines.append(f'[bold red]Error:[/bold red] {body["error"]}')
    console.print(Panel('\n'.join(lines), title=f'Run — {body["id"]}', border_style='cyan'))


@runs.command('cancel')
@click.argument('run_id')
@click.pass_context
def runs_cancel(ctx: click.Context, run_id: str) -> None:
    """Cancel a running initiative. Idempotent for already-terminal records."""
    response = client_from_ctx(ctx.obj).post(f'/initiatives/{run_id}/cancel')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    console.print(f'[yellow]Cancelled[/yellow] run {body["id"]} ({body["initiative"]}) → status={body["status"]}')


@runs.command('timeline')
@click.argument('run_id')
@click.pass_context
def runs_timeline(ctx: click.Context, run_id: str) -> None:
    """Turn-by-turn timeline derived from the run record."""
    response = client_from_ctx(ctx.obj).get(f'/initiatives/{run_id}/timeline')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    table = Table(title=f'Timeline — {body["initiative"]} ({body["run_id"]})', box=box.SIMPLE_HEAD)
    table.add_column('At', style='dim')
    table.add_column('Event', style='bold')
    table.add_column('Note', overflow='fold')
    for event in body['events']:
        table.add_row(event['at'], event['kind'], event['note'])
    console.print(table)


@runs.command('why')
@click.argument('run_id')
@click.pass_context
def runs_why(ctx: click.Context, run_id: str) -> None:
    """Show which lessons were matched + injected at session start."""
    response = client_from_ctx(ctx.obj).get(f'/initiatives/{run_id}/why')
    if response.status_code != 200:
        print_http_error(response)
        return
    body = response.json()
    table = Table(
        title=f'Calibration matched for {body["initiative"]} ({body["matched_count"]} lessons)',
        box=box.SIMPLE_HEAD,
    )
    table.add_column('Lesson ID', style='bold')
    for lesson_id in body['matched_lessons']:
        table.add_row(lesson_id)
    console.print(table)


@runs.command('follow')
@click.argument('run_id')
@click.option('--interval', default=5.0, show_default=True, help='Seconds between status polls.')
@click.option('--max-polls', default=600, show_default=True, help='Stop after this many polls.')
@click.pass_context
def runs_follow(ctx: click.Context, run_id: str, interval: float, max_polls: int) -> None:
    """Poll a run's status until it reaches a terminal state.

    Real SSE streaming is a follow-up; this command mirrors the operator
    pattern in ``leartech-orchestrator/scripts/tail_plan_log.sh`` — short
    interval, print every status delta, stop on terminal.
    """
    terminal = {'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'}
    last_status: str | None = None
    client = client_from_ctx(ctx.obj)
    for _ in range(max_polls):
        response = client.get(f'/initiatives/{run_id}')
        if response.status_code != 200:
            print_http_error(response)
            return
        body = response.json()
        status = body['status']
        if status != last_status:
            console.print(f'[bold]{body["initiative"]}[/bold] — status: [cyan]{status}[/cyan]')
            last_status = status
        if status in terminal:
            return
        time.sleep(interval)
    console.print(f'[yellow]follow: reached max-polls ({max_polls}); run still in {last_status}[/yellow]')
