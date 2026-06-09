"""``leartech-agent lessons list|describe`` — lessons-catalog inspection."""

from __future__ import annotations

import click
from rich import box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from app.agent_cli.render import client_from_ctx, console, print_http_error


@click.group()
def lessons() -> None:
    """List + describe lessons in the catalog."""


@lessons.command('list')
@click.option('--category', help='Filter to one category (calibration / criteria_gap / tool_bug / architecture)')
@click.option('--status', help='Filter to one status (open / encoded / rejected / superseded)')
@click.pass_context
def lessons_list(ctx: click.Context, category: str | None, status: str | None) -> None:
    response = client_from_ctx(ctx.obj).get('/lessons')
    if response.status_code != 200:
        print_http_error(response)
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
    response = client_from_ctx(ctx.obj).get(f'/lessons/{lesson_id}')
    if response.status_code != 200:
        print_http_error(response)
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
