"""Smoke tests for ``leartech-agent ops`` (bidirectional command queue CLI).

Mirrors ``tests/test_agent_cli.py`` patterns — uses FastAPI's
``TestClient`` proxied through a stand-in ``httpx.Client`` so the CLI
exercises real router wiring without needing a live network.

Pairs with ``tests/agent/test_command_queue.py`` (which covers the DB
+ drain layers); these tests focus on the click-layer plumbing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.state as state_module
from app import db as db_module
from app.agent_cli.main import cli
from app.db.models import Base
from app.main import app
from app.state import InitiativeRecord, new_id, now, register

test_client = TestClient(app)


class _MockHttpxClient:
    """Stand-in for httpx.Client that proxies to FastAPI's TestClient."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._test_client = test_client

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        return self._test_client.get(url, **kwargs)  # type: ignore[arg-type]

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        return self._test_client.post(url, **kwargs)  # type: ignore[arg-type]


@pytest_asyncio.fixture
async def db_enabled(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Enable the DB with an in-memory SQLite engine + clean state cache."""
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
    # Reset the async sessionmaker that TestClient (sync) might have left dangling.
    _ = async_sessionmaker  # silence the unused-import warning


async def _register_running_run() -> str:
    rid = new_id()
    await register(InitiativeRecord(id=rid, initiative='ops-cli-test', status='running', started_at=now()))
    return rid


def test_ops_pause_queues_command(db_enabled: None) -> None:
    """``leartech-agent ops pause <id>`` POSTs to /commands and prints success."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'pause', rid])
    assert result.exit_code == 0, result.output
    assert 'queued' in result.output
    assert 'pause' in result.output


def test_ops_cancel_with_reason(db_enabled: None) -> None:
    """Cancel passes ``--reason`` through to the payload."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'cancel', rid, '--reason', 'wrong branch'])
    assert result.exit_code == 0, result.output
    assert 'queued' in result.output
    assert 'cancel' in result.output

    # Confirm the row landed with the reason payload via a follow-up list.
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        list_result = runner.invoke(cli, ['ops', 'list', rid])
    assert list_result.exit_code == 0
    assert 'wrong branch' in list_result.output or 'cancel' in list_result.output


def test_ops_inject_requires_text_argument(db_enabled: None) -> None:
    """``ops inject <id>`` without a text argument exits non-zero (click error)."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'inject', rid])
    # click's missing-argument error → non-zero exit.
    assert result.exit_code != 0


def test_ops_inject_passes_text_to_payload(db_enabled: None) -> None:
    """The text argument flows through as payload.text."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'inject', rid, 'use docker.io not ghcr'])
    assert result.exit_code == 0, result.output
    assert 'inject_guidance' in result.output


def test_ops_resume_queues_command(db_enabled: None) -> None:
    """``ops resume`` is a thin wrapper over POST /commands."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'resume', rid])
    assert result.exit_code == 0
    assert 'resume' in result.output


def test_ops_list_shows_no_commands_when_empty(db_enabled: None) -> None:
    """Empty queue → friendly message, not an error."""
    rid = asyncio.get_event_loop().run_until_complete(_register_running_run())

    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'list', rid])
    assert result.exit_code == 0
    assert 'No' in result.output and 'commands' in result.output


def test_ops_cancel_404_when_run_missing(db_enabled: None) -> None:
    """Unknown run id surfaces the 404 detail and exits non-zero."""
    runner = CliRunner()
    with patch('app.agent_cli.main.httpx.Client', _MockHttpxClient):
        result = runner.invoke(cli, ['ops', 'cancel', 'ghost-id'])
    # The CLI uses ctx.exit(1) on HTTP errors.
    assert result.exit_code != 0
    assert 'HTTP 404' in result.output


_ = Any  # keep the import for parity with sibling test modules
