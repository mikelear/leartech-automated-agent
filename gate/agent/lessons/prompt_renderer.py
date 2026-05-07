"""Render the lessons catalog into a markdown block prepended to system prompts.

The renderer filters by `applies_to` and `status` so each agent only sees lessons
relevant to it. New lessons take effect at the next session start — no code edits.

Default policy: only `status == 'encoded'` lessons render. `open` lessons are pending
human review; `rejected` / `superseded` are excluded.
"""

from __future__ import annotations

from gate.agent.lessons.loader import Lesson, load_all_lessons


def filter_for(agent_name: str, *, lessons: list[Lesson] | None = None) -> list[Lesson]:
    """Return calibration lessons applicable to `agent_name` and currently encoded."""
    pool = lessons if lessons is not None else load_all_lessons()
    return [
        lesson
        for lesson in pool
        if lesson.category == 'calibration' and lesson.status == 'encoded' and agent_name in lesson.applies_to
    ]


def render_for(agent_name: str, *, lessons: list[Lesson] | None = None) -> str:
    """Render the calibration block for `agent_name`. Returns empty string if no relevant lessons."""
    relevant = filter_for(agent_name, lessons=lessons)
    if not relevant:
        return ''
    blocks: list[str] = [
        '## Calibrations from past runs',
        '',
        '_The following lessons were learned from real agent runs and have been canonicalised. '
        'They take precedence when in conflict with general guidance below._',
        '',
    ]
    for lesson in relevant:
        blocks.append(f'### {lesson.title}')
        blocks.append('')
        blocks.append(lesson.body)
        blocks.append('')
    return '\n'.join(blocks).strip()
