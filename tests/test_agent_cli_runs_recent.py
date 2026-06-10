"""Tests for ``runs list`` recent-by-default + ``--since`` parsing.

The initiative goal calls out: ``leartech-agent runs list`` should default
to ``--since 24h`` and accept both relative (``7d``) and ISO date
(``2026-06-01``) forms. ``--all`` should bypass the filter entirely.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from app.agent_cli.commands.runs import _parse_since, _record_started_after
from app.agent_cli.main import cli


def test_parse_since_relative_hours() -> None:
    cutoff = _parse_since('3h')
    delta = _dt.datetime.now(_dt.UTC) - cutoff
    # Allow a small wall-clock window.
    assert 2.9 * 3600 < delta.total_seconds() < 3.1 * 3600


def test_parse_since_relative_days() -> None:
    cutoff = _parse_since('7d')
    delta = _dt.datetime.now(_dt.UTC) - cutoff
    assert 6.9 * 86400 < delta.total_seconds() < 7.1 * 86400


def test_parse_since_iso_date() -> None:
    cutoff = _parse_since('2026-06-01')
    assert cutoff.year == 2026
    assert cutoff.month == 6
    assert cutoff.day == 1
    # Made timezone-aware.
    assert cutoff.tzinfo is not None


def test_parse_since_iso_datetime() -> None:
    cutoff = _parse_since('2026-06-01T12:30:00')
    assert cutoff.hour == 12
    assert cutoff.minute == 30


def test_parse_since_rejects_garbage() -> None:
    import click as _click

    with pytest.raises(_click.BadParameter):
        _parse_since('two-fortnights')


def test_record_started_after_keeps_missing_field() -> None:
    cutoff = _dt.datetime.now(_dt.UTC)
    # If the record has no started_at, we keep it (cautious default).
    assert _record_started_after({'id': 'r1'}, cutoff) is True


def test_record_started_after_filters_by_cutoff() -> None:
    cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
    fresh = {'started_at': _dt.datetime.now(_dt.UTC).isoformat()}
    stale = {'started_at': (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=3)).isoformat()}
    assert _record_started_after(fresh, cutoff) is True
    assert _record_started_after(stale, cutoff) is False


def test_record_started_after_accepts_trailing_z() -> None:
    cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1)
    iso_z = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    assert _record_started_after({'started_at': iso_z}, cutoff) is True


def _patch_with_client(items: list[dict[str, Any]]) -> Any:
    """Patch the main `httpx.Client` reference with a MockTransport-backed real client.

    Using a real ``httpx.Client`` (subclassed) keeps ``client_from_ctx``'s
    ``isinstance(client, httpx.Client)`` assertion happy *and* keeps
    ``isinstance(..., httpx.Client)`` resolvable, since the patch target
    becomes a real class. ``MockTransport`` lets us hand-craft the
    response per request.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).endswith('/initiatives'), f'unexpected URL {request.url}'
        return httpx.Response(200, json=items)

    transport = httpx.MockTransport(handler)

    class _MockedClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs['transport'] = transport
            super().__init__(*args, **kwargs)

    return patch('app.agent_cli.main.httpx.Client', _MockedClient)


def _ts(hours_ago: float) -> str:
    return (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=hours_ago)).isoformat()


def test_runs_list_defaults_to_recent_24h() -> None:
    items = [
        {'id': 'r-fresh', 'initiative': 'i1', 'status': 'complete', 'started_at': _ts(2)},
        {'id': 'r-stale', 'initiative': 'i2', 'status': 'complete', 'started_at': _ts(72)},
    ]
    runner = CliRunner()
    with _patch_with_client(items):
        result = runner.invoke(cli, ['runs', 'list'])
    assert result.exit_code == 0, result.output
    assert 'r-fresh' in result.output
    # 24h default filter drops the 72h-old one.
    assert 'r-stale' not in result.output
    assert 'since 24h' in result.output


def test_runs_list_all_flag_shows_history() -> None:
    items = [
        {'id': 'r-fresh', 'initiative': 'i1', 'status': 'complete', 'started_at': _ts(2)},
        {'id': 'r-stale', 'initiative': 'i2', 'status': 'complete', 'started_at': _ts(720)},
    ]
    runner = CliRunner()
    with _patch_with_client(items):
        result = runner.invoke(cli, ['runs', 'list', '--all'])
    assert result.exit_code == 0, result.output
    assert 'r-fresh' in result.output
    assert 'r-stale' in result.output
    assert 'since' not in result.output  # `--all` strips the suffix


def test_runs_list_since_7d_widens_window() -> None:
    items = [
        {'id': 'r-2d', 'initiative': 'i1', 'status': 'complete', 'started_at': _ts(48)},
        {'id': 'r-10d', 'initiative': 'i2', 'status': 'complete', 'started_at': _ts(240)},
    ]
    runner = CliRunner()
    with _patch_with_client(items):
        result = runner.invoke(cli, ['runs', 'list', '--since', '7d'])
    assert result.exit_code == 0, result.output
    assert 'r-2d' in result.output
    # 10 days > 7 day window — should be filtered out.
    assert 'r-10d' not in result.output


def test_runs_list_since_iso_date() -> None:
    far_past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=400)).date().isoformat()
    items = [
        {'id': 'r-recent', 'initiative': 'i1', 'status': 'complete', 'started_at': _ts(2)},
    ]
    runner = CliRunner()
    with _patch_with_client(items):
        result = runner.invoke(cli, ['runs', 'list', '--since', far_past])
    assert result.exit_code == 0, result.output
    assert 'r-recent' in result.output


def test_runs_list_no_recent_runs_hints_at_all_flag() -> None:
    items = [
        {'id': 'r-old', 'initiative': 'i2', 'status': 'complete', 'started_at': _ts(720)},
    ]
    runner = CliRunner()
    with _patch_with_client(items):
        result = runner.invoke(cli, ['runs', 'list'])
    assert result.exit_code == 0, result.output
    assert '--all' in result.output


def test_runs_list_empty_state_unchanged() -> None:
    """Empty backend → original "No initiative runs yet" message preserved."""
    runner = CliRunner()
    with _patch_with_client([]):
        result = runner.invoke(cli, ['runs', 'list'])
    assert result.exit_code == 0, result.output
    assert 'No initiative runs yet' in result.output
