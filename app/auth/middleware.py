"""ASGI middleware that validates OIDC bearer tokens + extracts tenant_id.

See :mod:`app.auth` for the contract overview. This module is the
implementation: settings loader, JWKS cache, the middleware itself, and the
two request-scoped helper functions.

The middleware is registered AFTER FastAPI's exception handlers so that
:class:`fastapi.HTTPException` raised here surfaces as JSON via FastAPI's
default exception handler — not as a generic ASGI 500.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from fastapi import FastAPI, HTTPException, Request
from jose import jwt
from jose.exceptions import JWTError
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


# ─── Settings ────────────────────────────────────────────────────────────────


# Issuer is the Hydra public URL (shared with the orchestrator — same Hydra
# instance, same JWKS endpoint). Audience is agent-specific and MUST NOT
# overlap with the orchestrator's — that's how a token issued for Orch is
# rejected when replayed at the agent (and vice-versa). Per-cluster values:
#
#   gcp-staging: https://leartech-automated-agent-jx-staging.jx.leartech.com
#   az-staging:  https://leartech-automated-agent-jx-staging.az.leartech.com
#
# Helmfile / ExternalSecret renders these into the deployment env on each
# cluster. The agent reads them via ``LEARTECH_AUTH_*`` env vars at startup.
AUTH_ISSUER_ENV = 'LEARTECH_AUTH_ISSUER'
AUTH_AUDIENCE_ENV = 'LEARTECH_AUTH_AUDIENCE'
AUTH_REQUIRED_ENV = 'LEARTECH_AUTH_REQUIRED'
AUTH_JWKS_TTL_ENV = 'LEARTECH_AUTH_JWKS_TTL'
AUTH_TENANT_CLAIM_ENV = 'LEARTECH_AUTH_TENANT_CLAIM'

DEFAULT_TENANT_CLAIM = 'tenant_id'
DEFAULT_JWKS_TTL_SECONDS = 300  # 5 min — Hydra rotates keys infrequently.

# Paths that bypass the bearer requirement. Kept narrow on purpose: anything
# that can be probed by infra (health checks, OpenAPI clients) lives here;
# everything else gets the auth check. Add new entries only with reason.
_BYPASS_EXACT: frozenset[str] = frozenset(
    {
        '/health',
        '/healthz',
        '/readyz',
        '/openapi.json',
        '/docs',
        '/docs/oauth2-redirect',
        '/redoc',
    }
)
_BYPASS_PREFIX: tuple[str, ...] = ('/.well-known/',)


@dataclass(frozen=True)
class AuthSettings:
    """Resolved auth config — loaded once at startup and reused.

    Frozen so middleware / helpers can pass it around without worrying
    about callers mutating fields mid-request.
    """

    issuer: str
    audience: str
    required: bool
    tenant_claim: str = DEFAULT_TENANT_CLAIM
    jwks_ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS

    @property
    def jwks_url(self) -> str:
        """Hydra's JWKS URL — convention is ``<issuer>/.well-known/jwks.json``.

        Hydra exposes the OIDC discovery doc at ``<issuer>/.well-known/
        openid-configuration`` whose ``jwks_uri`` field is authoritative.
        Resolving that adds a second round-trip we'd otherwise cache; for
        now we use the well-known convention which Hydra honours. If a
        future Hydra config moves the JWKS URL we'll discover via the
        openid-config doc (a feature flag, not a breaking change).
        """
        return f'{self.issuer.rstrip("/")}/.well-known/jwks.json'


def load_settings_from_env(env: dict[str, str] | None = None) -> AuthSettings:
    """Resolve :class:`AuthSettings` from environment variables.

    ``env`` is exposed for tests (so they can construct settings without
    monkeypatching ``os.environ``); defaults to the live process env.

    Missing issuer or audience is tolerated when ``required=false`` (dev /
    preview) and surfaces empty strings — the middleware uses those to
    decide whether to attempt validation at all.
    """
    src = env if env is not None else dict(os.environ)
    issuer = src.get(AUTH_ISSUER_ENV, '').strip()
    audience = src.get(AUTH_AUDIENCE_ENV, '').strip()
    required = _parse_bool(src.get(AUTH_REQUIRED_ENV))
    tenant_claim = src.get(AUTH_TENANT_CLAIM_ENV, DEFAULT_TENANT_CLAIM).strip() or DEFAULT_TENANT_CLAIM
    raw_ttl = src.get(AUTH_JWKS_TTL_ENV)
    jwks_ttl_seconds = DEFAULT_JWKS_TTL_SECONDS
    if raw_ttl is not None:
        try:
            jwks_ttl_seconds = int(raw_ttl)
        except ValueError:
            logger.warning(
                'auth: %s=%r is not an int, falling back to %d',
                AUTH_JWKS_TTL_ENV,
                raw_ttl,
                DEFAULT_JWKS_TTL_SECONDS,
            )

    if required and (not issuer or not audience):
        raise RuntimeError(
            f'auth: {AUTH_REQUIRED_ENV}=true but {AUTH_ISSUER_ENV}/{AUTH_AUDIENCE_ENV} are not set. '
            'Refusing to start; either provide the issuer/audience or flip required to false.'
        )

    return AuthSettings(
        issuer=issuer,
        audience=audience,
        required=required,
        tenant_claim=tenant_claim,
        jwks_ttl_seconds=jwks_ttl_seconds,
    )


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {'true', '1', 'yes', 'on'}


# ─── JWKS cache ──────────────────────────────────────────────────────────────


@dataclass
class _CachedJWKS:
    fetched_at: float
    keys: list[dict[str, Any]] = field(default_factory=list)


class JWKSCache:
    """Caches the JWKS document for ``settings.jwks_ttl_seconds`` seconds.

    The cache is a single in-process slot — the JWKS itself is a small
    document and Hydra rotates keys infrequently, so we accept the
    occasional thundering-herd refetch when N concurrent requests miss
    a freshly-expired entry. Simpler than a lock-per-fetch.

    Override ``fetch_jwks`` in tests to inject a stub without monkeypatching
    httpx.
    """

    def __init__(self, settings: AuthSettings, http_client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._http_client = http_client
        self._slot: _CachedJWKS | None = None

    def fetch_jwks(self) -> list[dict[str, Any]]:
        """Fetch + return the JWKS ``keys`` list from Hydra.

        Override in tests to bypass the network. The default uses a
        short-lived httpx.Client (or the one injected at construction).
        """
        client = self._http_client
        try:
            if client is not None:
                response = client.get(self._settings.jwks_url, timeout=5.0)
            else:
                response = httpx.get(self._settings.jwks_url, timeout=5.0)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f'auth: JWKS fetch failed against {self._settings.jwks_url}: {exc}',
            ) from exc

        keys = body.get('keys')
        if not isinstance(keys, list):
            raise HTTPException(
                status_code=503,
                detail=f'auth: JWKS document at {self._settings.jwks_url} missing `keys` array',
            )
        return cast(list[dict[str, Any]], keys)

    def keys(self) -> list[dict[str, Any]]:
        """Return cached JWKS keys, refreshing if past TTL."""
        now = time.monotonic()
        slot = self._slot
        if slot is not None and (now - slot.fetched_at) < self._settings.jwks_ttl_seconds:
            return slot.keys
        keys = self.fetch_jwks()
        self._slot = _CachedJWKS(fetched_at=now, keys=keys)
        return keys

    def invalidate(self) -> None:
        """Drop the cached JWKS — call when a kid lookup misses, in case the
        JWKS rotated since the last fetch. Single retry on miss is the
        upstream pattern; we leave that policy to the middleware.
        """
        self._slot = None


# ─── Middleware ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AuthenticatedUser:
    """Lightweight user view exposed via ``request.state.user``.

    Carries the resolved subject + tenant_id plus the full claims dict for
    callers that need a non-standard claim (e.g. ``email``, ``roles``).
    """

    subject: str
    tenant_id: str | None
    claims: dict[str, Any]


def _should_bypass(path: str) -> bool:
    if path in _BYPASS_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _BYPASS_PREFIX)


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(' ', 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None
    token = parts[1].strip()
    return token or None


class AuthenticationMiddleware(BaseHTTPMiddleware):
    """ASGI middleware enforcing OIDC bearer validation.

    Behaviour matrix:

    +------------------+-------------+--------------------------+-----------------+
    | settings.required | path bypass | token present + valid    | response        |
    +==================+=============+==========================+=================+
    | true             | yes         | n/a                      | proceed         |
    +------------------+-------------+--------------------------+-----------------+
    | true             | no          | no                       | 401 missing     |
    +------------------+-------------+--------------------------+-----------------+
    | true             | no          | invalid                  | 401 invalid     |
    +------------------+-------------+--------------------------+-----------------+
    | true             | no          | yes                      | proceed; state  |
    +------------------+-------------+--------------------------+-----------------+
    | false            | any         | no                       | proceed; state  |
    |                  |             |                          | tenant=None     |
    +------------------+-------------+--------------------------+-----------------+
    | false            | any         | invalid                  | proceed; state  |
    |                  |             |                          | tenant=None     |
    +------------------+-------------+--------------------------+-----------------+
    | false            | any         | yes                      | proceed; state  |
    +------------------+-------------+--------------------------+-----------------+

    The "false + invalid → proceed" row is deliberate: dev / preview want
    the service to keep running even when a stale token is sent. Production
    clusters MUST set required=true so the 401 branch fires.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: AuthSettings,
        jwks_cache: JWKSCache | None = None,
    ) -> None:
        super().__init__(app)
        self._settings = settings
        self._jwks_cache = jwks_cache or JWKSCache(settings)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if _should_bypass(path):
            request.state.tenant_id = None
            request.state.user = None
            return await call_next(request)

        token = _extract_bearer(request.headers.get('authorization'))

        if token is None:
            if self._settings.required:
                return _unauthorized('missing bearer token')
            request.state.tenant_id = None
            request.state.user = None
            return await call_next(request)

        try:
            claims = self._validate_token(token)
        except HTTPException as exc:
            if self._settings.required:
                return _unauthorized(str(exc.detail), status_code=exc.status_code)
            logger.info('auth: token rejected on optional path (%s) — proceeding anonymously', exc.detail)
            request.state.tenant_id = None
            request.state.user = None
            return await call_next(request)

        tenant_value = claims.get(self._settings.tenant_claim)
        tenant_id: str | None = str(tenant_value) if tenant_value is not None else None
        subject = str(claims.get('sub', ''))
        request.state.tenant_id = tenant_id
        request.state.user = AuthenticatedUser(subject=subject, tenant_id=tenant_id, claims=claims)
        return await call_next(request)

    def _validate_token(self, token: str) -> dict[str, Any]:
        """Verify ``token`` and return the decoded claims dict.

        Raises :class:`fastapi.HTTPException` 401 on any validation failure.
        """
        if not self._settings.issuer or not self._settings.audience:
            # required=true was already enforced at startup; this branch is
            # only reachable in optional mode, in which case the caller has
            # opted into "best-effort" parsing. Treat missing config as
            # "can't validate" → fail closed at this layer.
            raise HTTPException(status_code=401, detail='auth: server-side issuer/audience not configured')

        try:
            headers = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise HTTPException(status_code=401, detail=f'auth: malformed token ({exc})') from exc

        kid = headers.get('kid')
        if not kid:
            raise HTTPException(status_code=401, detail='auth: token missing kid header')

        key = self._lookup_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[headers.get('alg', 'RS256')],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={'verify_at_hash': False},
            )
        except JWTError as exc:
            raise HTTPException(status_code=401, detail=f'auth: invalid token ({exc})') from exc
        return cast(dict[str, Any], claims)

    def _lookup_key(self, kid: str) -> dict[str, Any]:
        for entry in self._jwks_cache.keys():
            if entry.get('kid') == kid:
                return entry
        # Single retry on miss — covers the case where Hydra rotated keys
        # since our last fetch.
        self._jwks_cache.invalidate()
        for entry in self._jwks_cache.keys():
            if entry.get('kid') == kid:
                return entry
        raise HTTPException(status_code=401, detail=f'auth: signing key with kid={kid!r} not found in JWKS')


def _unauthorized(detail: str, status_code: int = 401) -> JSONResponse:
    """Build the 401 response body. Matches FastAPI's default exception shape."""
    return JSONResponse(status_code=status_code, content={'detail': detail})


# ─── Public app wiring ───────────────────────────────────────────────────────


def install(app: FastAPI, settings: AuthSettings | None = None) -> AuthSettings:
    """Wire :class:`AuthenticationMiddleware` into ``app`` and return the settings.

    Loads from env when ``settings`` is not provided. Returns the resolved
    settings so the caller can log them at startup.
    """
    resolved = settings if settings is not None else load_settings_from_env()
    app.add_middleware(AuthenticationMiddleware, settings=resolved)
    return resolved


# ─── Request-scoped helpers (callable directly OR via Depends) ───────────────


def get_current_tenant_id(request: Request) -> str | None:
    """Return the tenant_id attached by the middleware, or None when unauthenticated."""
    return cast(str | None, getattr(request.state, 'tenant_id', None))


def get_current_user(request: Request) -> AuthenticatedUser | None:
    """Return the :class:`AuthenticatedUser` attached by the middleware, or None."""
    return cast(AuthenticatedUser | None, getattr(request.state, 'user', None))
