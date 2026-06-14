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
from typing import Any

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
    AUTH_TENANT_CLAIM_ENV,
    AuthenticationMiddleware,
    AuthSettings,
    JWKSCache,
    get_current_tenant_id,
    get_current_user,
    load_settings_from_env,
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
        claims['tenant_id'] = tenant_id
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


def test_load_settings_from_env_returns_defaults_when_unset() -> None:
    settings = load_settings_from_env(env={})
    assert settings.required is False
    assert settings.issuer == ''
    assert settings.audience == ''
    assert settings.tenant_claim == 'tenant_id'
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


def test_load_settings_from_env_jwks_url_uses_issuer_well_known() -> None:
    settings = load_settings_from_env(env={AUTH_ISSUER_ENV: 'https://hydra/'})
    assert settings.jwks_url == 'https://hydra/.well-known/jwks.json'


def test_load_settings_from_env_invalid_ttl_falls_back_to_default() -> None:
    settings = load_settings_from_env(env={AUTH_JWKS_TTL_ENV: 'banana'})
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
