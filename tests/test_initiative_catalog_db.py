"""Tests for the DB-backed initiative catalog — CRUD layer + REST endpoints.

Uses in-memory SQLite (aiosqlite) so tests don't need Postgres. Production
uses Postgres via asyncpg — same SQLAlchemy 2.0 ORM, only the driver
differs. The InitiativeRow model uses only portable types (String, Text,
DateTime, timestamps) so the SQLite + Postgres behaviour matches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import db as db_module
from app.db.initiative_catalog import (
    create_initiative,
    delete_initiative,
    get_initiative,
    list_initiatives,
    update_initiative,
)
from app.db.models import Base
from app.main import app

# A minimal valid initiative YAML — passes the canonical loader without surprises.
VALID_YAML = """\
name: test-initiative-x
repo: leartech-test
branch: agent/test-x
base: main
goal: Test the DB catalogue end-to-end.
"""


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Per-test in-memory SQLite session with the schema applied."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest_asyncio.fixture
async def client_with_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """TestClient wired to an in-memory SQLite engine via the real lifespan.

    Patches the env var so is_db_enabled() returns True, then sets up the
    engine + schema before yielding the client.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()

    # Manual engine setup mirrors what lifespan does, but lets us also
    # create the schema before the client makes requests.
    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with TestClient(app) as client:
        yield client

    await db_module.dispose_engine()
    db_module._reset_for_tests()


@pytest.fixture
def client_no_db(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with DSN unset — endpoints should return 503."""
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()
    return TestClient(app)


# ─── CRUD layer tests (no FastAPI) ──────────────────────────────────────


async def test_create_and_get_roundtrip(db_session: AsyncSession) -> None:
    rec = await create_initiative(db_session, name='hello', yaml_body=VALID_YAML, description='doc')
    assert rec.name == 'hello'
    assert rec.yaml_body == VALID_YAML
    assert rec.description == 'doc'
    assert rec.created_by is None  # auth deferred

    fetched = await get_initiative(db_session, 'hello')
    assert fetched is not None
    assert fetched.name == 'hello'


async def test_get_returns_none_when_not_found(db_session: AsyncSession) -> None:
    assert await get_initiative(db_session, 'nope') is None


async def test_list_returns_ordered_by_name(db_session: AsyncSession) -> None:
    for name in ('charlie', 'alpha', 'bravo'):
        await create_initiative(db_session, name=name, yaml_body=VALID_YAML)
    records = await list_initiatives(db_session)
    assert [r.name for r in records] == ['alpha', 'bravo', 'charlie']


async def test_update_partial(db_session: AsyncSession) -> None:
    await create_initiative(db_session, name='upd', yaml_body=VALID_YAML, description='before')
    updated = await update_initiative(db_session, name='upd', description='after')
    assert updated is not None
    assert updated.description == 'after'
    assert updated.yaml_body == VALID_YAML  # unchanged


async def test_update_returns_none_when_not_found(db_session: AsyncSession) -> None:
    assert await update_initiative(db_session, name='ghost', description='x') is None


async def test_delete(db_session: AsyncSession) -> None:
    await create_initiative(db_session, name='temp', yaml_body=VALID_YAML)
    assert await delete_initiative(db_session, 'temp') is True
    assert await delete_initiative(db_session, 'temp') is False  # already gone


# ─── REST endpoint tests ────────────────────────────────────────────────


def test_endpoints_return_503_when_db_disabled(client_no_db: TestClient) -> None:
    """All four endpoints must surface 503 cleanly when DSN is unset."""
    for method, path in (
        ('GET', '/initiatives/catalog'),
        ('GET', '/initiatives/catalog/anything'),
        ('POST', '/initiatives/catalog'),
        ('PUT', '/initiatives/catalog/anything'),
        ('DELETE', '/initiatives/catalog/anything'),
    ):
        resp = client_no_db.request(method, path, json={'name': 'x', 'yaml_body': VALID_YAML})
        assert resp.status_code == 503, f'{method} {path} returned {resp.status_code}'
        assert 'LEARTECH_INITIATIVE_DB_DSN' in resp.json()['detail']


def test_create_and_list(client_with_db: TestClient) -> None:
    resp = client_with_db.post('/initiatives/catalog', json={
        'name': 'test-initiative-x',
        'yaml_body': VALID_YAML,
        'description': 'roundtrip via REST',
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['name'] == 'test-initiative-x'
    assert body['description'] == 'roundtrip via REST'

    list_resp = client_with_db.get('/initiatives/catalog')
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1
    assert list_resp.json()[0]['name'] == 'test-initiative-x'


def test_create_rejects_invalid_yaml(client_with_db: TestClient) -> None:
    resp = client_with_db.post('/initiatives/catalog', json={
        'name': 'broken',
        'yaml_body': 'this is not valid yaml: [unclosed',
    })
    assert resp.status_code == 422
    assert 'Invalid initiative YAML' in resp.json()['detail']


def test_create_rejects_name_mismatch(client_with_db: TestClient) -> None:
    """If the YAML's `name:` doesn't match the API-supplied name, 422."""
    resp = client_with_db.post('/initiatives/catalog', json={
        'name': 'api-said-this',
        'yaml_body': VALID_YAML,  # has name: test-initiative-x inside
    })
    assert resp.status_code == 422
    assert 'name:' in resp.json()['detail']


def test_create_409_on_duplicate(client_with_db: TestClient) -> None:
    body = {'name': 'test-initiative-x', 'yaml_body': VALID_YAML}
    client_with_db.post('/initiatives/catalog', json=body)
    resp = client_with_db.post('/initiatives/catalog', json=body)
    assert resp.status_code == 409


def test_get_404(client_with_db: TestClient) -> None:
    resp = client_with_db.get('/initiatives/catalog/nope')
    assert resp.status_code == 404


def test_update_404(client_with_db: TestClient) -> None:
    resp = client_with_db.put('/initiatives/catalog/nope', json={'description': 'x'})
    assert resp.status_code == 404


def test_delete_204_and_then_404(client_with_db: TestClient) -> None:
    client_with_db.post('/initiatives/catalog', json={
        'name': 'test-initiative-x', 'yaml_body': VALID_YAML,
    })
    resp = client_with_db.delete('/initiatives/catalog/test-initiative-x')
    assert resp.status_code == 204
    # Second delete: 404
    resp = client_with_db.delete('/initiatives/catalog/test-initiative-x')
    assert resp.status_code == 404
