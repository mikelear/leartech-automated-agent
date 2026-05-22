"""Database session factory + connection lifecycle for Postgres-backed catalogues.

Reads the DSN from `LEARTECH_INITIATIVE_DB_DSN` (env var). When unset, the
agent runs in **filesystem-only mode** — Postgres becomes optional, the
DB-backed initiative catalog endpoints return 503, and the loader falls
back to YAML files only. This lets the service start cleanly in dev/CI
without a Postgres available.

Pattern mirrors `leartech-auth-service` — async SQLAlchemy 2.0 with
asyncpg driver. DB connection is established on FastAPI startup,
disposed on shutdown.

Schema is managed by the chart's `migrations-job.yaml` Helm hook
(post-install,post-upgrade) — not Alembic. Mirrors auth-service.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DSN_ENV = 'LEARTECH_INITIATIVE_DB_DSN'

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def is_db_enabled() -> bool:
    """True iff a DSN is configured. Endpoints + loader behave accordingly."""
    return bool(os.environ.get(DSN_ENV))


def _normalise_dsn(dsn: str) -> str:
    """Make a libpq-style DSN safe for asyncpg + SQLAlchemy 2.x.

    Two transforms:

    - Scheme: `postgres://` / `postgresql://` → `postgresql+asyncpg://`
    - Query param: `sslmode=X` → `ssl=X` (asyncpg rejects libpq's `sslmode`
      keyword with `TypeError: connect() got an unexpected keyword argument
      'sslmode'`). Standard libpq DSNs everyone documents use `sslmode=require`;
      we accept that spelling and translate it so external tooling (psql,
      pgbench, IDE clients) can use the same string verbatim.
    """
    if dsn.startswith('postgres://'):
        dsn = 'postgresql+asyncpg://' + dsn[len('postgres://') :]
    elif dsn.startswith('postgresql://'):
        dsn = 'postgresql+asyncpg://' + dsn[len('postgresql://') :]
    dsn = dsn.replace('?sslmode=', '?ssl=').replace('&sslmode=', '&ssl=')
    return dsn


def _build_engine() -> AsyncEngine:
    """Build the async engine from the env-configured DSN.

    Accepts a libpq-style DSN; see `_normalise_dsn` for the transforms applied.
    """
    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        raise RuntimeError(f'{DSN_ENV} is not set; cannot build engine')
    dsn = _normalise_dsn(dsn)
    # pool_size/max_overflow only apply to QueuePool (Postgres). SQLite uses
    # StaticPool which rejects them — skip when running against SQLite (tests).
    kwargs: dict[str, object] = {'pool_pre_ping': True}
    if 'sqlite' not in dsn:
        kwargs['pool_size'] = 5
        kwargs['max_overflow'] = 10
    return create_async_engine(dsn, **kwargs)


def init_engine() -> AsyncEngine:
    """Initialise the module-level engine + session factory.

    Idempotent — called from FastAPI's startup. If the engine already
    exists, returns it without reconfiguring.
    """
    global _engine, _session_factory
    if _engine is None:
        _engine = _build_engine()
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def dispose_engine() -> None:
    """Tear down the engine. Called from FastAPI's shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    """Yield an async session, auto-rolling-back on exception.

    Use as `async with session() as db: ...` inside endpoint handlers
    or CRUD ops. The engine must be initialised first (handled by
    FastAPI's startup hook).
    """
    if _session_factory is None:
        raise RuntimeError('DB session factory not initialised; call init_engine() first')
    async with _session_factory() as sess:
        try:
            yield sess
            await sess.commit()
        except Exception:
            await sess.rollback()
            raise


def session_factory_or_none() -> async_sessionmaker[AsyncSession] | None:
    """Test helper — returns the factory without raising if uninitialised."""
    return _session_factory


def _reset_for_tests() -> None:
    """Test-only: drop the module-level state. Use after monkeypatching env vars."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None


__all__ = [
    'DSN_ENV',
    'AsyncSession',
    'dispose_engine',
    'init_engine',
    'is_db_enabled',
    'session',
    'session_factory_or_none',
]


def _engine_attr() -> Any:
    """Test helper — returns the current engine without exposing the global."""
    return _engine
