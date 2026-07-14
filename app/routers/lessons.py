"""Lessons-catalog endpoints — list, detail, capture.

Read against `gate/agent/lessons/catalog/`; write new lessons there as well.

`POST /lessons` is the integration surface for the three feedback rings:
- Ring 1 (PR-gate) auto-captures via the agent's in-loop CLI today
- Ring 2 (qa-arch staging) posts `source.type: staging_test` lessons here
- Ring 3 (qa-arch forensic) posts `source.type: prod_incident` lessons here
- Manual webhook receivers post `source.type: manual_review` lessons here

The endpoint refuses if a lesson with the same id already exists — it's
create-only; subsequent updates need a manual catalog edit + PR.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import AuthenticatedUser, require_service_caller
from gate.agent.lessons.loader import CATALOG_DIR, Lesson, load_all_lessons

router = APIRouter()

_VALID_ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$')


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


def _lesson_path(lesson_id: str) -> Path:
    """Resolve to <catalog>/<id>.md, refusing any id that could path-escape."""
    if not _VALID_ID_RE.match(lesson_id):
        raise HTTPException(
            status_code=422,
            detail=f'Invalid lesson id {lesson_id!r}: must be kebab-case (a-z, 0-9, hyphens; cannot start/end with hyphen)',
        )
    return CATALOG_DIR / f'{lesson_id}.md'


def _serialise_lesson(lesson: Lesson) -> str:
    """Render a Lesson back into the on-disk frontmatter+markdown format.

    Mirrors `parse_lesson_file`'s expected shape: `---\\n<yaml>\\n---\\n<body>`.
    """
    frontmatter = lesson.model_dump(mode='json', exclude={'body'}, exclude_none=True)
    yaml_text = yaml.safe_dump(frontmatter, sort_keys=False, default_flow_style=False)
    return f'---\n{yaml_text}---\n\n{lesson.body.rstrip()}\n'


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


@router.post('', response_model=LessonSummary, status_code=201)
async def capture_lesson(
    lesson: Lesson,
    # Auth-hardening C1 — `POST /lessons` is the ring-2/ring-3 feeder path
    # (qa-arch, forensic agent, manual-review webhooks). Every writer is a
    # service running under a client-credentials token; no dashboard/user
    # session should be able to inject arbitrary lessons into the catalog
    # (that would let a compromised session shape future agent behaviour).
    _caller: AuthenticatedUser | None = Depends(require_service_caller),
) -> LessonSummary:
    """Capture a new lesson by writing it to the catalog directory.

    Used by qa-arch (rings 2 + 3) and manual-review webhooks to post findings
    that should calibrate the agent on its next session. The Lesson model
    validates the body shape; we add filesystem-level checks (id format,
    no overwrite) on top.
    """
    target = _lesson_path(lesson.id)
    if target.exists():
        raise HTTPException(
            status_code=409,
            detail=f'Lesson {lesson.id!r} already exists. To update, edit the file in a PR; POST is create-only.',
        )
    # Atomic write: write to tmp, then rename, so a partial write can't
    # corrupt the catalog and break agent startup.
    tmp = target.with_suffix('.md.tmp')
    tmp.write_text(_serialise_lesson(lesson))
    tmp.rename(target)
    return _summarise(lesson)
