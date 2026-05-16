"""leartech-agent — operator CLI for the deployed agent service.

A thin Click + rich wrapper over the introspection endpoints.
No business logic; the service is the source of truth.

Default URL: http://localhost:8080
Override:    LEARTECH_AGENT_URL env var or `--url` flag

Common workflows:
    leartech-agent health
    leartech-agent mcps list
    leartech-agent lessons list --category criteria_gap
    leartech-agent runs status <id>
    leartech-agent topology --render browser
"""

from __future__ import annotations

import base64
import os
import subprocess
import webbrowser

import click
import httpx
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

DEFAULT_URL = 'http://localhost:8080'

console = Console()


def _client(ctx: click.Context) -> httpx.Client:
    client = ctx.obj['client']
    assert isinstance(client, httpx.Client)  # noqa: S101 — narrows for mypy
    return client


def _print_error(response: httpx.Response) -> None:
    try:
        detail = response.json().get('detail', response.text)
    except Exception:  # noqa: BLE001 — fall back to raw text on any parse error
        detail = response.text
    console.print(f'[red]HTTP {response.status_code}:[/red] {detail}')


@click.group(context_settings={'help_option_names': ['-h', '--help']})
@click.option(
    '--url',
    default=lambda: os.environ.get('LEARTECH_AGENT_URL', DEFAULT_URL),
    show_default='env LEARTECH_AGENT_URL or http://localhost:8080',
    help='Base URL of the deployed automated-agent service.',
)
@click.pass_context
def cli(ctx: click.Context, url: str) -> None:
    """leartech-agent — operator view of the deployed automated-agent platform."""
    ctx.ensure_object(dict)
    ctx.obj['client'] = httpx.Client(base_url=url, timeout=30.0)


# ─── health ───────────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def health(ctx: click.Context) -> None:
    """Rich platform health — lessons, MCPs, feedback rings."""
    response = _client(ctx).get('/health/detail')
    if response.status_code != 200:
        _print_error(response)
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


# ─── mcps ─────────────────────────────────────────────────────────────────


@cli.group()
def mcps() -> None:
    """List + describe MCP servers in the catalog."""


@mcps.command('list')
@click.pass_context
def mcps_list(ctx: click.Context) -> None:
    """Catalogue with reachability + per-role usage."""
    response = _client(ctx).get('/mcps')
    if response.status_code != 200:
        _print_error(response)
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
    response = _client(ctx).get(f'/mcps/{name}')
    if response.status_code != 200:
        _print_error(response)
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


# ─── roles ────────────────────────────────────────────────────────────────


@cli.group()
def roles() -> None:
    """List + describe agent personas."""


@roles.command('list')
@click.pass_context
def roles_list(ctx: click.Context) -> None:
    response = _client(ctx).get('/roles')
    if response.status_code != 200:
        _print_error(response)
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
    response = _client(ctx).get(f'/roles/{name}')
    if response.status_code != 200:
        _print_error(response)
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


# ─── lessons ──────────────────────────────────────────────────────────────


@cli.group()
def lessons() -> None:
    """List + describe lessons in the catalog."""


@lessons.command('list')
@click.option('--category', help='Filter to one category (calibration / criteria_gap / tool_bug / architecture)')
@click.option('--status', help='Filter to one status (open / encoded / rejected / superseded)')
@click.pass_context
def lessons_list(ctx: click.Context, category: str | None, status: str | None) -> None:
    response = _client(ctx).get('/lessons')
    if response.status_code != 200:
        _print_error(response)
        return
    items = response.json()
    if category:
        items = [it for it in items if it['category'] == category]
    if status:
        items = [it for it in items if it['status'] == status]
    table = Table(title=f'Lessons ({len(items)} matching)', box=box.SIMPLE_HEAD)
    table.add_column('ID', style='bold', overflow='fold')
    table.add_column('Category')
    table.add_column('Status')
    table.add_column('Applies To')
    for lesson in items:
        table.add_row(
            lesson['id'],
            lesson['category'],
            lesson['status'],
            ', '.join(lesson['applies_to']) or '-',
        )
    console.print(table)


@lessons.command('describe')
@click.argument('lesson_id')
@click.pass_context
def lessons_describe(ctx: click.Context, lesson_id: str) -> None:
    response = _client(ctx).get(f'/lessons/{lesson_id}')
    if response.status_code != 200:
        _print_error(response)
        return
    body = response.json()
    console.print(
        Panel(
            f'[bold]{body["title"]}[/bold]\n\n'
            f'ID: {body["id"]}\nCategory: {body["category"]}  Status: {body["status"]}\n'
            f'Applies to: {", ".join(body["applies_to"]) or "-"}\n'
            f'Captured: {body["captured_at"]}\n'
            f'Source: {body["source"]["type"]} / {body["source"]["reference"]} / {body["source"]["observer"]}',
            title='Lesson',
            border_style='cyan',
        )
    )
    console.print(Markdown(body['body']))


# ─── runs ─────────────────────────────────────────────────────────────────


@cli.group()
def runs() -> None:
    """List + inspect initiative runs."""


@runs.command('list')
@click.pass_context
def runs_list(ctx: click.Context) -> None:
    response = _client(ctx).get('/initiatives')
    if response.status_code != 200:
        _print_error(response)
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
    response = _client(ctx).get(f'/initiatives/{run_id}')
    if response.status_code != 200:
        _print_error(response)
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
    response = _client(ctx).post(f'/initiatives/{run_id}/cancel')
    if response.status_code != 200:
        _print_error(response)
        return
    body = response.json()
    console.print(f'[yellow]Cancelled[/yellow] run {body["id"]} ({body["initiative"]}) → status={body["status"]}')


# ─── fire (top-level convenience) ─────────────────────────────────────────


@cli.command()
@click.argument('initiative_name')
@click.pass_context
def fire(ctx: click.Context, initiative_name: str) -> None:
    """Fire a baked-in initiative on the deployed service. Returns the new run ID.

    The initiative YAML must already exist in the deployed image (run
    `leartech-agent runs list` or look at the service's bundled
    initiatives/ directory). Adding a new YAML requires a release + auto-promote.
    """
    response = _client(ctx).post('/initiatives', json={'initiative': initiative_name})
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
        _print_error(response)
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


# ─── topology ─────────────────────────────────────────────────────────────


@cli.command()
@click.option(
    '--render',
    type=click.Choice(['mermaid', 'png', 'browser', 'copy']),
    default='mermaid',
    help='Output format. mermaid=stdout, png=requires mmdc, browser=opens mermaid.live, copy=clipboard.',
)
@click.option('--output', '-o', default='/tmp/topo.png', help='Output path for --render png')
@click.option('--feedback', is_flag=True, help='Render only the feedback rings, not the full topology')
@click.pass_context
def topology(ctx: click.Context, render: str, output: str, feedback: bool) -> None:
    """Mermaid diagram of the platform topology."""
    path = '/topology/feedback' if feedback else '/topology'
    response = _client(ctx).get(path)
    if response.status_code != 200:
        _print_error(response)
        return
    src: str = response.json()['mermaid']

    if render == 'mermaid':
        console.print(src)
    elif render == 'png':
        try:
            subprocess.run(['mmdc', '-i', '-', '-o', output], input=src.encode(), check=True)
            console.print(f'[green]Wrote {output}[/green]')
        except FileNotFoundError:
            console.print('[red]mmdc not installed.[/red] Try: npm install -g @mermaid-js/mermaid-cli')
        except subprocess.CalledProcessError as exc:
            console.print(f'[red]mmdc failed: {exc}[/red]')
    elif render == 'browser':
        # mermaid.live uses base64-pako, but plain base64 works for `pako:` URLs too via the editor.
        # Simpler: just use the edit URL with the source.
        encoded = base64.b64encode(src.encode()).decode()
        url = f'https://mermaid.live/edit#base64:{encoded}'
        console.print(f'Opening {url}')
        webbrowser.open(url)
    elif render == 'copy':
        try:
            subprocess.run(['pbcopy'], input=src.encode(), check=True)
            console.print('[green]Copied to clipboard.[/green]')
        except FileNotFoundError:
            console.print(
                '[red]pbcopy not available (this command is macOS-only). Pipe to your clipboard tool manually.[/red]'
            )


if __name__ == '__main__':
    cli()
