"""Lessons catalog — agent calibration database.

Each lesson is a frontmatter+markdown file in `catalog/`. The renderer filters by
agent + status and injects relevant lessons into system prompts at runtime, so newly
captured lessons take effect on the next agent session without code edits.

Schema admits feedback from all 5 sources (agent runs, CI, staging, manual review,
production), even though v1 only auto-captures agent_run. The schema is forward-
compatible — adding a source = adding a string to the enum.
"""

from gate.agent.lessons.loader import (
    Lesson,
    LessonSource,
    load_all_lessons,
    parse_lesson_file,
)
from gate.agent.lessons.prompt_renderer import render_for

__all__ = [
    'Lesson',
    'LessonSource',
    'load_all_lessons',
    'parse_lesson_file',
    'render_for',
]
