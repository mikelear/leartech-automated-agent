"""Unit tests for app.db._normalise_dsn — the libpq → asyncpg DSN translation.

Surfaced 2026-05-22 by a crashlooping production pod: the DSN in GSM used the
standard libpq `?sslmode=require` query param, which asyncpg rejects with
`TypeError: connect() got an unexpected keyword argument 'sslmode'`. The fix
translates `sslmode=` → `ssl=` so libpq-style DSNs (the form documented
everywhere) work verbatim.
"""

from __future__ import annotations

from app.db import _normalise_dsn


def test_postgresql_scheme_rewritten() -> None:
    assert _normalise_dsn('postgresql://user:p@host/db') == 'postgresql+asyncpg://user:p@host/db'


def test_postgres_scheme_rewritten() -> None:
    assert _normalise_dsn('postgres://user:p@host/db') == 'postgresql+asyncpg://user:p@host/db'


def test_sslmode_query_translated() -> None:
    in_dsn = 'postgresql://user:p@host/db?sslmode=require'
    expected = 'postgresql+asyncpg://user:p@host/db?ssl=require'
    assert _normalise_dsn(in_dsn) == expected


def test_sslmode_with_other_params() -> None:
    in_dsn = 'postgresql://user:p@host/db?application_name=agent&sslmode=require'
    expected = 'postgresql+asyncpg://user:p@host/db?application_name=agent&ssl=require'
    assert _normalise_dsn(in_dsn) == expected


def test_no_sslmode_unchanged() -> None:
    in_dsn = 'postgresql://user:p@host/db?application_name=agent'
    expected = 'postgresql+asyncpg://user:p@host/db?application_name=agent'
    assert _normalise_dsn(in_dsn) == expected


def test_already_normalised_dsn_unchanged() -> None:
    in_dsn = 'postgresql+asyncpg://user:p@host/db?ssl=require'
    assert _normalise_dsn(in_dsn) == in_dsn


def test_sqlite_in_memory_passthrough() -> None:
    assert _normalise_dsn('sqlite+aiosqlite:///:memory:') == 'sqlite+aiosqlite:///:memory:'
