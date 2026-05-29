"""Tests for the Phase D.4 ``LEARTECH_INITIATIVE_RUNTIME`` dual-path branch.

POST /initiatives now selects one of two code paths based on the
``LEARTECH_INITIATIVE_RUNTIME`` env var:

* ``asyncio`` (default, today's behaviour) — spawns an asyncio.Task in the
  API pod's event loop; the run dies on pod restart.
* ``job`` — calls ``gate.agent.job_runner.spawn_initiative_job`` to start a
  K8s Job pod; the run lives outside the API pod and survives restarts.

These tests pin both branches:

1. Unset / explicit 'asyncio' → asyncio.Task path. No Job spawn occurs.
   Record has ``runtime='asyncio'`` and ``job_name=None``.
2. 'job' → spawn_initiative_job called with expected args; no asyncio.Task
   is created locally; record has ``runtime='job'`` and ``job_name`` set
   to the Job's name (=run_id by D.3 contract).

We mock ``spawn_initiative_job`` rather than reach for a fake cluster — D.3's
own tests already cover the K8s API surface. Here we're asserting the
router's branching + record-construction contract.

We mock ``run_initiative`` on the asyncio path so the background task
returns immediately and the TestClient teardown doesn't race against a
live agent loop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.main import app
from app.routers.initiatives import _current_runtime_mode, _pick_image_for_initiative
from gate.agent.initiative import RunSummary

# A filesystem-resolvable initiative — any will do; we use the same
# 404-then-pick-first-available trick the sibling test_app_initiatives
# tests use so this stays decoupled from any single YAML name.
_client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the in-memory state path for every test in this module.

    The agent's deployed pod sets ``LEARTECH_INITIATIVE_DB_DSN`` in its
    own environment; when pytest inherits that, ``is_db_enabled()``
    returns True and ``register()`` tries to use a session factory that
    the test fixture hasn't initialised. These tests care about the
    router's branching logic, not the DB write-through path — covered
    separately by ``test_initiative_runs_db.py``.
    """
    monkeypatch.delenv(db_module.DSN_ENV, raising=False)
    db_module._reset_for_tests()


def _pick_known_initiative_name() -> str:
    """Pick any filesystem-resolvable initiative name by inspecting a 404
    response's ``available`` list. Keeps the test decoupled from any
    specific YAML."""
    resp = _client.post('/initiatives', json={'initiative': 'does-not-exist-xyz'})
    assert resp.status_code == 404, 'expected 404 to expose the available-names list'
    available = resp.json()['detail']['available']
    assert isinstance(available, list) and available, 'no filesystem initiatives discovered'
    return available[0]


@pytest.fixture
def initiative_name() -> str:
    return _pick_known_initiative_name()


# ---------------------------------------------------------------------------
# Branch selector — _current_runtime_mode
# ---------------------------------------------------------------------------


def test_runtime_mode_defaults_to_asyncio_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_INITIATIVE_RUNTIME', raising=False)
    assert _current_runtime_mode() == 'asyncio'


def test_runtime_mode_lowercases_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'Job')
    assert _current_runtime_mode() == 'job'


def test_runtime_mode_unknown_value_falls_back_to_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in chart values must NOT silently flip to a path the
    operator didn't intend. Unknown → safe default."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'definitely-not-real')
    assert _current_runtime_mode() == 'asyncio'


# ---------------------------------------------------------------------------
# Image picker — _pick_image_for_initiative (D.4 stub; E.1 will extend)
# ---------------------------------------------------------------------------


def test_pick_image_uses_env_override_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/custom:1.2.3')
    assert _pick_image_for_initiative('any-name') == 'ghcr.io/foo/custom:1.2.3'


def test_pick_image_raises_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """D.4.2: no silent fallback. If the chart didn't render the env var,
    spawning a Job pod with a bogus default just guarantees an ErrImagePull
    loop. Raise so the API surfaces a 500 the operator can actually act on."""
    monkeypatch.delenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', raising=False)
    with pytest.raises(RuntimeError, match='LEARTECH_INITIATIVE_DEFAULT_IMAGE'):
        _pick_image_for_initiative('any-name')


def test_pick_image_accepts_language_kwarg_no_behaviour_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase E.2: the picker accepts an optional ``language`` kwarg. Until E.1's
    routing refactor lands, behaviour is unchanged regardless of value — the
    kwarg is plumbed through so E.1 only has to touch the function body, not
    every caller."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/default:1.0')
    # None (default), known, and unknown language values all return the default
    # image at the D.4 stub stage. Unknown languages must NOT raise here — the
    # picker is the only place that decides what's "known".
    assert _pick_image_for_initiative('any-name') == 'ghcr.io/foo/default:1.0'
    assert _pick_image_for_initiative('any-name', language=None) == 'ghcr.io/foo/default:1.0'
    assert _pick_image_for_initiative('any-name', language='angular') == 'ghcr.io/foo/default:1.0'
    assert _pick_image_for_initiative('any-name', language='kotlin') == 'ghcr.io/foo/default:1.0'


# ---------------------------------------------------------------------------
# POST /initiatives — asyncio path (default, today's behaviour)
# ---------------------------------------------------------------------------


def test_post_unset_runtime_uses_asyncio_path(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LEARTECH_INITIATIVE_RUNTIME is unset, the POST handler must
    use the asyncio.Task path and NOT call spawn_initiative_job."""
    monkeypatch.delenv('LEARTECH_INITIATIVE_RUNTIME', raising=False)

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> RunSummary:
        return RunSummary(exit_code=0)

    fake_spawn = AsyncMock()
    with (
        patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative),
        patch('gate.agent.job_runner.spawn_initiative_job', new=fake_spawn),
    ):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body['runtime'] == 'asyncio'
    assert body['job_name'] is None
    fake_spawn.assert_not_called()


def test_post_explicit_asyncio_runtime_uses_asyncio_path(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit ``LEARTECH_INITIATIVE_RUNTIME=asyncio`` must behave the same
    as unset — the env var being PRESENT shouldn't change behaviour, only
    its VALUE."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'asyncio')

    async def fake_run_initiative(*_args: object, **_kwargs: object) -> RunSummary:
        return RunSummary(exit_code=0)

    fake_spawn = AsyncMock()
    with (
        patch('app.routers.initiatives.run_initiative', side_effect=fake_run_initiative),
        patch('gate.agent.job_runner.spawn_initiative_job', new=fake_spawn),
    ):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body['runtime'] == 'asyncio'
    assert body['job_name'] is None
    fake_spawn.assert_not_called()


# ---------------------------------------------------------------------------
# POST /initiatives — Job path (Phase D.4 opt-in)
# ---------------------------------------------------------------------------


def test_post_job_runtime_spawns_k8s_job(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With LEARTECH_INITIATIVE_RUNTIME=job, the handler must call
    spawn_initiative_job, NOT create an asyncio.Task, and surface the
    Job's name on the record."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'job')
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    # Capture the spawn arguments so we can pin the contract.
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs: Any) -> tuple[str, str]:
        captured.update(kwargs)
        return kwargs['run_id'], kwargs['namespace']

    fake_run_initiative = AsyncMock()
    with (
        patch('gate.agent.job_runner.spawn_initiative_job', side_effect=fake_spawn),
        patch('app.routers.initiatives.run_initiative', new=fake_run_initiative),
    ):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body['runtime'] == 'job'
    # job_name equals the run_id by D.3 contract.
    assert body['job_name'] == body['id']

    # spawn_initiative_job was called with the expected fan-out.
    assert captured['initiative_name'] == initiative_name
    assert captured['run_id'] == body['id']
    assert captured['namespace'] == 'jx-staging'
    assert captured['image'] == 'ghcr.io/foo/agent:test'
    # env + secret_refs come from the helpers — only assert shape here so
    # the test isn't tied to a specific env layout.
    assert isinstance(captured['env'], dict)
    assert isinstance(captured['secret_refs'], dict)

    # Crucially: the asyncio agent loop is NOT invoked. The Job pod runs
    # its own copy of run_initiative; the API pod must not start one too.
    fake_run_initiative.assert_not_called()


def test_post_job_runtime_requires_pod_namespace(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POD_NAMESPACE must be set when runtime='job' — without it the Job
    spawn has no target namespace. The chart's Deployment injects this
    via downward-API fieldRef; missing it indicates a misconfiguration
    that should fail loudly (500) rather than guess a fallback."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'job')
    monkeypatch.delenv('POD_NAMESPACE', raising=False)

    fake_spawn = AsyncMock()
    with patch('gate.agent.job_runner.spawn_initiative_job', new=fake_spawn):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 500
    assert 'POD_NAMESPACE' in resp.json()['detail']
    fake_spawn.assert_not_called()


def test_post_job_runtime_spawn_failure_surfaces_502(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If spawn_initiative_job raises (e.g. K8s API timeout, RBAC denial),
    the handler must surface a 502 rather than letting the exception
    bubble as a generic 500. Operators reading the API logs need to
    know a Job spawn failed."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_RUNTIME', 'job')
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')

    async def boom(**_kwargs: Any) -> tuple[str, str]:
        raise RuntimeError('simulated K8s API failure')

    with patch('gate.agent.job_runner.spawn_initiative_job', side_effect=boom):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 502
    assert 'Failed to spawn initiative Job' in resp.json()['detail']


# ---------------------------------------------------------------------------
# Helper: _initiative_env / _initiative_secret_refs forward sensitive values
# correctly. Tested at the helper level so we don't need the full HTTP stack.
# ---------------------------------------------------------------------------


def test_initiative_env_only_forwards_known_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The forwarded env list is explicit (not a wildcard) so unrelated
    local vars never leak into a spawned Job. Set one allowed key and
    one disallowed key and verify only the allowed key flows through."""
    from app.routers.initiatives import _initiative_env

    monkeypatch.setenv('LEARTECH_REPO_ROOT', '/workspace')
    monkeypatch.setenv('SOMETHING_UNRELATED', 'should-not-flow')

    env = _initiative_env()
    assert env['LEARTECH_REPO_ROOT'] == '/workspace'
    assert 'SOMETHING_UNRELATED' not in env


def test_initiative_secret_refs_uses_chart_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without env overrides the secret refs must match the chart's
    `secrets.*` defaults (ai-review-api-keys + tekton-git)."""
    from app.routers.initiatives import _initiative_secret_refs

    for key in (
        'LEARTECH_JOB_ANTHROPIC_SECRET_NAME',
        'LEARTECH_JOB_ANTHROPIC_SECRET_KEY',
        'LEARTECH_JOB_GH_TOKEN_SECRET_NAME',
        'LEARTECH_JOB_GH_TOKEN_SECRET_KEY',
        'LEARTECH_JOB_DB_DSN_SECRET_NAME',
        'LEARTECH_JOB_DB_DSN_SECRET_KEY',
    ):
        monkeypatch.delenv(key, raising=False)

    refs = _initiative_secret_refs()
    assert refs['ANTHROPIC_API_KEY'] == {'secret': 'ai-review-api-keys', 'key': 'CLAUDE_API_KEY'}
    assert refs['GH_TOKEN'] == {'secret': 'tekton-git', 'key': 'password'}
    # DSN is only forwarded when both env vars are set (gated on
    # chart's postgresql.enabled).
    assert 'LEARTECH_INITIATIVE_DB_DSN' not in refs


def test_initiative_secret_refs_includes_db_dsn_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the API pod has the LEARTECH_JOB_DB_DSN_SECRET_* env pair set
    (chart wires this when postgresql.enabled), the Job must inherit the
    same DSN secret reference."""
    from app.routers.initiatives import _initiative_secret_refs

    monkeypatch.setenv('LEARTECH_JOB_DB_DSN_SECRET_NAME', 'leartech-automated-agent-db')
    monkeypatch.setenv('LEARTECH_JOB_DB_DSN_SECRET_KEY', 'dsn')

    refs = _initiative_secret_refs()
    assert refs['LEARTECH_INITIATIVE_DB_DSN'] == {
        'secret': 'leartech-automated-agent-db',
        'key': 'dsn',
    }
