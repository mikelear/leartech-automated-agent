"""Unit tests for the lessons catalog — schema, loader, and renderer."""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from gate.agent.lessons.loader import load_all_lessons, parse_lesson_file
from gate.agent.lessons.prompt_renderer import filter_for, render_for


def _write_lesson(path: Path, frontmatter: str, body: str) -> Path:
    path.write_text(f'---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n')
    return path


_VALID_FRONTMATTER = textwrap.dedent(
    """
    id: example
    title: Example lesson
    captured_at: 2026-05-04T12:00:00Z
    source:
      type: agent_run
      reference: pr_99
      observer: claude-sonnet-4-6
    category: calibration
    applies_to:
      - initiative_agent
    status: encoded
    """
)


def test_parses_valid_lesson(tmp_path: Path) -> None:
    path = _write_lesson(tmp_path / 'example.md', _VALID_FRONTMATTER, 'A short body explaining the lesson.')
    lesson = parse_lesson_file(path)
    assert lesson.id == 'example'
    assert lesson.title == 'Example lesson'
    assert lesson.captured_at == datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)
    assert lesson.source.type == 'agent_run'
    assert lesson.source.reference == 'pr_99'
    assert lesson.source.observer == 'claude-sonnet-4-6'
    assert lesson.category == 'calibration'
    assert lesson.applies_to == ['initiative_agent']
    assert lesson.status == 'encoded'
    assert lesson.body.startswith('A short body')


def test_rejects_missing_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / 'no-frontmatter.md'
    path.write_text('Just a body, no frontmatter')
    with pytest.raises(ValueError, match='missing YAML frontmatter'):
        parse_lesson_file(path)


def test_rejects_invalid_source_type(tmp_path: Path) -> None:
    bad = _VALID_FRONTMATTER.replace('type: agent_run', 'type: telepathy')
    path = _write_lesson(tmp_path / 'bad-source.md', bad, 'Body.')
    with pytest.raises(ValidationError):
        parse_lesson_file(path)


def test_rejects_extra_fields(tmp_path: Path) -> None:
    bad = _VALID_FRONTMATTER + '\nbogus_field: yes\n'
    path = _write_lesson(tmp_path / 'extra.md', bad, 'Body.')
    with pytest.raises(ValidationError):
        parse_lesson_file(path)


def test_load_all_lessons_skips_index_files(tmp_path: Path) -> None:
    _write_lesson(tmp_path / 'real.md', _VALID_FRONTMATTER, 'Body.')
    (tmp_path / 'README.md').write_text('# index file, not a lesson')
    (tmp_path / '_template.md').write_text('# template, not a lesson')
    lessons = load_all_lessons(tmp_path)
    assert len(lessons) == 1
    assert lessons[0].id == 'example'


def test_real_catalog_loads_without_errors() -> None:
    """Pin the production catalog — accidental edits surface as test failures."""
    lessons = load_all_lessons()
    assert len(lessons) >= 4, f'Expected at least 4 lessons in the production catalog, found {len(lessons)}'
    # Every lesson must have a non-empty title and ID.
    for lesson in lessons:
        assert lesson.id
        assert lesson.title
        assert lesson.body


def test_filter_for_initiative_agent_returns_only_relevant() -> None:
    relevant = filter_for('initiative_agent')
    assert all('initiative_agent' in lesson.applies_to for lesson in relevant)
    assert all(lesson.category == 'calibration' for lesson in relevant)
    assert all(lesson.status == 'encoded' for lesson in relevant)


def test_render_for_returns_empty_when_no_match(tmp_path: Path) -> None:
    """Rendering for an unknown agent returns empty string (gracefully)."""
    block = render_for('nonexistent_agent', lessons=[])
    assert block == ''


def test_render_for_includes_titles_and_bodies() -> None:
    lessons = load_all_lessons()
    relevant = filter_for('initiative_agent', lessons=lessons)
    if not relevant:
        pytest.skip('No initiative_agent lessons in the catalog yet')
    block = render_for('initiative_agent')
    assert 'Calibrations from past runs' in block
    for lesson in relevant:
        assert lesson.title in block


# ─── repo-tests-may-touch-k8s-api lesson (2026-08-15 incident) ───────────


def test_repo_tests_may_touch_k8s_api_lesson_loads_and_validates() -> None:
    """Frontmatter validates, applies to initiative_agent, status is encoded."""
    lessons = load_all_lessons()
    match = [lesson for lesson in lessons if lesson.id == 'repo-tests-may-touch-k8s-api']
    assert len(match) == 1, 'expected exactly one repo-tests-may-touch-k8s-api lesson in the catalog'
    lesson = match[0]
    assert 'initiative_agent' in lesson.applies_to
    assert lesson.status == 'encoded'
    assert lesson.category == 'calibration'


def test_repo_tests_may_touch_k8s_api_renders_for_initiative_agent() -> None:
    """The lesson makes it through the filter → renderer for the initiative agent.

    If a future edit flips the status back to `open` or drops
    initiative_agent from applies_to, this test surfaces the regression
    immediately — the warning would silently vanish from the prompt.
    """
    block = render_for('initiative_agent')
    assert 'Calibrations from past runs' in block
    # A recognisable phrase from the lesson body. If the wording drifts,
    # update the probe deliberately — do not soften the assertion.
    assert 'live AgentRun identity' in block
    assert 'managedFields' in block, 'diagnostic ladder must reach the prompt intact'
    assert 'load_incluster_config' in block, 'cheap-check probe must reach the prompt intact'


def test_pre_push_validation_still_loads_after_amendment() -> None:
    """Amending pre-push-validation.md to cross-reference the new lesson must
    not break its frontmatter or body — it's the entry point that carries the
    warning to the moment of risk."""
    lessons = load_all_lessons()
    match = [lesson for lesson in lessons if lesson.id == 'pre-push-validation']
    assert len(match) == 1
    lesson = match[0]
    assert lesson.status == 'encoded'
    # The pre-push mandate itself must still be present, not weakened by the
    # cross-reference edit.
    assert 'do NOT push' in lesson.body
    # And the cross-reference to the new lesson must be in place at the point
    # tests are discussed — that's the whole point of the amendment.
    assert 'repo-tests-may-touch-k8s-api' in lesson.body
