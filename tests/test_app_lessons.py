"""Lessons endpoint tests — list + detail + 404."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

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


def test_capture_lesson_returns_501_stub() -> None:
    response = client.post('/lessons')
    assert response.status_code == 501
