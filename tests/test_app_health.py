"""Health endpoint tests — liveness, readiness, root health."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get('/health')
    assert response.status_code == 200
    body = response.json()
    assert body['status'] == 'ok'
    assert body['service'] == 'leartech-automated-agent'
    assert body['version'] == '0.1.0'


def test_healthz_returns_ok() -> None:
    response = client.get('/healthz')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_readyz_returns_ok() -> None:
    response = client.get('/readyz')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'
