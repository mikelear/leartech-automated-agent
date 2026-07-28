"""Tests for gate.mcp_servers.ai_gateway_web_server — the BA agent's in-process
MCP wrapping /v1/search + /v1/fetch.

Two concerns pinned here:

  * The server builds cleanly with both tools registered (name-typo guard).
  * Each tool's handler:
      - Returns a structured envelope on success.
      - Returns an ``{"error": ...}`` envelope when the gateway env is unset,
        instead of raising — so the LLM can degrade gracefully and produce
        a plan without web research.
      - Returns an ``{"error": ...}`` envelope on empty inputs (empty query
        or url).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from gate.mcp_servers import ai_gateway_web_server, build_ai_gateway_web_server
from gate.tools import ai_gateway_web


def _text_payload(result: dict[str, Any]) -> Any:
    """Extract the parsed JSON body from a tool's `content` envelope."""
    return json.loads(result['content'][0]['text'])


def test_server_builds() -> None:
    server = build_ai_gateway_web_server()
    assert server is not None


@pytest.mark.asyncio
async def test_web_search_returns_error_when_gateway_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The BA agent invokes web_search speculatively — no env should not crash
    the SDK loop; the LLM sees a structured `{"error": ...}` and moves on."""
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    monkeypatch.delenv('AI_GATEWAY_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    result = await ai_gateway_web_server._web_search.handler({'query': 'anything'})
    body = _text_payload(result)
    assert 'error' in body
    assert 'unavailable' in body['error']


@pytest.mark.asyncio
async def test_web_fetch_returns_error_when_gateway_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    monkeypatch.delenv('AI_GATEWAY_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)

    result = await ai_gateway_web_server._web_fetch.handler({'url': 'https://x'})
    body = _text_payload(result)
    assert 'error' in body
    assert 'unavailable' in body['error']


@pytest.mark.asyncio
async def test_web_search_returns_error_on_empty_query() -> None:
    result = await ai_gateway_web_server._web_search.handler({'query': ''})
    body = _text_payload(result)
    assert 'error' in body
    assert 'empty query' in body['error']


@pytest.mark.asyncio
async def test_web_fetch_returns_error_on_empty_url() -> None:
    result = await ai_gateway_web_server._web_fetch.handler({'url': ''})
    body = _text_payload(result)
    assert 'error' in body
    assert 'empty url' in body['error']


@pytest.mark.asyncio
async def test_web_search_returns_results_envelope() -> None:
    """Happy path: the underlying helper returns hits; the tool wraps them in
    the SDK envelope so the LLM reads a JSON structure."""
    fake_hits = [
        ai_gateway_web.WebSearchResult(title='T1', url='https://u1', snippet='s1'),
        ai_gateway_web.WebSearchResult(title='T2', url='https://u2', snippet='s2'),
    ]
    with patch.object(ai_gateway_web_server, 'web_search', return_value=fake_hits):
        result = await ai_gateway_web_server._web_search.handler({'query': 'foo', 'max_results': 3})
    body = _text_payload(result)
    assert body['query'] == 'foo'
    assert body['max_results'] == 3
    assert body['results'] == [
        {'title': 'T1', 'url': 'https://u1', 'snippet': 's1'},
        {'title': 'T2', 'url': 'https://u2', 'snippet': 's2'},
    ]


@pytest.mark.asyncio
async def test_web_fetch_returns_page_envelope() -> None:
    fake_page = ai_gateway_web.WebFetchResult(url='https://u', title='Title', content='body', truncated=False)
    with patch.object(ai_gateway_web_server, 'web_fetch', return_value=fake_page):
        result = await ai_gateway_web_server._web_fetch.handler({'url': 'https://u'})
    body = _text_payload(result)
    assert body == {'url': 'https://u', 'title': 'Title', 'content': 'body', 'truncated': False}


@pytest.mark.asyncio
async def test_web_search_default_max_results_when_absent() -> None:
    """`max_results` is optional in the tool schema — the wrapper falls back
    to the helper's default (10) when the LLM omits it."""
    fake_hits: list[ai_gateway_web.WebSearchResult] = []
    with patch.object(ai_gateway_web_server, 'web_search', return_value=fake_hits) as spy:
        await ai_gateway_web_server._web_search.handler({'query': 'foo'})
    _args, kwargs = spy.call_args
    assert kwargs['max_results'] == ai_gateway_web.DEFAULT_MAX_RESULTS
