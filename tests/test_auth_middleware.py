"""Tests for the OIDC bearer-token middleware (v7-P1 step 2).

Mirrors the orchestrator step 1 suite shape:

- Valid token + correct audience → 200; tenant_id on request.state
- Wrong audience (Orch's instead of agent's) → 401
- Missing bearer → 401 (when required=true) / 200 (when required=false)
- Expired bearer → 401
- Wrong issuer → 401
- Public endpoints bypass (/healthz, /openapi.json, /docs, /.well-known/*)
- JWKS cache works (single fetch reused across multiple requests)

Tests construct local FastAPI apps with deterministic settings + a stubbed
:class:`JWKSCache`, so the global ``app.main.app`` (which reads from env at
import time) doesn't bleed into the assertions.
"""

from __future__ import annotations

import time
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from jose import jwk, jwt

from app.auth.middleware import (
    AUTH_AUDIENCE_ENV,
    AUTH_ISSUER_ENV,
    AUTH_JWKS_TTL_ENV,
    AUTH_REQUIRED_ENV,
    AUTH_S2S_SCOPE_ENV,
    AUTH_TENANT_CLAIM_ENV,
    DEFAULT_S2S_SCOPE,
    AuthenticationMiddleware,
    AuthSettings,
    JWKSCache,
    _token_scopes,
    get_current_tenant_id,
    get_current_user,
    load_settings_from_env,
    require_service_caller,
    require_user_caller,
)

ISSUER = 'https://hydra.staging.leartech.com'
AGENT_AUDIENCE = 'https://leartech-automated-agent-jx-staging.jx.leartech.com'
ORCH_AUDIENCE = 'https://leartech-orchestrator-jx-staging.jx.leartech.com'
KID = 'test-key-1'
ALG = 'RS256'


# ─── Crypto helpers ──────────────────────────────────────────────────────────


@pytest.fixture(scope='module')
def rsa_keypair() -> tuple[str, dict[str, Any]]:
    """Return (private_key_pem, jwk_public_dict) — generated once per module."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
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


def _mint_token(
    private_pem: str,
    *,
    audience: str = AGENT_AUDIENCE,
    issuer: str = ISSUER,
    tenant_id: str | None = 'tenant-alpha',
    subject: str = 'user-123',
    expires_in: int = 3600,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint an RS256-signed JWT with the agent-audience by default."""
    now = int(time.time())
    claims: dict[str, Any] = {
        'iss': issuer,
        'aud': audience,
        'sub': subject,
        'iat': now,
        'exp': now + expires_in,
    }
    if tenant_id is not None:
        # Hydra nests custom access-token claims under `ext`; the middleware
        # reads `ext.tenant_id` (matching the orchestrator + the real IdP).
        claims['ext'] = {'tenant_id': tenant_id}
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_pem, algorithm=ALG, headers={'kid': KID})


# ─── App / cache helpers ─────────────────────────────────────────────────────


class _StubJWKSCache(JWKSCache):
    """JWKSCache subclass that returns a fixed in-memory JWKS and counts fetches."""

    def __init__(self, settings: AuthSettings, keys: list[dict[str, Any]]) -> None:
        super().__init__(settings)
        self._keys = keys
        self.fetch_calls = 0

    def fetch_jwks(self) -> list[dict[str, Any]]:
        self.fetch_calls += 1
        return list(self._keys)


def _build_app(
    settings: AuthSettings, jwks_cache: JWKSCache | None = None
) -> tuple[FastAPI, _StubJWKSCache | JWKSCache]:
    app = FastAPI()
    cache = jwks_cache if jwks_cache is not None else JWKSCache(settings)
    app.add_middleware(AuthenticationMiddleware, settings=settings, jwks_cache=cache)

    @app.get('/health')
    async def health() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/healthz')
    async def healthz() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/openapi.json')
    async def openapi_doc() -> dict[str, str]:
        return {'openapi': '3.0.0'}

    @app.get('/.well-known/jwks.json')
    async def jwks_doc() -> dict[str, list[Any]]:
        return {'keys': []}

    @app.get('/initiatives')
    async def initiatives(request: Request) -> dict[str, Any]:
        return {
            'tenant_id': get_current_tenant_id(request),
            'user_subject': (u.subject if (u := get_current_user(request)) else None),
        }

    return app, cache


def _required_settings() -> AuthSettings:
    return AuthSettings(issuer=ISSUER, audience=AGENT_AUDIENCE, required=True)


def _optional_settings() -> AuthSettings:
    return AuthSettings(issuer=ISSUER, audience=AGENT_AUDIENCE, required=False)


# ─── load_settings_from_env ──────────────────────────────────────────────────


def test_load_settings_from_env_defaults_to_required_and_raises_without_issuer_audience() -> None:
    """Auth-hardening C1: empty env → required=true (fail-closed) → raise.

    An operator who deploys the chart without issuer/audience configured
    gets a startup RuntimeError instead of a silently-unauthenticated pod.
    """
    with pytest.raises(RuntimeError, match=AUTH_ISSUER_ENV):
        load_settings_from_env(env={})


def test_load_settings_from_env_explicit_disabled_returns_defaults() -> None:
    """Explicit opt-out (LEARTECH_AUTH_REQUIRED=false) keeps the pre-C1 shape.

    Local/dev/CI runs still work: middleware boots in optional mode with
    empty issuer/audience — same behaviour the test suite's conftest.py
    relies on to keep TestClient(app) constructions passing.
    """
    settings = load_settings_from_env(env={AUTH_REQUIRED_ENV: 'false'})
    assert settings.required is False
    assert settings.issuer == ''
    assert settings.audience == ''
    assert settings.tenant_claim == 'ext.tenant_id'
    assert settings.jwks_ttl_seconds == 300


def test_load_settings_from_env_reads_all_fields() -> None:
    settings = load_settings_from_env(
        env={
            AUTH_ISSUER_ENV: ISSUER,
            AUTH_AUDIENCE_ENV: AGENT_AUDIENCE,
            AUTH_REQUIRED_ENV: 'true',
            AUTH_TENANT_CLAIM_ENV: 'org_id',
            AUTH_JWKS_TTL_ENV: '900',
        }
    )
    assert settings.required is True
    assert settings.issuer == ISSUER
    assert settings.audience == AGENT_AUDIENCE
    assert settings.tenant_claim == 'org_id'
    assert settings.jwks_ttl_seconds == 900


def test_load_settings_from_env_required_without_issuer_raises() -> None:
    with pytest.raises(RuntimeError, match='LEARTECH_AUTH_ISSUER'):
        load_settings_from_env(env={AUTH_REQUIRED_ENV: 'true'})


def test_load_settings_from_env_required_without_audience_raises() -> None:
    with pytest.raises(RuntimeError, match='LEARTECH_AUTH_AUDIENCE'):
        load_settings_from_env(env={AUTH_REQUIRED_ENV: 'true', AUTH_ISSUER_ENV: ISSUER})


def test_load_settings_from_env_default_required_without_issuer_raises() -> None:
    """Even without an explicit LEARTECH_AUTH_REQUIRED, missing issuer/audience raises.

    Auth-hardening C1 guardrail: the default is fail-closed. Only an explicit
    opt-out flips it to false.
    """
    with pytest.raises(RuntimeError, match=AUTH_ISSUER_ENV):
        load_settings_from_env(env={AUTH_AUDIENCE_ENV: AGENT_AUDIENCE})


def test_load_settings_from_env_jwks_url_uses_issuer_well_known() -> None:
    settings = load_settings_from_env(env={AUTH_REQUIRED_ENV: 'false', AUTH_ISSUER_ENV: 'https://hydra/'})
    assert settings.jwks_url == 'https://hydra/.well-known/jwks.json'


def test_load_settings_from_env_invalid_ttl_falls_back_to_default() -> None:
    settings = load_settings_from_env(env={AUTH_REQUIRED_ENV: 'false', AUTH_JWKS_TTL_ENV: 'banana'})
    assert settings.jwks_ttl_seconds == 300


# ─── Happy path: valid token, correct audience ───────────────────────────────


def test_valid_token_proceeds_and_attaches_tenant_id(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, tenant_id='tenant-beta')

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json() == {'tenant_id': 'tenant-beta', 'user_subject': 'user-123'}


def test_token_without_tenant_claim_proceeds_with_none_tenant(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, tenant_id=None)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json() == {'tenant_id': None, 'user_subject': 'user-123'}


def test_custom_tenant_claim_is_read(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = AuthSettings(issuer=ISSUER, audience=AGENT_AUDIENCE, required=True, tenant_claim='org_id')
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, tenant_id=None, extra_claims={'org_id': 'tenant-gamma'})

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json()['tenant_id'] == 'tenant-gamma'


# ─── Audience mismatch — the marquee test ────────────────────────────────────


def test_orchestrator_audience_is_rejected(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """A token minted for the orchestrator MUST NOT be accepted by the agent."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, audience=ORCH_AUDIENCE)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401
    assert 'auth' in response.json()['detail'].lower()


# ─── Missing / malformed / expired / wrong-issuer ────────────────────────────


def test_missing_bearer_returns_401_when_required() -> None:
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get('/initiatives')

    assert response.status_code == 401
    assert response.json()['detail'] == 'missing bearer token'


def test_non_bearer_authorization_returns_401_when_required() -> None:
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': 'Basic dXNlcjpwYXNz'})

    assert response.status_code == 401


def test_malformed_token_returns_401(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [rsa_keypair[1]])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': 'Bearer not.a.jwt'})

    assert response.status_code == 401


def test_expired_token_returns_401(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, expires_in=-60)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401
    assert 'auth' in response.json()['detail'].lower()


def test_wrong_issuer_token_returns_401(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, issuer='https://attacker.example.com')

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401


def test_unknown_kid_returns_401(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Token signed with kid that isn't in JWKS even after refetch."""
    private_pem, public_jwk = rsa_keypair
    rogue_jwk = dict(public_jwk, kid='other-key')
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [rogue_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401
    assert 'kid' in response.json()['detail']


# ─── Bypass list ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    'path',
    [
        '/health',
        '/healthz',
        '/openapi.json',
        '/.well-known/jwks.json',
    ],
)
def test_bypass_paths_skip_auth(path: str) -> None:
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    # Cache never consulted on bypass paths
    assert cache.fetch_calls == 0


# ─── Optional mode ───────────────────────────────────────────────────────────


def test_optional_mode_allows_missing_token() -> None:
    settings = _optional_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get('/initiatives')

    assert response.status_code == 200
    assert response.json() == {'tenant_id': None, 'user_subject': None}


def test_optional_mode_allows_invalid_token() -> None:
    settings = _optional_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_app(settings, cache)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': 'Bearer garbage'})

    assert response.status_code == 200
    assert response.json()['tenant_id'] is None


def test_optional_mode_still_attaches_tenant_when_token_valid(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    private_pem, public_jwk = rsa_keypair
    settings = _optional_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, tenant_id='tenant-delta')

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json()['tenant_id'] == 'tenant-delta'


# ─── Validation edge branches ───────────────────────────────────────────────


def test_optional_mode_with_unset_issuer_proceeds_anonymously_on_token(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    """When issuer/audience are blank (optional mode bootstrap) the middleware
    can't validate — it must NOT 500 nor accept the bearer; it must degrade
    to anonymous. Guards the ``empty issuer / empty audience`` branch in
    ``_validate_token`` that's only reachable through the optional path.
    """
    private_pem, public_jwk = rsa_keypair
    settings = AuthSettings(issuer='', audience='', required=False)
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json()['tenant_id'] is None


def test_token_with_no_kid_header_returns_401(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Tokens whose JWS header lacks ``kid`` are rejected — we can't look up the key."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    now = int(time.time())
    # Hand-mint a token WITHOUT a kid header (jose's encode omits headers={} arg).
    token = jwt.encode(
        {
            'iss': ISSUER,
            'aud': AGENT_AUDIENCE,
            'sub': 'user-456',
            'iat': now,
            'exp': now + 3600,
        },
        private_pem,
        algorithm=ALG,
    )

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401
    assert 'kid' in response.json()['detail']


# ─── JWKS cache behaviour ────────────────────────────────────────────────────


def test_jwks_cache_reuses_fetch_within_ttl(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """N requests within TTL → JWKS fetched once."""
    private_pem, public_jwk = rsa_keypair
    settings = AuthSettings(issuer=ISSUER, audience=AGENT_AUDIENCE, required=True, jwks_ttl_seconds=60)
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        for _ in range(5):
            response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})
            assert response.status_code == 200

    assert cache.fetch_calls == 1


def test_jwks_cache_invalidates_on_kid_miss(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Single retry-fetch when the kid isn't in the cached JWKS."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [dict(public_jwk, kid='other')])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401
    assert cache.fetch_calls == 2  # initial + retry-after-invalidate


def test_jwks_cache_refetches_after_ttl(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Once TTL elapses, the next request triggers a fresh fetch."""
    private_pem, public_jwk = rsa_keypair
    # Cache returns the key once, then a refetch returns the same key —
    # we just care that the fetch counter increments after TTL.
    settings = AuthSettings(issuer=ISSUER, audience=AGENT_AUDIENCE, required=True, jwks_ttl_seconds=0)
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        for _ in range(3):
            response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})
            assert response.status_code == 200

    assert cache.fetch_calls == 3


# ─── JWKS fetch error paths (added in response to AI-review red finding) ─────


class _ExplodingJWKSCache(JWKSCache):
    """JWKSCache that raises a configured exception on every fetch.

    Used to exercise the 503 error branches in :meth:`JWKSCache.fetch_jwks`
    end-to-end through the middleware. The real default implementation hits
    the network via httpx and raises :class:`fastapi.HTTPException` 503 when
    the upstream JWKS is unreachable or malformed; we mirror that contract
    here without binding the tests to a particular httpx version.
    """

    def __init__(self, settings: AuthSettings, exc: Exception) -> None:
        super().__init__(settings)
        self._exc = exc

    def fetch_jwks(self) -> list[dict[str, Any]]:
        raise self._exc


def test_jwks_fetch_network_failure_surfaces_503_when_required(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    """An httpx connect error during JWKS fetch surfaces as 503 (not 500)."""
    from fastapi import HTTPException as _HTTPException

    private_pem, _ = rsa_keypair
    settings = _required_settings()
    cache = _ExplodingJWKSCache(
        settings,
        _HTTPException(status_code=503, detail='auth: JWKS fetch failed against ...: connect timed out'),
    )
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    # required=true → middleware converts the HTTPException to a JSON response with
    # the same status_code (503) and detail. Anything 5xx here would mean the
    # error escaped through the ASGI layer untyped — that's the regression we guard.
    assert response.status_code == 503
    assert 'JWKS' in response.json()['detail']


def test_jwks_fetch_network_failure_degrades_to_anonymous_when_optional(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    """Optional mode + JWKS fetch error → proceed anonymously (no 5xx)."""
    from fastapi import HTTPException as _HTTPException

    private_pem, _ = rsa_keypair
    settings = _optional_settings()
    cache = _ExplodingJWKSCache(
        settings,
        _HTTPException(status_code=503, detail='auth: JWKS fetch failed against ...: connect timed out'),
    )
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json()['tenant_id'] is None


def test_jwks_fetch_http_error_via_real_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real :class:`JWKSCache.fetch_jwks` raises 503 on httpx.HTTPError.

    Uses ``monkeypatch`` to replace ``httpx.get`` with a stub that raises a
    real :class:`httpx.ConnectError`, then asserts the cache converts it
    into a :class:`fastapi.HTTPException` with status_code 503 and a
    diagnostic detail that mentions the JWKS URL.
    """
    import httpx as _httpx
    from fastapi import HTTPException as _HTTPException

    settings = _required_settings()
    cache = JWKSCache(settings)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise _httpx.ConnectError('simulated connect failure')

    monkeypatch.setattr(_httpx, 'get', _boom)

    with pytest.raises(_HTTPException) as excinfo:
        cache.fetch_jwks()
    assert excinfo.value.status_code == 503
    assert 'JWKS fetch failed' in str(excinfo.value.detail)
    assert settings.jwks_url in str(excinfo.value.detail)


def test_jwks_fetch_invalid_json_via_real_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real :class:`JWKSCache.fetch_jwks` raises 503 when the JWKS body isn't JSON."""
    import httpx as _httpx
    from fastapi import HTTPException as _HTTPException

    class _StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            raise ValueError('not json')

    settings = _required_settings()
    cache = JWKSCache(settings)
    monkeypatch.setattr(_httpx, 'get', lambda *a, **kw: _StubResponse())

    with pytest.raises(_HTTPException) as excinfo:
        cache.fetch_jwks()
    assert excinfo.value.status_code == 503


def test_jwks_fetch_missing_keys_array_via_real_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real :class:`JWKSCache.fetch_jwks` raises 503 when the body lacks `keys`."""
    import httpx as _httpx
    from fastapi import HTTPException as _HTTPException

    class _StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {'issuer': 'https://hydra/'}  # no `keys` field

    settings = _required_settings()
    cache = JWKSCache(settings)
    monkeypatch.setattr(_httpx, 'get', lambda *a, **kw: _StubResponse())

    with pytest.raises(_HTTPException) as excinfo:
        cache.fetch_jwks()
    assert excinfo.value.status_code == 503
    assert 'missing `keys`' in str(excinfo.value.detail)


def test_jwks_fetch_uses_injected_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor-injected ``http_client`` is used in preference to module-level httpx.get."""
    import httpx as _httpx

    class _StubClient:
        calls: int = 0

        def get(self, url: str, timeout: float) -> Any:  # noqa: ARG002 — signature mirror
            type(self).calls += 1
            return _StubResponse()

    class _StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {'keys': [{'kid': 'test-1'}]}

    # Trip the module-level httpx.get to make absolutely sure it's not used.
    def _no_module_get(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError('module-level httpx.get must not be called when http_client is injected')

    monkeypatch.setattr(_httpx, 'get', _no_module_get)
    settings = _required_settings()
    stub = _StubClient()
    cache = JWKSCache(settings, http_client=cast(_httpx.Client, stub))

    keys = cache.fetch_jwks()
    assert keys == [{'kid': 'test-1'}]
    assert _StubClient.calls == 1


# ─── Config-driven required-scope gate (s2s) ─────────────────────────────────


def _scoped_settings() -> AuthSettings:
    """required=True + a required scope (the s2s enforcement config)."""
    return AuthSettings(
        issuer=ISSUER,
        audience=AGENT_AUDIENCE,
        required=True,
        required_scopes=frozenset({'leartechapi.internal_services'}),
    )


def test_missing_required_scope_rejected_403(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Valid signature/issuer/audience but WITHOUT the required scope → 403."""
    private_pem, public_jwk = rsa_keypair
    settings = _scoped_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)  # no scope claim

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 403
    assert 'internal_services' in response.json()['detail']


def test_required_scope_present_proceeds(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Token carrying the required scope (Hydra 'scope' string) → 200."""
    private_pem, public_jwk = rsa_keypair
    settings = _scoped_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem, extra_claims={'scope': 'openid leartechapi.internal_services'})

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200


def test_no_required_scope_config_allows_any_valid_token(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """Empty required_scope = no gate (back-compat): a valid token without any scope proceeds."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()  # required_scope defaults to ''
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)
    token = _mint_token(private_pem)

    with TestClient(app) as client:
        response = client.get('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200


def test_token_scopes_reads_scope_string_and_scp_list() -> None:
    assert _token_scopes({'scope': 'a b c'}) == {'a', 'b', 'c'}
    assert _token_scopes({'scp': ['x', 'y']}) == {'x', 'y'}
    assert _token_scopes({}) == set()


def test_multiple_required_scopes_any_of(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """LEARTECH_AUTH_REQUIRED_SCOPES with >1 scope → any-of: the token needs AT LEAST
    ONE of them (a caller is one type). None present → 403."""
    private_pem, public_jwk = rsa_keypair
    settings = AuthSettings(
        issuer=ISSUER,
        audience=AGENT_AUDIENCE,
        required=True,
        required_scopes=frozenset({'leartechapi.internal_services', 'leartechapi.extra'}),
    )
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_app(settings, cache)

    # one of the two accepted types → 200 (any-of)
    one = _mint_token(private_pem, extra_claims={'scope': 'leartechapi.internal_services'})
    # neither → 403
    none_ = _mint_token(private_pem, extra_claims={'scope': 'openid'})

    with TestClient(app) as client:
        assert client.get('/initiatives', headers={'Authorization': f'Bearer {one}'}).status_code == 200
        assert client.get('/initiatives', headers={'Authorization': f'Bearer {none_}'}).status_code == 403


# ─── Per-user vs s2s route split (auth-hardening C1) ─────────────────────────
#
# The middleware validates that ANY caller is authenticated (issuer + audience
# + signature + optional required-scopes any-of gate). The split adds a
# per-endpoint check: some routes are s2s-only (reject bare user tokens),
# some are user-only (reject s2s tokens). Both use the presence of the
# ``leartechapi.internal_services`` scope as the classification signal.


def _build_split_app(settings: AuthSettings, jwks_cache: JWKSCache | None = None) -> tuple[FastAPI, JWKSCache]:
    """FastAPI app with three routes exercising the C1 split:

    - ``/initiatives``       — s2s-only (orch → agent shape)
    - ``/lessons``           — s2s-only (ring-2/3 writers)
    - ``/mcps``              — user-only (dashboard operators)
    - ``/initiatives/probe`` — unmarked (accepts either — proves opting-in
                               is required for enforcement)
    """
    from fastapi import Depends as _Depends  # local import to keep the top block tidy

    app = FastAPI()
    cache = jwks_cache if jwks_cache is not None else JWKSCache(settings)
    app.add_middleware(AuthenticationMiddleware, settings=settings, jwks_cache=cache)

    @app.get('/healthz')
    async def healthz() -> dict[str, str]:  # bypass path
        return {'status': 'ok'}

    @app.post('/initiatives')
    async def fire(user: Any = _Depends(require_service_caller)) -> dict[str, str | None]:
        # Optional-mode passthrough is signalled by user=None; handler
        # is defensive so the test app doesn't 500 in that leg.
        return {'sub': user.subject if user is not None else None}

    @app.post('/lessons')
    async def lessons(user: Any = _Depends(require_service_caller)) -> dict[str, str | None]:
        return {'sub': user.subject if user is not None else None}

    @app.post('/mcps')
    async def register_mcp(user: Any = _Depends(require_user_caller)) -> dict[str, str | None]:
        return {'sub': user.subject if user is not None else None}

    @app.get('/initiatives/probe')
    async def probe(request: Request) -> dict[str, Any]:
        return {'subject': (u.subject if (u := get_current_user(request)) else None)}

    return app, cache


def test_s2s_route_accepts_service_token(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """A token carrying the ``leartechapi.internal_services`` scope passes
    ``require_service_caller``.
    """
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    token = _mint_token(private_pem, extra_claims={'scope': DEFAULT_S2S_SCOPE})

    with TestClient(app) as client:
        response = client.post('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200
    assert response.json() == {'sub': 'user-123'}


def test_s2s_route_rejects_bare_user_token(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """A valid audience+issuer token WITHOUT the s2s scope is rejected 403
    on s2s-only routes. This is the marquee test for C1's route split —
    a compromised user session cannot fire the orch-only path.
    """
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    token = _mint_token(private_pem)  # no scope claim

    with TestClient(app) as client:
        response = client.post('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 403
    assert DEFAULT_S2S_SCOPE in response.json()['detail']


def test_user_route_accepts_bare_user_token(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """A valid user token (no s2s scope) passes ``require_user_caller``."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    token = _mint_token(private_pem)  # user token — no scope

    with TestClient(app) as client:
        response = client.post('/mcps', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 200


def test_user_route_rejects_service_token(rsa_keypair: tuple[str, dict[str, Any]]) -> None:
    """An s2s token is rejected 403 on user-only routes.

    A compromised service-to-service token cannot drive human-only admin
    surfaces (MCP catalog edits, dashboard mutations). The split is
    bidirectional — that's what makes the marking meaningful.
    """
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    token = _mint_token(private_pem, extra_claims={'scope': DEFAULT_S2S_SCOPE})

    with TestClient(app) as client:
        response = client.post('/mcps', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 403
    assert DEFAULT_S2S_SCOPE in response.json()['detail']


def test_split_dependency_uses_configured_scope_env(
    rsa_keypair: tuple[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cluster can rename the s2s scope via env; the split honours it."""
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    monkeypatch.setenv(AUTH_S2S_SCOPE_ENV, 'partner.tenant.services')
    good = _mint_token(private_pem, extra_claims={'scope': 'partner.tenant.services'})
    bad = _mint_token(private_pem, extra_claims={'scope': DEFAULT_S2S_SCOPE})

    with TestClient(app) as client:
        assert client.post('/initiatives', headers={'Authorization': f'Bearer {good}'}).status_code == 200
        # A token carrying only the OLD scope no longer counts as s2s
        assert client.post('/initiatives', headers={'Authorization': f'Bearer {bad}'}).status_code == 403


def test_split_dependency_passthrough_in_optional_mode() -> None:
    """In optional-mode + no bearer, the split dependencies are passthroughs.

    This preserves the pre-C1 test-suite shape (``TestClient(app)`` without
    minting bearers can still exercise every endpoint) while making the
    split fully enforce in every cluster the chart deploys to (all of
    which run required=true under C1).

    The alternative — enforcing 401 in optional-mode too — would break
    every existing ``TestClient(app)`` construction across the suite,
    which is a much larger surface than the marginal safety of "guards
    against a misconfigured cluster that runs required=false in prod".
    Production runs required=true; if that invariant breaks, other
    problems come first.
    """
    settings = _optional_settings()
    cache = _StubJWKSCache(settings, [])
    app, _ = _build_split_app(settings, cache)

    with TestClient(app) as client:
        response = client.post('/initiatives')

    assert response.status_code == 200


def test_orch_audience_still_rejected_on_split_routes(
    rsa_keypair: tuple[str, dict[str, Any]],
) -> None:
    """A token minted for the orchestrator (aud=orch), even carrying the s2s
    scope, MUST NOT pass the fire route: the audience check fires first.

    This is the "audience-bound to automated-agent" invariant — no
    substitution of a live orch token to reach the agent.
    """
    private_pem, public_jwk = rsa_keypair
    settings = _required_settings()
    cache = _StubJWKSCache(settings, [public_jwk])
    app, _ = _build_split_app(settings, cache)
    token = _mint_token(private_pem, audience=ORCH_AUDIENCE, extra_claims={'scope': DEFAULT_S2S_SCOPE})

    with TestClient(app) as client:
        response = client.post('/initiatives', headers={'Authorization': f'Bearer {token}'})

    assert response.status_code == 401  # audience rejected before the split dependency runs
