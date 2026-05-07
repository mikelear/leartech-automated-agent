"""Lessons-catalog endpoints — list + detail.

Read-only against `gate/agent/lessons/catalog/`. Capture (write) endpoint
remains a stub until v1.5; the existing `uv run lessons capture` CLI is
authoritative for now.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from gate.agent.lessons.loader import Lesson, load_all_lessons

router = APIRouter()


class LessonSummary(BaseModel):
    id: str
    title: str
    category: str
    status: str
    captured_at: datetime
    applies_to: list[str]


def _summarise(lesson: Lesson) -> LessonSummary:
    return LessonSummary(
        id=lesson.id,
        title=lesson.title,
        category=lesson.category,
        status=lesson.status,
        captured_at=lesson.captured_at,
        applies_to=lesson.applies_to,
    )


@router.get('', response_model=list[LessonSummary])
async def list_lessons() -> list[LessonSummary]:
    """List every lesson in the catalog."""
    return [_summarise(lesson) for lesson in load_all_lessons()]


@router.get('/{lesson_id}', response_model=Lesson)
async def get_lesson(lesson_id: str) -> Lesson:
    """Return the full lesson — frontmatter + body markdown."""
    for lesson in load_all_lessons():
        if lesson.id == lesson_id:
            return lesson
    raise HTTPException(status_code=404, detail=f'No lesson with id {lesson_id!r}')


@router.post('', status_code=501)
async def capture_lesson() -> None:
    """Capture a new lesson. Use `uv run lessons capture` until v1.5."""
    raise HTTPException(status_code=501, detail='Capture not yet wired — use the CLI for now')
