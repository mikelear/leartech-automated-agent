"""Tests for the Phase F Job-mode initiative spawn path.

POST /initiatives now always spawns a K8s Job via
``gate.agent.job_runner.spawn_initiative_job``; the in-process asyncio
task path was removed in Phase F (the chart default ``agent.runtime``
flipped to ``"job"`` and the dual-path branch in
``app.routers.initiatives.start_initiative`` collapsed to a single
Job-spawn code path).

These tests pin the Job-spawn contract:

- ``spawn_initiative_job`` is called with the expected fan-out (image,
  namespace, env, secret_refs, yaml_body).
- The record returned to the caller carries ``runtime='job'``,
  ``job_name`` equal to the run_id, and ``status='running'`` once the
  K8s API has accepted the Job.
- ``POD_NAMESPACE`` is required — without it the handler raises 500.
- spawn failures (RBAC denied, network timeout) surface as 502 so
  operators can act.

The picker tests (``_pick_image_for_initiative``) are kept because they
exercise an orthogonal contract (image selection) that the Job spawn
relies on.

We mock ``spawn_initiative_job`` rather than reach for a fake cluster —
the job_runner module has its own tests covering the K8s API surface.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app.main import app
from app.routers.initiatives import _pick_image_for_initiative

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
# POST /initiatives — Job spawn contract (Phase F)
# ---------------------------------------------------------------------------


def test_post_creates_agentrun(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /initiatives must ensure the per-language AgentType and create an
    AgentRun (the Go controller spawns + tracks the Job). Slice B — job_runner
    is gone; every POST takes this path."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')
    monkeypatch.setenv('LEARTECH_INITIATIVE_DEFAULT_IMAGE', 'ghcr.io/foo/agent:test')

    ensured: dict[str, Any] = {}
    created: dict[str, Any] = {}

    async def fake_ensure(**kwargs: Any) -> str:
        ensured.update(kwargs)
        return 'leartech-agent-py'

    async def fake_create(**kwargs: Any) -> str:
        created.update(kwargs)
        return kwargs['run_id']

    with (
        patch('gate.agent.agentrun_client.ensure_agent_type', side_effect=fake_ensure),
        patch('gate.agent.agentrun_client.create_agent_run', side_effect=fake_create),
    ):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body['runtime'] == 'job'
    assert body['job_name'] == body['id']  # AgentRun name == run_id
    assert body['status'] == 'running'

    # Catalog state updated too, not just the response payload.
    status_resp = _client.get(f'/initiatives/{body["id"]}')
    assert status_resp.status_code == 200, status_resp.text
    assert status_resp.json()['status'] == 'running'

    # AgentType ensured with the routed image; AgentRun created with the run id.
    assert ensured['image'] == 'ghcr.io/foo/agent:test'
    assert created['run_id'] == body['id']
    assert created['namespace'] == 'jx-staging'
    assert created['agent_type'] == 'leartech-agent-py'
    assert isinstance(created['inputs'], dict)


def test_post_requires_pod_namespace(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POD_NAMESPACE must be set — the chart injects it via downward-API
    fieldRef; missing it fails loudly (500) rather than guessing."""
    monkeypatch.delenv('POD_NAMESPACE', raising=False)

    fake_create = AsyncMock()
    with patch('gate.agent.agentrun_client.create_agent_run', new=fake_create):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 500
    assert 'POD_NAMESPACE' in resp.json()['detail']
    fake_create.assert_not_called()


def test_post_agentrun_failure_surfaces_502(
    initiative_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If AgentRun creation raises (K8s API timeout, RBAC denial), the handler
    surfaces a 502 rather than a generic 500."""
    monkeypatch.setenv('POD_NAMESPACE', 'jx-staging')

    async def ok_ensure(**_kwargs: Any) -> str:
        return 'leartech-agent-py'

    async def boom(**_kwargs: Any) -> str:
        raise RuntimeError('simulated K8s API failure')

    with (
        patch('gate.agent.agentrun_client.ensure_agent_type', side_effect=ok_ensure),
        patch('gate.agent.agentrun_client.create_agent_run', side_effect=boom),
    ):
        resp = _client.post('/initiatives', json={'initiative': initiative_name})

    assert resp.status_code == 502
    assert 'Failed to create AgentRun' in resp.json()['detail']


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


def test_initiative_env_forwards_gateway_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway repoint (Phase 1): a spawned Job MUST inherit ANTHROPIC_BASE_URL
    from the API pod. If this drops off the forward list, Jobs silently call
    Anthropic directly — unmetered, unbudgeted, bypassing the gateway. This
    guard makes that regression a red test, not a silent billing leak."""
    from app.routers.initiatives import _JOB_FORWARDED_ENV_KEYS, _initiative_env

    assert 'ANTHROPIC_BASE_URL' in _JOB_FORWARDED_ENV_KEYS
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://leartech-ai-gateway.ai-gateway.svc:8080')
    env = _initiative_env()
    assert env['ANTHROPIC_BASE_URL'] == 'http://leartech-ai-gateway.ai-gateway.svc:8080'


def test_initiative_secret_refs_uses_chart_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without env overrides the secret refs must match the chart's
    `secrets.*` defaults (leartech-ai-gateway-key + tekton-git)."""
    from app.routers.initiatives import _initiative_secret_refs

    for key in (
        'LEARTECH_JOB_LLM_SECRET_NAME',
        'LEARTECH_JOB_LLM_SECRET_KEY',
        'LEARTECH_JOB_ANTHROPIC_SECRET_NAME',
        'LEARTECH_JOB_ANTHROPIC_SECRET_KEY',
        'LEARTECH_JOB_GH_TOKEN_SECRET_NAME',
        'LEARTECH_JOB_GH_TOKEN_SECRET_KEY',
        'LEARTECH_JOB_DB_DSN_SECRET_NAME',
        'LEARTECH_JOB_DB_DSN_SECRET_KEY',
    ):
        monkeypatch.delenv(key, raising=False)

    refs = _initiative_secret_refs()
    # Provider-neutral defaults (renamed off ai-review-api-keys/CLAUDE_API_KEY as
    # part of the ai-gateway migration). The injected env-var name stays
    # ANTHROPIC_API_KEY (SDK contract); only the secret NAME/KEY are neutral.
    assert refs['ANTHROPIC_API_KEY'] == {'secret': 'leartech-ai-gateway-key', 'key': 'AI_GATEWAY_API_KEY'}
    assert refs['GH_TOKEN'] == {'secret': 'tekton-git', 'key': 'password'}
    # DSN is only forwarded when both env vars are set (gated on
    # chart's postgresql.enabled).
    assert 'LEARTECH_INITIATIVE_DB_DSN' not in refs


def test_initiative_secret_refs_neutral_env_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The neutral LEARTECH_JOB_LLM_SECRET_* wins; the legacy
    LEARTECH_JOB_ANTHROPIC_SECRET_* is honoured only as a back-compat fallback
    (clusters mid-migration) so a rename can roll out without a flag-day."""
    from app.routers.initiatives import _initiative_secret_refs

    # Legacy set, neutral unset → fallback used.
    monkeypatch.delenv('LEARTECH_JOB_LLM_SECRET_NAME', raising=False)
    monkeypatch.delenv('LEARTECH_JOB_LLM_SECRET_KEY', raising=False)
    monkeypatch.setenv('LEARTECH_JOB_ANTHROPIC_SECRET_NAME', 'legacy-secret')
    monkeypatch.setenv('LEARTECH_JOB_ANTHROPIC_SECRET_KEY', 'LEGACY_KEY')
    assert _initiative_secret_refs()['ANTHROPIC_API_KEY'] == {'secret': 'legacy-secret', 'key': 'LEGACY_KEY'}

    # Neutral set → wins over legacy.
    monkeypatch.setenv('LEARTECH_JOB_LLM_SECRET_NAME', 'neutral-secret')
    monkeypatch.setenv('LEARTECH_JOB_LLM_SECRET_KEY', 'NEUTRAL_KEY')
    assert _initiative_secret_refs()['ANTHROPIC_API_KEY'] == {'secret': 'neutral-secret', 'key': 'NEUTRAL_KEY'}


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
