"""Initiatives endpoint tests — validation, listing, status lookup.

The execution path (POST /initiatives → real run_initiative) is mocked
because real execution would call the Anthropic API. We focus on the
contract surface: validation, error shapes, list/lookup behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_post_with_unknown_initiative_returns_404_with_available() -> None:
    response = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    assert response.status_code == 404
    detail = response.json()['detail']
    assert 'message' in detail
    assert 'does-not-exist-xyz' in detail['message']
    assert isinstance(detail['available'], list)
    assert len(detail['available']) > 0, 'expected at least one initiative listed'


def test_post_missing_body_field_returns_422() -> None:
    response = client.post('/initiatives', json={})
    assert response.status_code == 422


def test_validate_endpoint_returns_initiative_model_for_known() -> None:
    listed = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    available = listed.json()['detail']['available']
    target = available[0]
    response = client.get(f'/initiatives/_validate/{target}')
    assert response.status_code == 200
    body = response.json()
    assert body['name'] == target


def test_validate_endpoint_returns_404_for_unknown() -> None:
    response = client.get('/initiatives/_validate/does-not-exist-xyz')
    assert response.status_code == 404


def test_get_unknown_status_returns_404() -> None:
    response = client.get('/initiatives/abc123notreal')
    assert response.status_code == 404


def test_list_initiatives_returns_array() -> None:
    response = client.get('/initiatives')
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_post_valid_initiative_queues_with_mocked_runtime() -> None:
    """POST /initiatives with a valid name returns 202 + queued record when
    the runtime is mocked. Confirms the validation + spawn path works without
    actually firing the agent loop."""
    listed = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    target = listed.json()['detail']['available'][0]

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> int:
        return 0

    with patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative):
        response = client.post('/initiatives', json={'initiative': target})

    assert response.status_code == 202
    body = response.json()
    assert body['initiative'] == target
    assert body['status'] in {'queued', 'running', 'complete'}
    assert 'id' in body
    assert len(body['id']) == 12


def test_cancel_unknown_id_returns_404() -> None:
    response = client.post('/initiatives/unknown-xyz/cancel')
    assert response.status_code == 404
