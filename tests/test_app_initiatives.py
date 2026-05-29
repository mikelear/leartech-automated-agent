"""Initiatives endpoint tests — validation, listing, status lookup.

Phase F: POST /initiatives always spawns a K8s Job (the in-process
asyncio path was removed). We mock ``spawn_initiative_job`` so these
tests don't touch real K8s and don't fire the Anthropic API. We focus
on the contract surface: validation, error shapes, list/lookup
behaviour.

Includes tests for the catalog-first resolution path introduced by
feat(api): catalog-fire-fallback — DB-stored initiatives can be fired
without an image rebuild; filesystem serves as fallback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app import db as db_module
from app.db.models import Base
from app.main import app

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


def test_post_valid_initiative_queues_with_mocked_job_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /initiatives with a valid name returns 202 + running record
    when the Job spawn is mocked. Confirms the validation + spawn path
    works without actually creating a real K8s Job."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    listed = client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    target = listed.json()['detail']['available'][0]

    async def fake_spawn(**kwargs: Any) -> tuple[str, str]:
        return kwargs['run_id'], kwargs['namespace']

    with patch('gate.agent.job_runner.spawn_initiative_job', side_effect=fake_spawn):
        response = client.post('/initiatives', json={'initiative': target})

    assert response.status_code == 202
    body = response.json()
    assert body['initiative'] == target
    # Phase D.5.3 — record reflects 'running' after a successful spawn.
    assert body['status'] == 'running'
    assert body['runtime'] == 'job'
    assert body['job_name'] == body['id']
    assert 'id' in body
    assert len(body['id']) == 12
    # pr_repo is set at register() time from loaded.primary.qualified_repo
    # — must be present from the very first response (no completion update
    # needed). Self_retrospect (fired by job_reconciler later) depends on
    # pr_repo being on the DB row.
    assert body['pr_repo'] is not None, (
        'pr_repo must be set on the record from spawn time; the '
        'self_retrospect hook (fired by the reconciler on completion) '
        'depends on it being on the DB row.'
    )


def test_cancel_unknown_id_returns_404() -> None:
    response = client.post('/initiatives/unknown-xyz/cancel')
    assert response.status_code == 404


# ─── Phase D.5.1.3 — branch exposed in API response surface ──────────────────
# D.5.1.2 persisted `branch` on the DB row; these tests pin the contract that
# the FastAPI response_model actually surfaces it through both POST and GET so
# operators (and `scripts/list_runs.sh`) can see which branch each run targets.


def test_post_job_mode_response_includes_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /initiatives returns the YAML-declared `branch` in the response body.

    The InitiativeRecord pydantic model exposes `branch` (D.5.1.2), but the
    HTTP surface wasn't covered by any test. This guards the response_model
    serialization end-to-end.
    """
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    # Use a known-on-disk initiative so we can pin the expected branch value.
    # auth-ui-add-about-page declares `branch: agent/add-about-page`.
    target = 'auth-ui-add-about-page'
    expected_branch = 'agent/add-about-page'

    async def fake_spawn(**kwargs: Any) -> tuple[str, str]:
        return kwargs['run_id'], kwargs['namespace']

    with patch('gate.agent.job_runner.spawn_initiative_job', side_effect=fake_spawn):
        response = client.post('/initiatives', json={'initiative': target})

    assert response.status_code == 202
    body = response.json()
    assert 'branch' in body, 'branch must appear in the POST /initiatives response JSON'
    assert body['branch'] == expected_branch, f'expected branch={expected_branch!r} from YAML, got {body["branch"]!r}'


def test_get_initiative_returns_branch_after_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /initiatives/{id} returns the same branch as the POST response.

    Round-trips through the in-memory store and FastAPI response_model so a
    regression in either path (store read, pydantic serialization) is caught.
    """
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    target = 'auth-ui-add-about-page'
    expected_branch = 'agent/add-about-page'

    async def fake_spawn(**kwargs: Any) -> tuple[str, str]:
        return kwargs['run_id'], kwargs['namespace']

    with patch('gate.agent.job_runner.spawn_initiative_job', side_effect=fake_spawn):
        post_response = client.post('/initiatives', json={'initiative': target})
    assert post_response.status_code == 202
    run_id = post_response.json()['id']

    get_response = client.get(f'/initiatives/{run_id}')
    assert get_response.status_code == 200
    body = get_response.json()
    assert body['branch'] == expected_branch, (
        f'GET /initiatives/{{id}} must surface the persisted branch; '
        f'expected {expected_branch!r}, got {body["branch"]!r}'
    )


@pytest.mark.asyncio
async def test_get_initiative_returns_none_branch_for_legacy_record() -> None:
    """Records registered without a `branch` (legacy / pre-D.5.1.2) surface branch=None.

    Operators reading the API for an old DB row must see an explicit None, not
    a missing field or a KeyError. We register a record directly through the
    state layer (bypassing the router) to simulate the pre-migration shape.
    """
    from app.state import InitiativeRecord, new_id, now, register

    initiative_id = new_id()
    await register(
        InitiativeRecord(
            id=initiative_id,
            initiative='legacy-no-branch',
            status='running',
            started_at=now(),
            # branch intentionally omitted — defaults to None on the model.
        )
    )

    response = client.get(f'/initiatives/{initiative_id}')
    assert response.status_code == 200
    body = response.json()
    assert 'branch' in body, 'branch field must always be present in the response JSON'
    assert body['branch'] is None, f'legacy record without branch must serialise to None, got {body["branch"]!r}'


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
