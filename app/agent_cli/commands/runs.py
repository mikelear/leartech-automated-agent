"""``leartech-agent runs list|status|timeline|why|cancel|follow`` — initiative-run inspection."""

from __future__ import annotations

import datetime as _dt
import re
import time

import click
from rich import box
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error

# Polling cap for `runs follow` — at the default 5s interval this is 50 minutes,
# which exceeds every initiative's K8s `activeDeadlineSeconds`. Operators can
# tune the cap with --max-polls when watching multi-hour orchestrator runs.
_DEFAULT_MAX_POLLS = 600
_DEFAULT_FOLLOW_INTERVAL_SECONDS = 5.0
_TERMINAL_RUN_STATES = frozenset({'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'})

# Default `--since` window for ``runs list``. 24h is wide enough to catch
# yesterday-evening runs an operator might triage in the morning but narrow
# enough that the table doesn't blow past a terminal scroll buffer for
# busy services. ``--all`` bypasses the filter entirely.
_DEFAULT_RUNS_SINCE = '24h'

# Recognised `--since` shorthand. We deliberately keep this short — anything
# more complex (ISO datetime ranges, "yesterday", etc.) is a future need that
# would warrant a real date-parsing dependency.
_RELATIVE_SINCE_RE = re.compile(r'^(\d+)([smhd])$')
_RELATIVE_SECONDS: dict[str, int] = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}
# Loose ISO-8601 detector: a date or a date+time. ``datetime.fromisoformat``
# does the actual parse; this just gives a friendlier error message before
# we hit a ValueError.
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$')


def _parse_since(value: str) -> _dt.datetime:
    """Parse ``--since`` (e.g. ``24h``, ``7d``, ``2026-06-01``) → UTC datetime.

    Returned datetime is timezone-aware (UTC) so callers can compare
    against ``started_at`` ISO strings without surprise. Raises
    ``click.BadParameter`` on malformed input to give Click's standard
    "Usage: ... " hint rather than a stack trace.
    """
    now = _dt.datetime.now(_dt.UTC)
    if match := _RELATIVE_SINCE_RE.match(value):
        amount = int(match.group(1))
        unit = match.group(2)
        return now - _dt.timedelta(seconds=amount * _RELATIVE_SECONDS[unit])
    if _ISO_DATE_RE.match(value):
        try:
            parsed = _dt.datetime.fromisoformat(value)
        except ValueError as exc:
            raise click.BadParameter(f'--since {value!r}: {exc}') from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.UTC)
        return parsed
    raise click.BadParameter(f'--since {value!r}: expected forms like "24h", "7d", "2026-06-01".')


@click.group()
def runs() -> None:
    """List + inspect initiative runs."""


@runs.command('list')
@click.option(
    '--since',
    default=_DEFAULT_RUNS_SINCE,
    show_default=True,
    help='Only show runs started within this window (e.g. 24h, 7d, 2026-06-01). Ignored if --all is set.',
)
@click.option(
    '--all',
    'show_all',
    is_flag=True,
    default=False,
    help='Show every recorded run, ignoring --since.',
)
@click.pass_context
def runs_list(ctx: click.Context, since: str, show_all: bool) -> None:
    """List initiative runs known to the deployed service.

    By default only recent runs (started within ``--since``, default 24h)
    are shown — operators triaging "what ran today?" should not have to
    page through last week's archive. ``--all`` reverts to the full
    history.
    """
    response = client_from_ctx(ctx.obj).get('/initiatives')
    if response.status_code != 200:
        print_http_error(response)
        return
    items = response.json()
    if not items:
        console.print('No initiative runs yet on this service.')
        return
    filtered = items
    cutoff: _dt.datetime | None = None
    if not show_all:
        cutoff = _parse_since(since)
        filtered = [r for r in items if _record_started_after(r, cutoff)]
    if not filtered:
        scope = 'in the last ' + since if not show_all else 'recorded'
        console.print(
            f'No initiative runs {scope}. Re-run with `--all` to see the full history ({len(items)} records).'
        )
        return
    suffix = '' if show_all else f' since {since}'
    title = f'Initiative runs ({len(filtered)}/{len(items)}{suffix})'
    table = Table(title=title, box=box.SIMPLE_HEAD)
    table.add_column('ID', style='bold')
    table.add_column('Initiative')
    table.add_column('Status')
    table.add_column('Turns')
    table.add_column('Cost ($)')
    table.add_column('Started')
    for r in filtered:
        table.add_row(
            r['id'],
            r['initiative'],
            r['status'],
            str(r.get('turns') or '-'),
            f'{r["cost_usd"]:.4f}' if r.get('cost_usd') is not None else '-',
            r.get('started_at') or '-',
        )
    console.print(table)


def _record_started_after(record: dict[str, object], cutoff: _dt.datetime) -> bool:
    """True if the run's ``started_at`` is at or after ``cutoff``.

    Tolerant of records missing the field (we keep them — they're
    probably the freshly-queued ones) and of trailing-Z ISO strings the
    service emits.
    """
    raw = record.get('started_at')
    if not isinstance(raw, str):
        return True
    try:
        # Accept "...Z" by replacing with "+00:00" — fromisoformat won't
        # parse "Z" directly until 3.11+ even though we're on 3.13.
        parsed = _dt.datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed >= cutoff


@runs.command('status')
@click.argument('run_id')
@click.pass_context
def runs_status(ctx: click.Context, run_id: str) -> None:
    """Show one run's current status, PR number, turns, and cost."""
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
@click.option(
    '--interval',
    default=_DEFAULT_FOLLOW_INTERVAL_SECONDS,
    show_default=True,
    help='Seconds between status polls.',
)
@click.option(
    '--max-polls',
    default=_DEFAULT_MAX_POLLS,
    show_default=True,
    help='Stop after this many polls.',
)
@click.pass_context
def runs_follow(ctx: click.Context, run_id: str, interval: float, max_polls: int) -> None:
    """Poll a run's status until it reaches a terminal state.

    Real SSE streaming is a follow-up; this command mirrors the operator
    pattern in ``leartech-orchestrator/scripts/tail_plan_log.sh`` — short
    interval, print every status delta, stop on terminal.
    """
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
        if status in _TERMINAL_RUN_STATES:
            return
        time.sleep(interval)
    console.print(f'[yellow]follow: reached max-polls ({max_polls}); run still in {last_status}[/yellow]')
