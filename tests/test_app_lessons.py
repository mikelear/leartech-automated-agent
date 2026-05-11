"""Lessons endpoint tests — list, detail, 404, capture round-trip."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from gate.agent.lessons.loader import CATALOG_DIR

client = TestClient(app)


def test_list_lessons_returns_summaries() -> None:
    response = client.get('/lessons')
    assert response.status_code == 200
    lessons = response.json()
    assert isinstance(lessons, list)
    assert len(lessons) > 0, 'expected at least one lesson in the catalog'
    first = lessons[0]
    # LessonSummary shape
    for field in ('id', 'title', 'category', 'status', 'captured_at', 'applies_to'):
        assert field in first, f'missing field {field!r} in summary'


def test_get_existing_lesson_returns_full_model() -> None:
    listed = client.get('/lessons').json()
    target_id = listed[0]['id']
    response = client.get(f'/lessons/{target_id}')
    assert response.status_code == 200
    body = response.json()
    assert body['id'] == target_id
    # Lesson model has body field that LessonSummary doesn't expose
    assert 'body' in body
    assert len(body['body']) > 0


def test_get_unknown_lesson_returns_404() -> None:
    response = client.get('/lessons/this-lesson-does-not-exist')
    assert response.status_code == 404
    assert 'not' in response.json()['detail'].lower()


def _sample_lesson_payload(lesson_id: str) -> dict[str, object]:
    return {
        'id': lesson_id,
        'title': f'test lesson {lesson_id}',
        'captured_at': datetime.now(UTC).isoformat(),
        'source': {
            'type': 'manual_review',
            'reference': 'test-suite',
            'observer': 'pytest',
            'latency_to_capture': 'seconds',
        },
        'category': 'calibration',
        'applies_to': ['initiative_agent'],
        'status': 'open',
        'body': 'This is a test lesson body. It needs to be non-empty.',
    }


def test_capture_lesson_writes_file_and_returns_summary(tmp_path: Path) -> None:
    """Round-trip: POST a Lesson → file lands in catalog → GET it back."""
    lesson_id = 'pytest-capture-roundtrip-9f3a'
    target = CATALOG_DIR / f'{lesson_id}.md'
    target.unlink(missing_ok=True)  # ensure clean slate

    try:
        response = client.post('/lessons', json=_sample_lesson_payload(lesson_id))
        assert response.status_code == 201, response.text
        summary = response.json()
        assert summary['id'] == lesson_id
        assert summary['category'] == 'calibration'

        # File actually landed on disk
        assert target.exists()
        contents = target.read_text()
        assert contents.startswith('---\n')
        assert lesson_id in contents

        # Loader can read it back
        get_response = client.get(f'/lessons/{lesson_id}')
        assert get_response.status_code == 200
        assert get_response.json()['id'] == lesson_id
    finally:
        target.unlink(missing_ok=True)


def test_capture_lesson_refuses_duplicate_id(tmp_path: Path) -> None:
    lesson_id = 'pytest-duplicate-check-7b2c'
    target = CATALOG_DIR / f'{lesson_id}.md'
    target.unlink(missing_ok=True)

    try:
        first = client.post('/lessons', json=_sample_lesson_payload(lesson_id))
        assert first.status_code == 201

        second = client.post('/lessons', json=_sample_lesson_payload(lesson_id))
        assert second.status_code == 409
        assert 'already exists' in second.json()['detail'].lower()
    finally:
        target.unlink(missing_ok=True)


def test_capture_lesson_rejects_invalid_id() -> None:
    bad_ids = ['..escape', '/abs/path', 'has spaces', 'CapsHere', '-leading-hyphen', 'trailing-']
    for bad_id in bad_ids:
        payload = _sample_lesson_payload(bad_id)
        response = client.post('/lessons', json=payload)
        # Could be 422 from our regex check OR 422 from Pydantic — either rejects it
        assert response.status_code == 422, f'expected 422 for id {bad_id!r}, got {response.status_code}'


def test_capture_lesson_rejects_malformed_body() -> None:
    response = client.post('/lessons', json={'id': 'incomplete', 'title': 'no body or source'})
    assert response.status_code == 422
