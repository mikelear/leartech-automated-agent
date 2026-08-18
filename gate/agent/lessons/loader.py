"""Lesson schema + filesystem loader.

Each lesson is a `.md` file in `gate/agent/lessons/catalog/` with YAML frontmatter
between `---` delimiters and a markdown body after. The frontmatter validates against
the `Lesson` model; the body is free-form markdown rendered into prompts verbatim.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CATALOG_DIR = Path(__file__).parent / 'catalog'


SourceType = Literal['agent_run', 'ci_failure', 'staging_test', 'manual_review', 'prod_incident']
CategoryType = Literal['calibration', 'criteria_gap', 'tool_bug', 'architecture']
StatusType = Literal['open', 'encoded', 'rejected', 'superseded']


class LessonSource(BaseModel):
    """Provenance of the lesson — answers `where did this signal come from`."""

    model_config = ConfigDict(extra='forbid')

    type: SourceType = Field(description='Feedback latency layer the signal came from.')
    reference: str = Field(min_length=1, description='PR#, incident ID, run ID — anchors the lesson to a real event.')
    observer: str = Field(min_length=1, description='Who/what saw it — human name, model ID, monitoring system.')
    latency_to_capture: str | None = Field(
        default=None, description='Human-readable latency (e.g. "minutes", "4h", "3d").'
    )


class Lesson(BaseModel):
    """A single calibration / criteria-gap / tool-bug / architecture finding."""

    model_config = ConfigDict(extra='forbid')

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    captured_at: datetime
    source: LessonSource
    category: CategoryType = Field(description='What kind of fix this implies.')
    applies_to: list[str] = Field(
        default_factory=list,
        description='Which agents or criteria should consume this — e.g. ["initiative_agent", "review_agent"].',
    )
    status: StatusType = Field(default='open')
    encoded_in: list[str] = Field(
        default_factory=list,
        description='Files / criteria that were modified once the lesson was acted on.',
    )
    encoded_at: datetime | None = Field(default=None)

    slipped_past_criteria: list[str] = Field(
        default_factory=list,
        description='Criterion names that should have caught this but did not.',
    )
    proposed_criterion: str | None = Field(
        default=None,
        description='Sketch for a new criterion that would catch this in future.',
    )

    body: str = Field(min_length=1, description='Free-form markdown — the lesson itself.')


def parse_lesson_file(path: Path | str) -> Lesson:
    """Parse a frontmatter+markdown lesson file into a validated Lesson.

    Format:
        ---
        <YAML frontmatter>
        ---
        <markdown body>
    """
    p = Path(path)
    text = p.read_text()
    if not text.startswith('---\n'):
        raise ValueError(f'{p}: missing YAML frontmatter (must start with `---`)')
    parts = text.split('---\n', 2)
    if len(parts) < 3:
        raise ValueError(f'{p}: malformed frontmatter — expected `---` delimiters before and after')
    _, frontmatter_text, body = parts
    frontmatter: dict[str, Any] = yaml.safe_load(frontmatter_text) or {}
    frontmatter['body'] = body.strip()
    return Lesson.model_validate(frontmatter)


def load_all_lessons(catalog_dir: Path | None = None) -> list[Lesson]:
    """Load every `*.md` file in the catalog directory. Skips README/index files."""
    root = catalog_dir or CATALOG_DIR
    if not root.exists():
        return []
    lessons: list[Lesson] = []
    for path in sorted(root.rglob('*.md')):
        if path.name.startswith(('README', 'INDEX', '_')):
            continue
        lessons.append(parse_lesson_file(path))
    return lessons
