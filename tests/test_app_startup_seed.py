"""Tests for seed_catalog_from_filesystem() — startup seeding of the DB catalog.

Verifies the three seed contract points:
  1. Empty DB + N filesystem YAMLs → N catalog rows after seeding.
  2. Existing DB entries are NOT overwritten by seeding (idempotent).
  3. DB-disabled mode: seeding is skipped entirely, no errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import db as db_module
from app.db.initiative_catalog import create_initiative, get_initiative, list_initiatives
from app.db.models import Base
from app.main import seed_catalog_from_filesystem

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """In-memory SQLite session with a live schema — used by async tests."""
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await db_module.dispose_engine()
    db_module._reset_for_tests()


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_yaml(name: str) -> str:
    return f"""\
name: {name}
repo: leartech-test
branch: agent/{name}
base: main
goal: Seeded from test fixture.
"""


# ─── Tests ───────────────────────────────────────────────────────────────────


async def test_seed_populates_empty_db_from_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session: object
) -> None:
    """Empty DB + 3 filesystem YAMLs → 3 DB rows after seeding.

    Verifies test_startup_seed criterion: seed_catalog_from_filesystem() must
    INSERT every YAML found in initiatives/ when the DB has no prior entries.
    """
    # Create a fake initiatives/ directory with 3 YAMLs.
    ini_dir = tmp_path / 'initiatives'
    ini_dir.mkdir()
    names = ['alpha-seed-test', 'bravo-seed-test', 'charlie-seed-test']
    for name in names:
        (ini_dir / f'{name}.yaml').write_text(_make_yaml(name))
    # Underscore-prefixed files must be skipped.
    (ini_dir / '_template.yaml').write_text(_make_yaml('_template'))

    # Patch cwd so seed_catalog_from_filesystem finds tmp_path/initiatives/.
    monkeypatch.chdir(tmp_path)
    await seed_catalog_from_filesystem()

    # Verify all 3 non-prefixed names are now in the DB.
    from app.db import session as sess_ctx

    async with sess_ctx() as sess:
        records = await list_initiatives(sess)

    seeded_names = {r.name for r in records}
    assert names[0] in seeded_names, f'{names[0]} not seeded'
    assert names[1] in seeded_names, f'{names[1]} not seeded'
    assert names[2] in seeded_names, f'{names[2]} not seeded'
    assert '_template' not in seeded_names, 'underscore-prefixed file must be skipped'


async def test_seed_does_not_overwrite_existing_db_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, db_session: object
) -> None:
    """Existing DB entries must NOT be overwritten by the seed.

    Verifies idempotency: if the operator has edited an initiative via PUT
    /initiatives/catalog, a pod restart must not clobber their changes.
    """
    ini_dir = tmp_path / 'initiatives'
    ini_dir.mkdir()
    name = 'already-exists-seed-test'
    (ini_dir / f'{name}.yaml').write_text(_make_yaml(name))

    # Pre-populate the DB with a DIFFERENT yaml_body.
    modified_yaml = _make_yaml(name).replace('Seeded from test fixture.', 'Manually edited goal.')
    from app.db import session as sess_ctx

    async with sess_ctx() as sess:
        await create_initiative(sess, name=name, yaml_body=modified_yaml)

    monkeypatch.chdir(tmp_path)
    await seed_catalog_from_filesystem()

    # DB must still have the manually-edited version.
    async with sess_ctx() as sess:
        record = await get_initiative(sess, name)

    assert record is not None
    assert 'Manually edited goal.' in record.yaml_body, 'seed must not overwrite an existing DB entry'


def test_seed_skipped_when_db_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When DB is not configured, seed_catalog_from_filesystem must be a no-op.

    test_startup_seed: filesystem-only deployments (dev, CI, preview without
    Postgres) must start cleanly — seeding must not raise or attempt DB ops.
    """
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()

    ini_dir = tmp_path / 'initiatives'
    ini_dir.mkdir()
    (ini_dir / 'some-initiative.yaml').write_text(_make_yaml('some-initiative'))
    monkeypatch.chdir(tmp_path)

    # Must complete without error even though no DB engine is configured.
    # asyncio.get_event_loop() raises RuntimeError in Python 3.14 when no loop
    # is bound to the thread (deprecated since 3.12). Use asyncio.run() instead.
    import asyncio

    asyncio.run(seed_catalog_from_filesystem())

    # Confirm DB still uninitialised.
    assert db_module.session_factory_or_none() is None
