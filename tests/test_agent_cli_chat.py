"""Tests for the ``leartech-agent chat`` REPL.

These don't drive a real prompt_toolkit session — that needs a TTY.
Instead we exercise the building blocks:

* ``_post_chat`` against a stub httpx.Client
* ``_handle_slash`` for all REPL slash commands
* End-to-end REPL by stubbing ``_read_user_input`` to feed a scripted
  list of operator messages (this also covers the conversation_id
  continuity property the initiative explicitly calls out)
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from app.agent_cli.commands import chat as chat_mod
from app.agent_cli.commands.chat import _handle_slash, _post_chat, _Transcript
from app.agent_cli.main import cli


def _make_client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(base_url='http://orch.test', transport=transport)


def test_post_chat_sends_message_and_conversation_id() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured['method'] = request.method
        captured['url'] = str(request.url)
        captured['body'] = request.content.decode('utf-8')
        return httpx.Response(
            200,
            json={
                'conversation_id': 'conv-abc',
                'turn': {'content': 'hello back', 'cost_usd': 0.0123},
            },
        )

    transport = httpx.MockTransport(handler)
    with _make_client(transport) as client:
        body = _post_chat(client, message='hi', conversation_id=None)

    assert captured['method'] == 'POST'
    assert captured['url'].endswith('/chat')
    assert '"message":"hi"' in captured['body'].replace(' ', '')
    assert body['conversation_id'] == 'conv-abc'


def test_post_chat_raises_for_status() -> None:
    """5xx + 4xx should propagate so the REPL renders an error and keeps going."""
    transport = httpx.MockTransport(lambda req: httpx.Response(500, json={'detail': 'boom'}))
    with _make_client(transport) as client, pytest.raises(httpx.HTTPStatusError):
        _post_chat(client, message='hi', conversation_id=None)


def test_post_chat_rejects_non_object_body() -> None:
    """Defensive guard — a list body would have no conversation_id."""
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=['oops']))
    with _make_client(transport) as client, pytest.raises(ValueError, match='JSON object'):
        _post_chat(client, message='hi', conversation_id=None)


def test_slash_exit_terminates_loop() -> None:
    cont, _ = _handle_slash(':exit', _Transcript())
    assert cont is False


def test_slash_quit_alias_terminates_loop() -> None:
    cont, _ = _handle_slash('quit', _Transcript())
    assert cont is False


def test_slash_cost_reports_running_total() -> None:
    tr = _Transcript(conversation_id='conv-1')
    tr.turns.append({'user': 'hi', 'assistant': 'hi back', 'cost_usd': 0.01})
    tr.turns.append({'user': 'more?', 'assistant': 'yes', 'cost_usd': 0.025})
    cont, _ = _handle_slash(':cost', tr)
    assert cont is True
    # total_cost() is what the slash command renders; just verify the math.
    assert abs(tr.total_cost() - 0.035) < 1e-9


def test_slash_new_resets_transcript() -> None:
    tr = _Transcript(conversation_id='conv-1')
    tr.turns.append({'user': 'hi', 'assistant': 'hi back', 'cost_usd': 0.01})
    cont, fresh = _handle_slash(':new', tr)
    assert cont is True
    assert fresh.conversation_id is None
    assert fresh.turns == []


def test_slash_save_writes_transcript(tmp_path: Any) -> None:
    target = tmp_path / 'transcript.md'
    tr = _Transcript(conversation_id='conv-saved')
    tr.turns.append({'user': 'hi', 'assistant': 'hello', 'cost_usd': 0.01})
    cont, _ = _handle_slash(f':save {target}', tr)
    assert cont is True
    body = target.read_text(encoding='utf-8')
    assert 'conv-saved' in body
    assert '**You:** hi' in body
    assert '**Assistant:** hello' in body
    assert '$0.0100' in body


def test_slash_unknown_command_keeps_running() -> None:
    cont, _ = _handle_slash(':bogus', _Transcript())
    assert cont is True


def test_chat_repl_continues_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two messages → second call sends the conversation_id from turn 1.

    This is the headline contract: REPL must thread conversation_id
    through successive turns so the orchestrator retains context.
    """
    posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        posts.append(body)
        # First turn assigns conv-1; second turn must echo it back.
        return httpx.Response(
            200,
            json={
                'conversation_id': 'conv-1',
                'turn': {'content': f'reply {len(posts)}', 'cost_usd': 0.01},
            },
        )

    transport = httpx.MockTransport(handler)

    # Feed two messages then EOF.
    inputs = iter(['first', 'second', None])

    def fake_input() -> str | None:
        return next(inputs)

    monkeypatch.setattr(chat_mod, '_read_user_input', fake_input)

    # Patch httpx.Client construction inside the chat module so the REPL
    # uses our MockTransport-backed client.
    class _MockedClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs['transport'] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(chat_mod.httpx, 'Client', _MockedClient)

    runner = CliRunner()
    result = runner.invoke(cli, ['chat', '--orch-url', 'http://orch.test'])
    assert result.exit_code == 0, result.output
    # Two POSTs landed.
    assert len(posts) == 2
    # First call: no conversation_id (it's null in the JSON body).
    assert posts[0]['conversation_id'] is None
    assert posts[0]['message'] == 'first'
    # Second call: conversation_id from turn 1 was carried forward.
    assert posts[1]['conversation_id'] == 'conv-1'
    assert posts[1]['message'] == 'second'


def test_chat_handles_orch_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """When /chat is unreachable, the REPL prints an error and keeps going.

    A ConnectError is the closest analogue to "service is down" in
    httpx.MockTransport land — the handler raises it, the REPL catches
    it, the operator sees a friendly line, and ``--orch-url`` keeps the
    test deterministic across machines.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('Connection refused', request=request)

    transport = httpx.MockTransport(handler)
    inputs = iter(['hello when orch is down', None])

    monkeypatch.setattr(chat_mod, '_read_user_input', lambda: next(inputs))

    class _MockedClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs['transport'] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(chat_mod.httpx, 'Client', _MockedClient)

    runner = CliRunner()
    result = runner.invoke(cli, ['chat', '--orch-url', 'http://orch.test'])
    assert result.exit_code == 0  # graceful exit, not a traceback
    assert '/chat unreachable' in result.output


def test_chat_resolves_cluster_to_orch_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """`chat --cluster gcp-staging` must hit the gcp-staging orch URL."""
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))

    seen_base_urls: list[str] = []

    class _MockedClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen_base_urls.append(str(kwargs.get('base_url', args[0] if args else '')))
            kwargs['transport'] = httpx.MockTransport(
                lambda req: httpx.Response(200, json={'conversation_id': 'x', 'turn': {'content': 'ok'}})
            )
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(chat_mod.httpx, 'Client', _MockedClient)
    monkeypatch.setattr(chat_mod, '_read_user_input', lambda: None)

    runner = CliRunner()
    result = runner.invoke(cli, ['chat', '--cluster', 'gcp-staging'])
    assert result.exit_code == 0, result.output
    assert any('leartech-orchestrator-jx-staging.jx' in url for url in seen_base_urls)


def test_chat_unknown_cluster_exits_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(cli, ['chat', '--cluster', 'moonbase'])
    assert result.exit_code != 0
    assert 'Unknown cluster' in result.output
