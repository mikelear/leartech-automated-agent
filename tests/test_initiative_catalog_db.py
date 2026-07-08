"""Tests for the DB-backed initiative catalog — CRUD layer + REST endpoints.

Uses in-memory SQLite (aiosqlite) so tests don't need Postgres. Production
uses Postgres via asyncpg — same SQLAlchemy 2.0 ORM, only the driver
differs. The InitiativeRow model uses only portable types (String, Text,
DateTime, timestamps) so the SQLite + Postgres behaviour matches.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

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


def test_mutating_endpoints_return_503_when_db_disabled(client_no_db: TestClient) -> None:
    """Mutating + single-item read endpoints must surface 503 cleanly when DSN is unset.

    The list endpoint (``GET /initiatives/catalog``) is deliberately excluded:
    it falls back to the filesystem (``initiatives/*.yaml``) when the DB is
    disabled so the paginated catalog-walk terminates the same way in both
    deployment modes.
    """
    for method, path in (
        ('GET', '/initiatives/catalog/anything'),
        ('POST', '/initiatives/catalog'),
        ('PUT', '/initiatives/catalog/anything'),
        ('DELETE', '/initiatives/catalog/anything'),
    ):
        resp = client_no_db.request(method, path, json={'name': 'x', 'yaml_body': VALID_YAML})
        assert resp.status_code == 503, f'{method} {path} returned {resp.status_code}'
        assert 'LEARTECH_INITIATIVE_DB_DSN' in resp.json()['detail']


def test_create_and_list(client_with_db: TestClient) -> None:
    resp = client_with_db.post(
        '/initiatives/catalog',
        json={
            'name': 'test-initiative-x',
            'yaml_body': VALID_YAML,
            'description': 'roundtrip via REST',
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body['name'] == 'test-initiative-x'
    assert body['description'] == 'roundtrip via REST'

    list_resp = client_with_db.get('/initiatives/catalog')
    assert list_resp.status_code == 200
    catalog = list_resp.json()
    # Startup seeding populates the catalog with the baked-in filesystem YAMLs, so
    # we may see more than just the one we inserted — check membership, not exact count.
    names = [item['name'] for item in catalog]
    assert 'test-initiative-x' in names, f'Expected test-initiative-x in catalog; got {names}'


def test_create_rejects_invalid_yaml(client_with_db: TestClient) -> None:
    resp = client_with_db.post(
        '/initiatives/catalog',
        json={
            'name': 'broken',
            'yaml_body': 'this is not valid yaml: [unclosed',
        },
    )
    assert resp.status_code == 422
    assert 'Invalid initiative YAML' in resp.json()['detail']


def test_create_rejects_name_mismatch(client_with_db: TestClient) -> None:
    """If the YAML's `name:` doesn't match the API-supplied name, 422."""
    resp = client_with_db.post(
        '/initiatives/catalog',
        json={
            'name': 'api-said-this',
            'yaml_body': VALID_YAML,  # has name: test-initiative-x inside
        },
    )
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
    client_with_db.post(
        '/initiatives/catalog',
        json={
            'name': 'test-initiative-x',
            'yaml_body': VALID_YAML,
        },
    )
    resp = client_with_db.delete('/initiatives/catalog/test-initiative-x')
    assert resp.status_code == 204
    # Second delete: 404
    resp = client_with_db.delete('/initiatives/catalog/test-initiative-x')
    assert resp.status_code == 404


# ─── Pagination — DB-backed path ────────────────────────────────────────


def _yaml_with_name(name: str) -> str:
    """Build a minimal-valid initiative YAML whose ``name:`` matches ``name``."""
    return f'name: {name}\nrepo: leartech-test\nbranch: agent/test-x\nbase: main\ngoal: Pagination fixture.\n'


def _seed_db_catalog(client: TestClient, count: int, prefix: str = 'pg') -> list[str]:
    """Insert ``count`` initiatives via the create endpoint. Returns the sorted names.

    Uses zero-padded names so the DB's ``ORDER BY name`` produces a
    predictable, stable order for the assertions.
    """
    names = [f'{prefix}-{i:04d}' for i in range(count)]
    for n in names:
        resp = client.post('/initiatives/catalog', json={'name': n, 'yaml_body': _yaml_with_name(n)})
        assert resp.status_code == 201, resp.text
    return sorted(names)


def test_pagination_db_limit_smaller_than_total_returns_exactly_limit(client_with_db: TestClient) -> None:
    _seed_db_catalog(client_with_db, count=25)
    resp = client_with_db.get('/initiatives/catalog?limit=10&offset=0')
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 10


def test_pagination_db_offset_skips_the_right_rows(client_with_db: TestClient) -> None:
    all_names = _seed_db_catalog(client_with_db, count=25)
    resp = client_with_db.get('/initiatives/catalog?limit=1000&offset=0')
    assert resp.status_code == 200
    full = [item['name'] for item in resp.json()]

    resp = client_with_db.get('/initiatives/catalog?limit=10&offset=5')
    assert resp.status_code == 200
    page = [item['name'] for item in resp.json()]
    # Offset skips first 5 rows of the ordered set; the next 10 must match.
    assert page == full[5:15]
    # Sanity: every seeded name is contained in the full listing.
    for name in all_names:
        assert name in full


def test_pagination_db_final_partial_page_returns_less_than_limit(client_with_db: TestClient) -> None:
    _seed_db_catalog(client_with_db, count=25)
    resp = client_with_db.get('/initiatives/catalog?limit=1000&offset=0')
    assert resp.status_code == 200
    total = len(resp.json())

    # Ask for a page starting near the tail — the returned count must be
    # less than the requested limit AND (total - offset).
    tail_offset = total - 3
    resp = client_with_db.get(f'/initiatives/catalog?limit=100&offset={tail_offset}')
    assert resp.status_code == 200
    page = resp.json()
    assert len(page) == 3
    assert len(page) < 100


def test_pagination_db_limit_ge_total_returns_all_remaining(client_with_db: TestClient) -> None:
    _seed_db_catalog(client_with_db, count=25)
    resp = client_with_db.get('/initiatives/catalog?limit=1000&offset=0')
    assert resp.status_code == 200
    total = len(resp.json())

    # limit exactly equal to total → all rows returned
    resp = client_with_db.get(f'/initiatives/catalog?limit={total}&offset=0')
    assert resp.status_code == 200
    assert len(resp.json()) == total

    # limit > total → same result (no over-run)
    resp = client_with_db.get(f'/initiatives/catalog?limit={total + 50}&offset=0')
    assert resp.status_code == 200
    assert len(resp.json()) == total


def test_pagination_db_walk_terminates(client_with_db: TestClient) -> None:
    """A caller walking with increasing offset must eventually see a short page.

    This is the anti-regression for the shipped bug: pagination that
    returns the full catalog on every call sends the walk to its page
    cap forever. With the fix, walking terminates the first time a page
    comes back shorter than ``limit``.
    """
    _seed_db_catalog(client_with_db, count=25)
    limit = 10
    offset = 0
    seen_names: list[str] = []
    for _ in range(20):  # safety cap on the walk
        resp = client_with_db.get(f'/initiatives/catalog?limit={limit}&offset={offset}')
        assert resp.status_code == 200
        page = resp.json()
        seen_names.extend(item['name'] for item in page)
        if len(page) < limit:
            break
        offset += limit
    else:
        raise AssertionError('walk did not terminate within safety cap — pagination is not truncating')
    # No duplicates across pages — stable ordering + offset must not
    # revisit the same row.
    assert len(seen_names) == len(set(seen_names))


def test_pagination_db_rejects_out_of_range_params(client_with_db: TestClient) -> None:
    # limit=0 is rejected (ge=1)
    resp = client_with_db.get('/initiatives/catalog?limit=0&offset=0')
    assert resp.status_code == 422
    # negative offset is rejected (ge=0)
    resp = client_with_db.get('/initiatives/catalog?limit=10&offset=-1')
    assert resp.status_code == 422
    # limit above the 1000 cap is rejected (le=1000)
    resp = client_with_db.get('/initiatives/catalog?limit=1001&offset=0')
    assert resp.status_code == 422


# ─── Pagination — filesystem fallback path ──────────────────────────────


@pytest.fixture
def client_fs_catalog(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    """TestClient with DB disabled + a controlled initiatives/ directory.

    Chdirs into a tmp dir with a synthesised ``initiatives/`` tree so the
    filesystem-fallback code path has a deterministic corpus to page over
    (not the ~200 baked-in real initiatives, which would make the
    assertions repo-state-dependent).
    """
    # Force filesystem-only mode.
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()

    initiatives_dir = tmp_path / 'initiatives'
    initiatives_dir.mkdir()
    for i in range(25):
        name = f'fs-{i:04d}'
        (initiatives_dir / f'{name}.yaml').write_text(
            f'name: {name}\n'
            'repo: leartech-test\n'
            'branch: agent/test-x\n'
            'base: main\n'
            'goal: FS-fallback pagination fixture.\n'
        )
    # `_`-prefixed files must be skipped by the loader.
    (initiatives_dir / '_template.yaml').write_text('name: ignored\n')

    monkeypatch.chdir(tmp_path)
    return TestClient(app)


def test_pagination_fs_limit_smaller_than_total(client_fs_catalog: TestClient) -> None:
    resp = client_fs_catalog.get('/initiatives/catalog?limit=10&offset=0')
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 10


def test_pagination_fs_offset_skips_the_right_rows(client_fs_catalog: TestClient) -> None:
    resp = client_fs_catalog.get('/initiatives/catalog?limit=1000&offset=0')
    assert resp.status_code == 200
    full = [item['name'] for item in resp.json()]
    assert len(full) == 25

    resp = client_fs_catalog.get('/initiatives/catalog?limit=10&offset=5')
    assert resp.status_code == 200
    page = [item['name'] for item in resp.json()]
    assert page == full[5:15]

    # `_`-prefixed template must be filtered out.
    assert 'ignored' not in full
    assert '_template' not in full


def test_pagination_fs_final_partial_page(client_fs_catalog: TestClient) -> None:
    resp = client_fs_catalog.get('/initiatives/catalog?limit=100&offset=22')
    assert resp.status_code == 200
    page = resp.json()
    assert len(page) == 3
    assert len(page) < 100


def test_pagination_fs_limit_ge_total_returns_all_remaining(client_fs_catalog: TestClient) -> None:
    # 25 files, limit exactly at total → 25 rows
    resp = client_fs_catalog.get('/initiatives/catalog?limit=25&offset=0')
    assert resp.status_code == 200
    assert len(resp.json()) == 25

    resp = client_fs_catalog.get('/initiatives/catalog?limit=100&offset=0')
    assert resp.status_code == 200
    assert len(resp.json()) == 25


def test_pagination_fs_walk_terminates(client_fs_catalog: TestClient) -> None:
    """FS-fallback walk terminates on a short final page — same as DB path."""
    limit = 10
    offset = 0
    seen_names: list[str] = []
    for _ in range(20):
        resp = client_fs_catalog.get(f'/initiatives/catalog?limit={limit}&offset={offset}')
        assert resp.status_code == 200
        page = resp.json()
        seen_names.extend(item['name'] for item in page)
        if len(page) < limit:
            break
        offset += limit
    else:
        raise AssertionError('FS walk did not terminate')
    assert len(seen_names) == len(set(seen_names))
