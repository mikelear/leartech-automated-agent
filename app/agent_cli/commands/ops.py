"""``leartech-agent ops`` — operator commands for a running initiative.

Pairs with the bidirectional command queue (initiative
``agent-add-command-queue-with-injection``). Today, once an agent
starts, an operator can only watch + cancel via K8s Job deletion (which
is abrupt — no reason recorded, no graceful shutdown). The ``ops``
subcommands queue structured commands that the agent's SDK loop polls
for between turns, giving the operator real bidirectional control:

    leartech-agent ops inject <id> "use docker.io not ghcr.io"
    leartech-agent ops cancel <id> --reason "wrong branch"
    leartech-agent ops pause  <id>
    leartech-agent ops resume <id>
    leartech-agent ops list   <id>            # show queued commands

All four mutating subcommands POST to ``/initiatives/{id}/commands``;
``list`` is a GET on the same path. The CLI is intentionally thin —
the service is the source of truth, the agent's SDK loop is the
applier.
"""

from __future__ import annotations

import click
from rich import box
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.group()
def ops() -> None:
    """Send commands to a running initiative (cancel / pause / inject)."""


def _post_command(
    ctx: click.Context,
    run_id: str,
    command_type: str,
    payload: dict[str, object] | None = None,
) -> None:
    """Shared POST + render for every mutating ``ops`` subcommand.

    Keeps the per-subcommand modules trivial (one click decoration +
    one call). Renders the queued command_id on success so the operator
    can quote it in a follow-up ``ops list`` to confirm ack.
    """
    body: dict[str, object] = {'command_type': command_type}
    if payload is not None:
        body['payload'] = payload
    response = client_from_ctx(ctx.obj).post(
        f'/initiatives/{run_id}/commands',
        json=body,
    )
    if response.status_code not in (200, 201):
        print_http_error(response)
        ctx.exit(1)
    parsed = response.json()
    console.print(
        f'[green]queued[/green] {command_type} (id={parsed["command_id"]}) for run {run_id} at {parsed["submitted_at"]}'
    )


@ops.command('inject')
@click.argument('run_id')
@click.argument('text')
@click.pass_context
def ops_inject(ctx: click.Context, run_id: str, text: str) -> None:
    """Append ``text`` to the agent's conversation as a UserMessage.

    The injected text is observable in the snapshot table (Layer 3
    diagnostics) immediately. In v1 the model does NOT consume injected
    text in the same SDK session — v1.5 (migration to
    ``ClaudeSDKClient``) will pump injections into the model's input
    stream in real time. Either way, the injection is durable and
    surfaced to post-run forensics.
    """
    _post_command(ctx, run_id, 'inject_guidance', payload={'text': text})


@ops.command('cancel')
@click.argument('run_id')
@click.option(
    '--reason',
    default='',
    help='Optional cancellation reason (recorded in initiative_runs.error).',
)
@click.pass_context
def ops_cancel(ctx: click.Context, run_id: str, reason: str) -> None:
    """Request graceful shutdown of the agent driving ``run_id``.

    Distinct from ``runs cancel``: that command deletes the K8s Job
    immediately (abrupt; no reason recorded). ``ops cancel`` queues a
    cancel command that the agent picks up at the next turn boundary,
    writes the reason into ``initiative_runs.error``, persists a final
    snapshot, then exits. Use ``ops cancel`` for any operator-driven
    shutdown where attribution matters; reserve ``runs cancel`` for
    "this run is stuck and I need it gone now".
    """
    payload: dict[str, object] | None = {'reason': reason} if reason else None
    _post_command(ctx, run_id, 'cancel', payload=payload)


@ops.command('pause')
@click.argument('run_id')
@click.pass_context
def ops_pause(ctx: click.Context, run_id: str) -> None:
    """Pause the agent at the next turn boundary.

    The agent loop sleeps in short intervals while paused, re-draining
    the command queue each cycle so a follow-up ``resume`` or ``cancel``
    is picked up within ~2s. Long pauses count against the K8s Job's
    ``activeDeadlineSeconds`` — don't park a run for hours; cancel it.
    """
    _post_command(ctx, run_id, 'pause')


@ops.command('resume')
@click.argument('run_id')
@click.pass_context
def ops_resume(ctx: click.Context, run_id: str) -> None:
    """Resume a paused agent."""
    _post_command(ctx, run_id, 'resume')


@ops.command('list')
@click.argument('run_id')
@click.option(
    '--unacked-only',
    is_flag=True,
    default=False,
    help='Show only commands the agent has NOT yet processed.',
)
@click.pass_context
def ops_list(ctx: click.Context, run_id: str, unacked_only: bool) -> None:
    """List commands queued for ``run_id`` (acked + unacked by default).

    Useful for "is my cancel still pending?" checks — the ack_message
    column shows the agent's outcome (``ok: ...`` or ``err: ...``) so
    the operator can confirm delivery without re-checking the snapshot.
    """
    params: dict[str, str] = {}
    if unacked_only:
        params['unacked_only'] = 'true'
    response = client_from_ctx(ctx.obj).get(
        f'/initiatives/{run_id}/commands',
        params=params,
    )
    if response.status_code != 200:
        print_http_error(response)
        ctx.exit(1)
    items = response.json()
    if not items:
        scope = 'unacked' if unacked_only else 'queued'
        console.print(f'No {scope} commands for run {run_id}.')
        return
    table = Table(title=f'Commands for {run_id}', box=box.SIMPLE_HEAD)
    table.add_column('ID', style='bold')
    table.add_column('Type')
    table.add_column('Submitted')
    table.add_column('Acked')
    table.add_column('Ack message', overflow='fold')
    for item in items:
        table.add_row(
            str(item['id']),
            item['command_type'],
            item['submitted_at'],
            item.get('acked_at') or '-',
            item.get('ack_message') or '-',
        )
    console.print(table)


__all__ = ['ops']
