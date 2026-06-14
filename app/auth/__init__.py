"""OIDC bearer-token validation + tenant claim extraction.

v7-P1 step 2 of 5 — companion to the orchestrator's middleware shipped in
step 1. Same Hydra issuer + JWKS endpoint, agent-specific audience.

The public surface:

- :class:`AuthSettings` — resolved config (issuer / audience / required) read
  from env. Loaded once at app-startup via :func:`load_settings_from_env`.
- :func:`install` — wires :class:`AuthenticationMiddleware` into a FastAPI app.
- :func:`get_current_tenant_id` / :func:`get_current_user` — request-scoped
  helpers callable from route handlers as ``Depends(...)`` or directly off
  ``request.state``.

Validation strategy (mirrors the leartech-orchestrator step-1 middleware):

1. Bypass list (``/healthz``, ``/health``, ``/readyz``, ``/openapi.json``,
   ``/docs``, ``/redoc``, ``/.well-known/*``) — these endpoints are
   probed by infrastructure that cannot present a bearer.
2. ``Authorization: Bearer <token>`` header is required for everything
   else when ``auth.required`` is true; missing header → 401.
3. Token is validated against the configured Hydra JWKS endpoint.
   :class:`JWKSCache` does a short-lived in-process cache (default 5 min)
   to avoid re-fetching the JWKS on every request — Hydra rotates keys
   infrequently and a stampede on every cold pod would be wasteful.
4. ``iss`` must match configured issuer; ``aud`` must match the agent's
   own audience (NOT the orchestrator's — different audience values are
   how a token issued for Orch is rejected when replayed at the agent).
5. ``tenant_id`` claim is extracted and attached to ``request.state`` so
   downstream handlers / DB queries can scope by tenant.

When ``auth.required`` is false (dev / preview / opt-out), the middleware
still attempts to parse a bearer if present (so request.state.tenant_id
gets populated when callers DO send one), but does NOT 401 on missing or
invalid tokens. Production clusters MUST set ``LEARTECH_AUTH_REQUIRED=true``.
"""

from __future__ import annotations

from app.auth.middleware import (
    TENANT_RELAY_HEADER,
    AuthenticationMiddleware,
    AuthSettings,
    JWKSCache,
    get_current_tenant_id,
    get_current_user,
    install,
    load_settings_from_env,
)

__all__ = [
    'TENANT_RELAY_HEADER',
    'AuthSettings',
    'AuthenticationMiddleware',
    'JWKSCache',
    'get_current_tenant_id',
    'get_current_user',
    'install',
    'load_settings_from_env',
]
