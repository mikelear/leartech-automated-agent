"""Initiatives endpoint tests — validation, listing, status lookup.

The execution path (POST /initiatives → real run_initiative) is mocked
because real execution would call the Anthropic API. We focus on the
contract surface: validation, error shapes, list/lookup behaviour.

Includes tests for the catalog-first resolution path introduced by
feat(api): catalog-fire-fallback — DB-stored initiatives can be fired
without an image rebuild; filesystem serves as fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import NoReturn
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app import db as db_module
from app.db.models import Base
from app.main import app
from gate.agent.initiative import RunSummary

client = TestClient(app)

# ─── DB-backed fixtures (for catalog-first tests) ────────────────────────────

# Minimal valid YAML that the loader accepts — name must match what's used in tests.
_DB_ONLY_YAML = """\
name: db-only-initiative-xyz
repo: leartech-test
branch: agent/db-only
base: main
goal: Initiative that exists only in the DB catalog, not on the filesystem.
"""

_FS_YAML_NAME_IN_DB = """\
name: {name}
repo: leartech-test-override
branch: agent/db-wins
base: main
goal: DB copy of a filesystem initiative — DB entry should win.
"""


@pytest_asyncio.fixture
async def db_enabled_for_resolution(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Enable DB with an in-memory SQLite engine for testing _resolve_yaml_path directly.

    Does NOT create a TestClient — we test the helper at the function level to
    avoid the TestClient teardown / background-task / SQLite CancelledError
    interaction that occurs when app.state.update also writes to the same
    SQLite instance during task cancellation.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await db_module.dispose_engine()
    db_module._reset_for_tests()


@pytest_asyncio.fixture
async def client_with_db(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """TestClient wired to an in-memory SQLite DB.

    Used only by tests that do NOT fire an initiative (i.e. name doesn't
    resolve → 404 returned before creating a background task). This avoids
    the app.state.update → SQLite CancelledError during TestClient teardown.
    """
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    with TestClient(app) as c:
        yield c

    await db_module.dispose_engine()
    db_module._reset_for_tests()


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

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> RunSummary:
        return RunSummary(exit_code=0)

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


def test_completed_run_surfaces_pr_number_turns_cost() -> None:
    """RunSummary fields (pr_number/turns/cost_usd) must reach the GET response.

    Regression guard: the original handler discarded everything except exit_code,
    so `GET /initiatives/{id}` showed pr_number=null even after the agent opened
    the PR. The fix returns a RunSummary and the handler unpacks the fields.
    """
    listed = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    target = listed.json()['detail']['available'][0]

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> RunSummary:
        return RunSummary(exit_code=0, turns=7, cost_usd=0.4242, pr_number=99)

    with patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative):
        post_resp = client.post('/initiatives', json={'initiative': target})
        run_id = post_resp.json()['id']
        # The background task is scheduled on the same event loop; TestClient
        # blocks long enough for it to complete by the time the GET returns.
        for _ in range(20):
            get_resp = client.get(f'/initiatives/{run_id}')
            body = get_resp.json()
            if body['status'] == 'complete':
                break

    assert body['status'] == 'complete'
    assert body['pr_number'] == 99
    assert body['turns'] == 7
    assert body['cost_usd'] == 0.4242


# ─── Catalog-first resolution tests (feat: catalog-fire-fallback) ────────────
# Tests 1-3 test _resolve_yaml_path directly (no HTTP / no background task) to
# avoid the TestClient teardown / asyncio cancel interaction with SQLite.
# Test 4 uses HTTP but does NOT fire an initiative (404 path only).


async def test_resolve_yaml_path_db_only_returns_tmp_path(
    db_enabled_for_resolution: None,
) -> None:
    """_resolve_yaml_path returns /tmp/agent-catalog/<name>.yaml for a DB-only initiative.

    Verifies test_catalog_fire_fallback: DB catalog is checked first, yaml_body
    is materialised to the tmp path, and that path is what run_initiative gets.
    """
    from app import db as db_m
    from app.db.initiative_catalog import create_initiative as ci
    from app.routers.initiatives import _resolve_yaml_path

    # Seed a DB-only initiative (no corresponding filesystem file).
    async with db_m.session() as sess:
        await ci(sess, name='db-only-initiative-xyz', yaml_body=_DB_ONLY_YAML)

    result = await _resolve_yaml_path('db-only-initiative-xyz')

    assert result is not None, '_resolve_yaml_path must return a Path for a DB-stored name'
    assert result.parent.name == 'agent-catalog', f'Expected /tmp/agent-catalog/<name>.yaml, got {result}'
    assert result.exists(), 'Materialised YAML must be written to disk'
    assert result.read_text() == _DB_ONLY_YAML, 'Materialised content must match DB yaml_body'


async def test_resolve_yaml_path_filesystem_fallback(
    db_enabled_for_resolution: None,
) -> None:
    """_resolve_yaml_path falls back to the filesystem when the name is not in the DB.

    Regression for test_catalog_fire_fallback: the filesystem path must still
    work when DB is enabled but has no entry for the requested name.
    """
    from app.routers.initiatives import _resolve_yaml_path

    # Use any filesystem name that is NOT in the (empty) DB.
    # We know 'automated-agent-catalog-fire-fallback' is on the filesystem.
    result = await _resolve_yaml_path('automated-agent-catalog-fire-fallback')

    assert result is not None, 'Filesystem fallback must return a Path'
    # Path must be in the cwd/initiatives/ directory, not under /tmp/agent-catalog/
    assert result.parent.name == 'initiatives', f'Filesystem path must be under initiatives/, got {result}'
    assert result.exists(), 'Filesystem YAML must exist'


async def test_resolve_yaml_path_db_wins_over_filesystem(
    db_enabled_for_resolution: None,
) -> None:
    """DB entry must WIN over a same-named filesystem entry.

    test_catalog_fire_fallback — DB is the live source of truth; filesystem is
    the starter pack. When both exist, the DB-sourced path must be returned.
    """
    from app import db as db_m
    from app.db.initiative_catalog import create_initiative as ci
    from app.routers.initiatives import _resolve_yaml_path

    # 'automated-agent-catalog-fire-fallback' exists on the filesystem.
    # Seed the DB with a DIFFERENT yaml_body under the same name.
    fs_name = 'automated-agent-catalog-fire-fallback'
    db_yaml = _FS_YAML_NAME_IN_DB.format(name=fs_name)
    async with db_m.session() as sess:
        await ci(sess, name=fs_name, yaml_body=db_yaml)

    result = await _resolve_yaml_path(fs_name)

    assert result is not None
    # DB wins → path is under /tmp/agent-catalog/ (parent directory name = 'agent-catalog')
    assert result.parent.name == 'agent-catalog', f'DB must win; expected /tmp/agent-catalog path, got {result}'
    # Content must be the DB version, not the filesystem version.
    assert 'DB copy of a filesystem initiative' in result.read_text()


def test_404_lists_both_db_and_filesystem_names(client_with_db: TestClient) -> None:
    """404 response must list both DB and filesystem names in `available`.

    test_catalog_fire_fallback — combined discovery for the not-found case.
    """
    # Seed one DB-only name.
    client_with_db.post(
        '/initiatives/catalog',
        json={'name': 'db-only-initiative-xyz', 'yaml_body': _DB_ONLY_YAML},
    )

    resp = client_with_db.post('/initiatives', json={'initiative': 'no-such-initiative-ever'})
    assert resp.status_code == 404
    available = resp.json()['detail']['available']
    assert isinstance(available, list)
    # DB name must appear alongside filesystem names.
    assert 'db-only-initiative-xyz' in available
    # At least one filesystem name must also be present.
    assert len(available) > 1, 'expected both DB and FS names in 404 available list'


async def test_cancel_cleanup_tolerates_db_engine_disposal(
    db_enabled_for_resolution: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancel cleanup must tolerate DB engine being disposed during pod shutdown.

    Regression for the observed issue during run fcbbc53f2650 (2026-05-26):
    when FastAPI shutdown disposes the DB engine before asyncio cancellation
    completes, the update() call in the CancelledError handler fails with
    RuntimeError. The fix wraps the update in try/except to tolerate this.

    The test simulates the race: a background task is cancelled while the
    engine is disposed. The cancel cleanup path should log a warning and
    not raise.
    """
    import asyncio
    from pathlib import Path
    from unittest.mock import patch

    from app import db as db_m
    from app.routers.initiatives import _run_and_track
    from app.state import new_id

    # Set up a mock initiative record in memory.
    initiative_id = new_id()

    # Create a background task that will be cancelled.
    # `started` synchronises deterministically with the task entering
    # run_initiative — replaces the previous `asyncio.sleep(0.01)` race
    # which flaked on contention-pressed nodes (GCP release tb8t6, 6kkjj
    # 2026-05-27) when 10ms wasn't enough for the task to schedule.
    started = asyncio.Event()

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> NoReturn:
        started.set()
        await asyncio.sleep(float('inf'))
        raise AssertionError('unreachable: sleep(inf) only exits via cancellation')

    yaml_path = Path('/tmp/dummy.yaml')  # noqa: S108 — test only, path not created
    with patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative):
        task = asyncio.create_task(_run_and_track(initiative_id, yaml_path))

        # Wait deterministically for the task to enter running state.
        await started.wait()

        # Dispose the DB engine to simulate the pod shutdown race.
        await db_m.dispose_engine()

        # Now cancel the task. This triggers the CancelledError handler.
        # Without the fix, this would raise RuntimeError from update().
        # With the fix, it should log a warning and re-raise CancelledError cleanly.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Assert the warning was logged with the expected substring.
    assert any(
        'DB engine already disposed' in record.message for record in caplog.records if record.levelname == 'WARNING'
    ), f'Expected warning "DB engine already disposed" in logs. Got: {caplog.messages}'


async def test_cancel_cleanup_tolerates_db_programming_error(
    db_enabled_for_resolution: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancel cleanup must tolerate ProgrammingError on closed engine.

    Regression for GCP release tb8t6 (2026-05-27): under cluster contention,
    FastAPI shutdown disposes the engine such that the cancel-cleanup's
    update() raises sqlite3.ProgrammingError (wrapped by SQLAlchemy as
    sqlalchemy.exc.ProgrammingError) instead of RuntimeError. Both surfaces
    must be tolerated — see PR #35 for the RuntimeError case.

    AZ release rwkch on identical code passed the same release pipeline,
    confirming the failure is contention-exposed rather than a code bug.
    Under cluster pressure the engine disposal interleaves DIFFERENTLY:
    the engine connection is closed mid-cleanup rather than the session
    factory being cleared, exposing a different exception surface.

    The test deterministically forces the closed-engine surface by patching
    update() to raise sqlite3.ProgrammingError. Outcome must mirror the
    sibling test: warning logged, CancelledError re-raised cleanly, no
    crash propagating out of _run_and_track.
    """
    import asyncio
    from pathlib import Path
    from sqlite3 import ProgrammingError as SQLiteProgrammingError
    from unittest.mock import patch

    from app.routers.initiatives import _run_and_track
    from app.state import new_id

    initiative_id = new_id()

    # `started` synchronises deterministically with the task entering
    # run_initiative — same flake fix as the sibling test.
    started = asyncio.Event()

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> NoReturn:
        started.set()
        await asyncio.sleep(float('inf'))
        raise AssertionError('unreachable: sleep(inf) only exits via cancellation')

    async def fake_update(_id: str, **fields: object) -> None:
        # Only the cancel-cleanup update should fail — the initial 'running'
        # status update must succeed so we deterministically reach the
        # CancelledError handler. Mirrors the in-cluster surface: when the
        # engine pool's connection is closed mid-operation, sqlite3 raises
        # ProgrammingError (and SQLAlchemy wraps it as
        # sqlalchemy.exc.ProgrammingError). The catch tuple covers both;
        # raising the raw sqlite3 variant here exercises the
        # SQLiteProgrammingError leg of the tuple directly.
        if fields.get('status') == 'cancelled':
            raise SQLiteProgrammingError('Cannot operate on a closed database.')

    yaml_path = Path('/tmp/dummy.yaml')  # noqa: S108 — test only, path not created
    with (
        patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative),
        patch('app.routers.initiatives.update', side_effect=fake_update),
    ):
        task = asyncio.create_task(_run_and_track(initiative_id, yaml_path))

        # Wait deterministically for the task to enter running state.
        await started.wait()

        # Cancel — _run_and_track's CancelledError handler will invoke update(),
        # which now raises SQLiteProgrammingError. The broadened catch must
        # swallow it and re-raise CancelledError cleanly.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # Warning must be logged. Substring 'DB engine already disposed' is the
    # stable contract — same message structure as PR #35's log line, just
    # surfaced for a different underlying exception class.
    assert any(
        'DB engine already disposed' in record.message for record in caplog.records if record.levelname == 'WARNING'
    ), f'Expected warning "DB engine already disposed" in logs. Got: {caplog.messages}'
