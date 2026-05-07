"""CLI for managing the lessons catalog.

Subcommands:
- `lessons list`    — show the current catalog (filtered by agent / category if requested)
- `lessons capture` — file a new lesson stub for human refinement
- `lessons render`  — print the rendered system-prompt block for a given agent
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import click

from gate.agent.lessons.loader import CATALOG_DIR, load_all_lessons
from gate.agent.lessons.prompt_renderer import render_for


def _slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9-]+', '-', text.lower()).strip('-')
    return slug[:80]


@click.group()
def main() -> None:
    """Manage the agent calibration lessons catalog."""


@main.command('list')
@click.option('--agent', default=None, help='Filter to lessons applicable to this agent (e.g. initiative_agent).')
@click.option(
    '--category',
    default=None,
    type=click.Choice(['calibration', 'criteria_gap', 'tool_bug', 'architecture']),
    help='Filter to a single category.',
)
@click.option(
    '--status',
    default=None,
    type=click.Choice(['open', 'encoded', 'rejected', 'superseded']),
    help='Filter to a single status.',
)
def list_cmd(agent: str | None, category: str | None, status: str | None) -> None:
    """List lessons in the catalog with optional filters."""
    lessons = load_all_lessons()
    if agent:
        lessons = [lesson for lesson in lessons if agent in lesson.applies_to]
    if category:
        lessons = [lesson for lesson in lessons if lesson.category == category]
    if status:
        lessons = [lesson for lesson in lessons if lesson.status == status]

    if not lessons:
        click.echo('(no lessons match)')
        return

    for lesson in lessons:
        click.echo(f'{lesson.id:50}  {lesson.category:15}  {lesson.status:11}  {",".join(lesson.applies_to) or "-"}')


@main.command('render')
@click.argument('agent_name')
def render_cmd(agent_name: str) -> None:
    """Print the rendered calibration block for `agent_name`. Empty if no relevant lessons."""
    block = render_for(agent_name)
    if not block:
        click.echo(f'(no encoded calibration lessons apply to {agent_name})', err=True)
        return
    click.echo(block)


@main.command('capture')
@click.option('--title', required=True, help='Short imperative title for the lesson.')
@click.option(
    '--source-type',
    required=True,
    type=click.Choice(['agent_run', 'ci_failure', 'staging_test', 'manual_review', 'prod_incident']),
    help='Where the signal came from.',
)
@click.option('--source-reference', required=True, help='PR#, incident ID, run ID anchoring the lesson.')
@click.option('--source-observer', required=True, help='Who/what saw it (human name, model, monitoring system).')
@click.option(
    '--category', default='calibration', type=click.Choice(['calibration', 'criteria_gap', 'tool_bug', 'architecture'])
)
@click.option('--applies-to', multiple=True, help='Repeat per agent/criterion this lesson affects.')
@click.option('--slipped-past', multiple=True, help='Criterion names that should have caught this (criteria_gap).')
@click.option('--proposed-criterion', default=None, help='Sketch of a new criterion to add (criteria_gap).')
@click.option('--body', default=None, help='Lesson body (markdown). If omitted, opens $EDITOR.')
def capture(
    title: str,
    source_type: str,
    source_reference: str,
    source_observer: str,
    category: str,
    applies_to: tuple[str, ...],
    slipped_past: tuple[str, ...],
    proposed_criterion: str | None,
    body: str | None,
) -> None:
    """File a new lesson stub. Opens $EDITOR for the body if not provided.

    Lessons land with status=open — humans review + flip to encoded once they've
    decided what to do. Use `lessons list --status open` to find them later.
    """
    lesson_id = _slugify(title)
    path = CATALOG_DIR / f'{lesson_id}.md'
    if path.exists():
        click.echo(f'Lesson already exists: {path} (run `lessons list` to see).', err=True)
        raise click.Abort

    if body is None:
        body = (
            click.edit(
                '\n# Describe the lesson here. Markdown is fine. Save + exit when done.\n\n'
                f'# Title: {title}\n'
                f'# Source: {source_type} ({source_reference})\n'
            )
            or ''
        )
        body = body.strip()

    if not body:
        click.echo('Lesson body is empty — aborting.', err=True)
        raise click.Abort

    captured = datetime.now(UTC).isoformat(timespec='seconds')
    lines = [
        '---',
        f'id: {lesson_id}',
        f'title: {title}',
        f'captured_at: {captured}',
        'source:',
        f'  type: {source_type}',
        f'  reference: {source_reference}',
        f'  observer: {source_observer}',
        f'category: {category}',
        'applies_to:',
        *(f'  - {target}' for target in applies_to),
        'status: open',
    ]
    if slipped_past:
        lines.extend(['slipped_past_criteria:', *(f'  - {name}' for name in slipped_past)])
    if proposed_criterion:
        lines.append('proposed_criterion: |')
        lines.extend(f'  {line}' for line in proposed_criterion.splitlines())
    lines.append('---')
    lines.append('')
    lines.append(body)
    lines.append('')

    path.write_text('\n'.join(lines))
    click.echo(f'Captured: {path}')
    click.echo('Status: open. Review then flip status to `encoded` to inject into agent prompts.')


if __name__ == '__main__':
    main()
