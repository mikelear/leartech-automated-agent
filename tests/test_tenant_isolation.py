"""Multi-tenant data-isolation tests (v7-P1 step 5 — mirror of orchestrator step 4).

Verifies row-level tenant scoping across:

  - ``initiative_catalog`` — initiative DEFINITIONS, supports per-tenant
    libraries plus a global (NULL tenant_id) shared library.
  - ``initiative_runs``    — initiative EXECUTIONS (a.k.a. ``agent_runs``
    in the v7-P1 spec). Cross-tenant lookups return 404 (not 403) at the
    router so existence is not leaked.

Two scopes exercised:

  1. **CRUD layer** — exercising :func:`create_initiative` / :func:`list_initiatives`
     / :func:`get_initiative` etc. directly with ``tenant_id`` kwargs, against
     an in-memory SQLite session. This is the unit-test layer; it doesn't need
     real bearer tokens.

  2. **HTTP layer** — full request flow: the OIDC middleware extracts
     ``tenant_id`` from a minted bearer, the router stamps it onto rows on
     write and filters by it on read, with cross-tenant access surfacing as
     404.

The HTTP-layer harness re-uses the bearer-minting infrastructure from
``test_auth_middleware.py`` to mint tenant-A and tenant-B tokens against
a stub JWKS cache, so the suite stays self-contained (no network, no Hydra,
no Postgres).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwk, jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.state as state_module
from app import db as db_module
from app.auth import TENANT_RELAY_HEADER
from app.auth.middleware import (
    AuthenticationMiddleware,
    AuthSettings,
    JWKSCache,
)
from app.db.initiative_catalog import (
    create_initiative,
    delete_initiative,
    get_initiative,
    list_initiatives,
    update_initiative,
)
from app.db.initiative_runs import (
    create_run,
    get_run,
    list_runs,
    update_run,
)
from app.db.models import Base
from app.routers.initiative_catalog import router as catalog_router
from app.state import InitiativeRecord, get, list_records, new_id, register
from app.state import update as state_update

# ─── Test-only crypto helpers (mirror of test_auth_middleware.py) ───────


ISSUER = 'https://hydra.test'
AUDIENCE = 'https://agent.test'
SYSTEM_TENANT = 'system-tenant'
KID = 'tenant-iso-test-key'
ALG = 'RS256'


@pytest.fixture(scope='module')
def rsa_keypair() -> tuple[str, dict[str, Any]]:
    """Generate one keypair per test module — token signing + JWKS publication."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    public_jwk = jwk.construct(public_pem, algorithm=ALG).to_dict()
    public_jwk['kid'] = KID
    public_jwk['alg'] = ALG
    public_jwk['use'] = 'sig'
    return private_pem, public_jwk


def _mint(
    private_pem: str,
    *,
    tenant_id: str | None,
    subject: str = 'user-x',
) -> str:
    """Mint an RS256 JWT signed by ``rsa_keypair`` with the given tenant_id."""
    now = int(time.time())
    claims: dict[str, Any] = {
        'iss': ISSUER,
        'aud': AUDIENCE,
        'sub': subject,
        'iat': now,
        'exp': now + 3600,
    }
    if tenant_id is not None:
        # Hydra nests custom access-token claims under `ext` → `ext.tenant_id`.
        claims['ext'] = {'tenant_id': tenant_id}
    return jwt.encode(claims, private_pem, algorithm=ALG, headers={'kid': KID})


class _StubJWKSCache(JWKSCache):
    def __init__(self, settings: AuthSettings, keys: list[dict[str, Any]]) -> None:
        super().__init__(settings)
        self._keys = keys

    def fetch_jwks(self) -> list[dict[str, Any]]:  # noqa: D401 — same shape as parent
        return list(self._keys)


# ─── CRUD layer fixtures ────────────────────────────────────────────────


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with the full schema applied."""
    engine = create_async_engine('sqlite+aiosqlite:///:memory:')
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


# Re-use the auth-middleware suite's keypair pattern but locally so this
# file doesn't import private symbols from another test module.

VALID_YAML = """\
name: tenant-iso-test
repo: leartech-test
branch: agent/test
base: main
goal: Tenant isolation test fixture.
"""


def _started_at() -> datetime:
    return datetime.now(UTC)


# ─── CRUD layer — initiative_catalog ────────────────────────────────────


class TestInitiativeCatalogTenantScoping:
    """Exercise the tenant_id kwargs on :mod:`app.db.initiative_catalog`."""

    async def test_create_stamps_tenant_id(self, db_session: AsyncSession) -> None:
        rec = await create_initiative(db_session, name='alpha-only', yaml_body=VALID_YAML, tenant_id='tenant-alpha')
        assert rec.tenant_id == 'tenant-alpha'

    async def test_create_with_no_tenant_is_global(self, db_session: AsyncSession) -> None:
        rec = await create_initiative(db_session, name='global-init', yaml_body=VALID_YAML)
        assert rec.tenant_id is None

    async def test_list_returns_own_plus_global(self, db_session: AsyncSession) -> None:
        # Seed three rows: tenant-alpha's, tenant-beta's, and a global.
        await create_initiative(db_session, name='alpha-x', yaml_body=VALID_YAML, tenant_id='tenant-alpha')
        await create_initiative(db_session, name='beta-x', yaml_body=VALID_YAML, tenant_id='tenant-beta')
        await create_initiative(db_session, name='global-x', yaml_body=VALID_YAML)

        listed = await list_initiatives(db_session, tenant_id='tenant-alpha')
        names = sorted(r.name for r in listed)
        assert names == ['alpha-x', 'global-x'], 'tenant-alpha must see their own row + global; never tenant-beta'

    async def test_list_without_tenant_returns_everything(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='alpha-x', yaml_body=VALID_YAML, tenant_id='tenant-alpha')
        await create_initiative(db_session, name='beta-x', yaml_body=VALID_YAML, tenant_id='tenant-beta')
        await create_initiative(db_session, name='global-x', yaml_body=VALID_YAML)

        # tenant_id=None is the system-tenant / unauthenticated path —
        # returns the full catalog so admin tooling still works.
        listed = await list_initiatives(db_session)
        names = sorted(r.name for r in listed)
        assert names == ['alpha-x', 'beta-x', 'global-x']

    async def test_get_cross_tenant_returns_none(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='alpha-only', yaml_body=VALID_YAML, tenant_id='tenant-alpha')

        # Tenant-beta querying tenant-alpha's row sees None (router maps to 404).
        assert await get_initiative(db_session, 'alpha-only', tenant_id='tenant-beta') is None
        # Tenant-alpha sees their own.
        assert (await get_initiative(db_session, 'alpha-only', tenant_id='tenant-alpha')) is not None

    async def test_get_global_visible_to_every_tenant(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='shared', yaml_body=VALID_YAML)
        for tenant in ('tenant-alpha', 'tenant-beta', None):
            row = await get_initiative(db_session, 'shared', tenant_id=tenant)
            assert row is not None and row.name == 'shared'

    async def test_update_cross_tenant_returns_none(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='alpha-only', yaml_body=VALID_YAML, tenant_id='tenant-alpha')

        # Tenant-beta tries to edit — refused (router maps to 404).
        assert (
            await update_initiative(db_session, name='alpha-only', description='hijacked', tenant_id='tenant-beta')
            is None
        )

        # Tenant-alpha can edit their own.
        edited = await update_initiative(
            db_session, name='alpha-only', description='legit edit', tenant_id='tenant-alpha'
        )
        assert edited is not None and edited.description == 'legit edit'

    async def test_tenant_cannot_update_global_initiative(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='shared', yaml_body=VALID_YAML)  # global
        # A tenant editing a global row would mutate state every tenant sees — refuse.
        assert (
            await update_initiative(db_session, name='shared', description='tenant edit', tenant_id='tenant-alpha')
            is None
        )
        # System tenant (tenant_id=None) can edit globals — that's how the
        # catalog is curated centrally.
        edited = await update_initiative(db_session, name='shared', description='system edit')
        assert edited is not None and edited.description == 'system edit'

    async def test_delete_cross_tenant_returns_false(self, db_session: AsyncSession) -> None:
        await create_initiative(db_session, name='alpha-only', yaml_body=VALID_YAML, tenant_id='tenant-alpha')
        assert await delete_initiative(db_session, 'alpha-only', tenant_id='tenant-beta') is False
        assert await delete_initiative(db_session, 'alpha-only', tenant_id='tenant-alpha') is True


# ─── CRUD layer — initiative_runs ───────────────────────────────────────


class TestInitiativeRunsTenantScoping:
    async def test_create_run_persists_tenant_id(self, db_session: AsyncSession) -> None:
        rec = await create_run(
            db_session,
            id='rid-alpha',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id='tenant-alpha',
        )
        assert rec.tenant_id == 'tenant-alpha'

    async def test_get_run_cross_tenant_returns_none(self, db_session: AsyncSession) -> None:
        await create_run(
            db_session,
            id='rid-alpha',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id='tenant-alpha',
        )
        assert await get_run(db_session, 'rid-alpha', tenant_id='tenant-beta') is None
        assert await get_run(db_session, 'rid-alpha', tenant_id='tenant-alpha') is not None

    async def test_list_runs_scoped_to_tenant_plus_legacy(self, db_session: AsyncSession) -> None:
        await create_run(
            db_session,
            id='r-alpha',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id='tenant-alpha',
        )
        await create_run(
            db_session,
            id='r-beta',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id='tenant-beta',
        )
        await create_run(
            db_session,
            id='r-legacy',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id=None,
        )

        scoped = await list_runs(db_session, tenant_id='tenant-alpha')
        ids = sorted(r.id for r in scoped)
        assert ids == ['r-alpha', 'r-legacy'], 'tenant-alpha sees own + legacy NULL-tenant rows; never tenant-beta'

    async def test_update_cross_tenant_returns_none(self, db_session: AsyncSession) -> None:
        await create_run(
            db_session,
            id='r-alpha',
            initiative='x',
            status='queued',
            started_at=_started_at(),
            tenant_id='tenant-alpha',
        )
        # Tenant-beta tries to mutate — refused.
        updated = await update_run(db_session, id='r-alpha', status='cancelled', tenant_id='tenant-beta')
        assert updated is None

        # Verify the original row was not mutated.
        row = await get_run(db_session, 'r-alpha')
        assert row is not None and row.status == 'queued'


# ─── HTTP layer — full request flow ─────────────────────────────────────


@pytest_asyncio.fixture
async def http_app(
    rsa_keypair: tuple[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    """Build a small FastAPI app that exercises catalog router + auth middleware.

    The auth middleware is wired with ``required=true`` so unauthenticated
    requests return 401 (matching production). A stub JWKSCache short-circuits
    the network fetch — tokens are minted against the same keypair the cache
    publishes.
    """
    _, public_jwk = rsa_keypair
    settings = AuthSettings(
        issuer=ISSUER,
        audience=AUDIENCE,
        required=True,
        system_tenant_id=SYSTEM_TENANT,
    )
    cache = _StubJWKSCache(settings, [public_jwk])

    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, settings=settings, jwks_cache=cache)
    app.include_router(catalog_router, prefix='/initiatives/catalog')

    # Enable the DB so the catalog router accepts requests (otherwise 503).
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield app

    await db_module.dispose_engine()
    db_module._reset_for_tests()


@pytest.fixture
def http_client(http_app: FastAPI) -> Iterator[TestClient]:
    """Synchronous TestClient over the HTTP fixture app."""
    with TestClient(http_app) as client:
        yield client


def _yaml_for(name: str) -> str:
    """Compose a valid initiative YAML whose ``name:`` matches the catalog key."""
    return (
        f'name: {name}\nrepo: leartech-test\nbranch: agent/test\nbase: main\ngoal: HTTP-layer tenant isolation test.\n'
    )


def _auth(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}'}


def test_http_create_then_list_isolates_by_tenant(
    http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]
) -> None:
    """End-to-end: tenant A creates → tenant B's list doesn't see it; tenant A's does."""
    private_pem, _ = rsa_keypair
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    beta = _mint(private_pem, tenant_id='tenant-beta')

    # Alpha creates a tenant-scoped initiative.
    resp = http_client.post(
        '/initiatives/catalog',
        json={'name': 'alpha-private', 'yaml_body': _yaml_for('alpha-private')},
        headers=_auth(alpha),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()['tenant_id'] == 'tenant-alpha'

    # Alpha's list includes it; Beta's list excludes it.
    alpha_list = http_client.get('/initiatives/catalog', headers=_auth(alpha))
    assert alpha_list.status_code == 200
    assert any(r['name'] == 'alpha-private' for r in alpha_list.json())

    beta_list = http_client.get('/initiatives/catalog', headers=_auth(beta))
    assert beta_list.status_code == 200
    assert not any(r['name'] == 'alpha-private' for r in beta_list.json()), (
        'tenant-beta must NEVER see tenant-alpha private rows'
    )


def test_http_cross_tenant_get_returns_404_not_403(
    http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]
) -> None:
    """Tenant B looking up Tenant A's row gets 404 — leaking existence is the same harm as content."""
    private_pem, _ = rsa_keypair
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    beta = _mint(private_pem, tenant_id='tenant-beta')

    http_client.post(
        '/initiatives/catalog',
        json={'name': 'secret-alpha', 'yaml_body': _yaml_for('secret-alpha')},
        headers=_auth(alpha),
    )

    # Tenant-beta sees 404 (not 403). 403 would confirm the row exists.
    cross = http_client.get('/initiatives/catalog/secret-alpha', headers=_auth(beta))
    assert cross.status_code == 404, f'Cross-tenant must be 404, not {cross.status_code} — 403 leaks existence'

    # Tenant-alpha gets it.
    own = http_client.get('/initiatives/catalog/secret-alpha', headers=_auth(alpha))
    assert own.status_code == 200


async def test_http_global_initiative_visible_to_every_tenant(
    http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]
) -> None:
    """A global (NULL-tenant) initiative is visible from every tenant.

    True "global" initiatives are rows with ``tenant_id IS NULL`` —
    created either via filesystem seed (no auth context) or by the CRUD
    layer directly. The system tenant's POSTs through the endpoint carry
    SYSTEM_TENANT as their tenant_id (not NULL) so they belong to the
    system tenant's own library, not to the global pool. This test seeds
    via the CRUD layer to mirror the filesystem-seed path.
    """
    private_pem, _ = rsa_keypair
    system = _mint(private_pem, tenant_id=SYSTEM_TENANT)
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    beta = _mint(private_pem, tenant_id='tenant-beta')

    # Seed a NULL-tenant row directly through the CRUD layer using the
    # same DB engine the TestClient is hitting.
    async with db_module.session() as sess:
        await create_initiative(sess, name='global-shared', yaml_body=_yaml_for('global-shared'))

    for token in (alpha, beta, system):
        resp = http_client.get('/initiatives/catalog/global-shared', headers=_auth(token))
        assert resp.status_code == 200, f'Global initiative must be visible to every tenant; got {resp.status_code}'
        assert resp.json()['tenant_id'] is None


def test_http_tenant_relay_header_takes_effect_for_system_tenant(
    http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]
) -> None:
    """System-tenant + X-Tenant-Id header → effective tenant_id is from the header.

    This is how the Orchestrator (a system-tenant service) makes
    on-behalf-of-a-tenant calls without minting a fresh token per tenant.
    """
    private_pem, _ = rsa_keypair
    system_token = _mint(private_pem, tenant_id=SYSTEM_TENANT)

    # System tenant on behalf of tenant-gamma creates a row.
    resp = http_client.post(
        '/initiatives/catalog',
        json={'name': 'gamma-by-relay', 'yaml_body': _yaml_for('gamma-by-relay')},
        headers={**_auth(system_token), TENANT_RELAY_HEADER: 'tenant-gamma'},
    )
    assert resp.status_code == 201
    assert resp.json()['tenant_id'] == 'tenant-gamma', (
        'X-Tenant-Id header from a system-tenant call must drive the row tenant'
    )

    # A regular tenant CANNOT forge the header — the relay only fires for
    # system-tenant claims. Send a tenant-alpha token claiming
    # X-Tenant-Id: tenant-beta and verify the row gets stamped tenant-alpha
    # (the claim wins, the header is ignored).
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    resp2 = http_client.post(
        '/initiatives/catalog',
        json={'name': 'forged-attempt', 'yaml_body': _yaml_for('forged-attempt')},
        headers={**_auth(alpha), TENANT_RELAY_HEADER: 'tenant-beta'},
    )
    assert resp2.status_code == 201
    assert resp2.json()['tenant_id'] == 'tenant-alpha', (
        'Non-system tenant must NOT be able to forge X-Tenant-Id to elevate or hop tenants'
    )


def test_http_cross_tenant_update_returns_404(http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Mutating a row you do not own returns 404."""
    private_pem, _ = rsa_keypair
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    beta = _mint(private_pem, tenant_id='tenant-beta')

    http_client.post(
        '/initiatives/catalog',
        json={'name': 'alpha-row', 'yaml_body': _yaml_for('alpha-row')},
        headers=_auth(alpha),
    )

    resp = http_client.put(
        '/initiatives/catalog/alpha-row',
        json={'description': 'hijacked by beta'},
        headers=_auth(beta),
    )
    assert resp.status_code == 404, 'Cross-tenant update must 404, not 403'

    # Confirm the row was not mutated.
    own = http_client.get('/initiatives/catalog/alpha-row', headers=_auth(alpha))
    assert own.json().get('description') in (None, '')


def test_http_cross_tenant_delete_returns_404(http_client: TestClient, rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    private_pem, _ = rsa_keypair
    alpha = _mint(private_pem, tenant_id='tenant-alpha')
    beta = _mint(private_pem, tenant_id='tenant-beta')

    http_client.post(
        '/initiatives/catalog',
        json={'name': 'alpha-deleteable', 'yaml_body': _yaml_for('alpha-deleteable')},
        headers=_auth(alpha),
    )

    resp = http_client.delete('/initiatives/catalog/alpha-deleteable', headers=_auth(beta))
    assert resp.status_code == 404

    # The row is still there for tenant-alpha.
    own = http_client.get('/initiatives/catalog/alpha-deleteable', headers=_auth(alpha))
    assert own.status_code == 200


# ─── state.py — register / get / list_records with tenant_id ────────────


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv(db_module.DSN_ENV, 'sqlite+aiosqlite:///:memory:')
    db_module._reset_for_tests()
    state_module._records.clear()

    engine = db_module.init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await db_module.dispose_engine()
    db_module._reset_for_tests()
    state_module._records.clear()


async def test_state_register_persists_tenant_id_and_get_filters(db_enabled: None) -> None:
    """state.register stamps tenant_id; state.get filters cross-tenant access."""
    _ = db_enabled
    rec = InitiativeRecord(
        id=new_id(),
        initiative='x',
        status='queued',
        started_at=datetime.now(UTC),
        runtime='job',
        tenant_id='tenant-alpha',
    )
    await register(rec)

    # Same tenant — sees it.
    assert (await get(rec.id, tenant_id='tenant-alpha')) is not None

    # Cross tenant — None (router maps to 404).
    assert (await get(rec.id, tenant_id='tenant-beta')) is None

    # System tenant / unauthenticated context — sees everything.
    assert (await get(rec.id)) is not None


async def test_state_list_records_scopes_by_tenant(db_enabled: None) -> None:
    _ = db_enabled
    alpha_id = new_id()
    beta_id = new_id()
    await register(
        InitiativeRecord(
            id=alpha_id,
            initiative='x',
            status='queued',
            started_at=datetime.now(UTC),
            runtime='job',
            tenant_id='tenant-alpha',
        )
    )
    await register(
        InitiativeRecord(
            id=beta_id,
            initiative='y',
            status='queued',
            started_at=datetime.now(UTC),
            runtime='job',
            tenant_id='tenant-beta',
        )
    )

    alpha_view = await list_records(tenant_id='tenant-alpha')
    alpha_ids = {r.id for r in alpha_view}
    assert alpha_id in alpha_ids
    assert beta_id not in alpha_ids

    everything = await list_records()
    all_ids = {r.id for r in everything}
    assert {alpha_id, beta_id}.issubset(all_ids)


async def test_state_update_cross_tenant_is_noop(db_enabled: None) -> None:
    _ = db_enabled
    run_id = new_id()
    await register(
        InitiativeRecord(
            id=run_id,
            initiative='x',
            status='queued',
            started_at=datetime.now(UTC),
            runtime='job',
            tenant_id='tenant-alpha',
        )
    )
    # Tenant-beta tries to update — silently no-op (DB layer returns None;
    # in-memory layer is guarded too).
    await state_update(run_id, tenant_id='tenant-beta', status='cancelled')

    # The original status is preserved when read by the owning tenant.
    rec = await get(run_id, tenant_id='tenant-alpha')
    assert rec is not None and rec.status == 'queued', 'Cross-tenant update must NOT mutate the row'
