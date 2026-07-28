"""Tests for gate.tools.ai_gateway_web — the BA agent's /v1/search + /v1/fetch client.

We monkeypatch `httpx.post` so the tests don't hit the network. What we pin:

  * Base URL is read from ``ANTHROPIC_BASE_URL`` (the same env the Claude Agent
    SDK reads — one env for the LLM AND for search / fetch).
  * Bearer key preference: ``AI_GATEWAY_API_KEY`` wins over ``ANTHROPIC_API_KEY``
    (the provider-neutral secret name is the future).
  * Missing config → ``AIGatewayWebUnavailableError`` (not a crash).
  * Response parsing is robust to missing / non-list fields.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gate.tools import ai_gateway_web


class _FakeResp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError('boom', request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._payload


def _set_gateway(monkeypatch: pytest.MonkeyPatch, *, base: str = 'https://gw.example', key: str = 'sk-test') -> None:
    monkeypatch.setenv('ANTHROPIC_BASE_URL', base)
    monkeypatch.setenv('AI_GATEWAY_API_KEY', key)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)


def _clear_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ANTHROPIC_BASE_URL', raising=False)
    monkeypatch.delenv('AI_GATEWAY_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)


# --- Unavailability path ------------------------------------------------------


def test_unavailable_when_base_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway(monkeypatch)
    monkeypatch.setenv('AI_GATEWAY_API_KEY', 'sk-test')  # key set, base URL not — still unavailable
    with pytest.raises(ai_gateway_web.AIGatewayWebUnavailableError):
        ai_gateway_web.web_search('any')


def test_unavailable_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://gw.example')  # base set, key not
    with pytest.raises(ai_gateway_web.AIGatewayWebUnavailableError):
        ai_gateway_web.web_fetch('https://a')


def test_is_available_reflects_both_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_gateway(monkeypatch)
    assert ai_gateway_web.is_available() is False
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://gw')
    assert ai_gateway_web.is_available() is False
    monkeypatch.setenv('AI_GATEWAY_API_KEY', 'sk')
    assert ai_gateway_web.is_available() is True


def test_anthropic_api_key_is_accepted_as_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """During Phase 1 clusters have ANTHROPIC_API_KEY (== gateway virtual
    key) — accept it so the helpers work without waiting for the
    AI_GATEWAY_API_KEY GitOps rollout."""
    _clear_gateway(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://gw')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-fallback')
    assert ai_gateway_web.is_available() is True


def test_ai_gateway_api_key_wins_over_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both env vars are set, the provider-neutral one wins."""
    _clear_gateway(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'https://gw')
    monkeypatch.setenv('AI_GATEWAY_API_KEY', 'sk-neutral')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-legacy')

    captured: dict[str, Any] = {}

    def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResp:
        captured['headers'] = headers
        return _FakeResp(200, {'results': []})

    monkeypatch.setattr(ai_gateway_web.httpx, 'post', _fake_post)
    ai_gateway_web.web_search('q')
    assert captured['headers']['Authorization'] == 'Bearer sk-neutral'


# --- Search happy path --------------------------------------------------------


def test_web_search_posts_to_v1_search_and_parses_results(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResp:
        captured['url'] = url
        captured['headers'] = headers
        captured['json'] = json
        return _FakeResp(
            200,
            {
                'results': [
                    {'title': 'A', 'url': 'https://a', 'snippet': 'aaa'},
                    {'title': 'B', 'url': 'https://b', 'snippet': 'bbb'},
                ]
            },
        )

    monkeypatch.setattr(ai_gateway_web.httpx, 'post', _fake_post)
    hits = ai_gateway_web.web_search('the query', max_results=5)

    assert captured['url'] == 'https://gw.example/v1/search'
    assert captured['headers']['Authorization'] == 'Bearer sk-test'
    assert captured['headers']['Content-Type'] == 'application/json'
    assert captured['json'] == {'query': 'the query', 'max_results': 5}

    assert len(hits) == 2
    assert hits[0].title == 'A'
    assert hits[0].url == 'https://a'
    assert hits[0].snippet == 'aaa'


def test_web_search_handles_malformed_result_items(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hit missing some fields should degrade to empty strings — one bad
    row doesn't poison the entire result set."""
    _set_gateway(monkeypatch)
    monkeypatch.setattr(
        ai_gateway_web.httpx,
        'post',
        lambda *a, **k: _FakeResp(
            200,
            {
                'results': [
                    'not-a-dict',  # dropped entirely (not a mapping)
                    {'title': 'ok'},  # url/snippet degrade to ''
                ]
            },
        ),
    )
    hits = ai_gateway_web.web_search('q')
    assert len(hits) == 1
    assert hits[0].title == 'ok'
    assert hits[0].url == ''
    assert hits[0].snippet == ''


def test_web_search_returns_empty_when_no_results_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(200, {}))
    assert ai_gateway_web.web_search('q') == []


def test_web_search_returns_empty_on_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(200, ['strange']))
    assert ai_gateway_web.web_search('q') == []


# --- Fetch happy path ---------------------------------------------------------


def test_web_fetch_posts_to_v1_fetch_and_parses_content(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float) -> _FakeResp:
        captured['url'] = url
        captured['json'] = json
        return _FakeResp(
            200,
            {'url': 'https://a', 'title': 'Title', 'content': 'body...', 'truncated': True},
        )

    monkeypatch.setattr(ai_gateway_web.httpx, 'post', _fake_post)
    page = ai_gateway_web.web_fetch('https://a')

    assert captured['url'] == 'https://gw.example/v1/fetch'
    assert captured['json'] == {'url': 'https://a'}
    assert page.url == 'https://a'
    assert page.title == 'Title'
    assert page.content == 'body...'
    assert page.truncated is True


def test_web_fetch_falls_back_to_input_url_when_payload_missing_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(200, {'content': 'x'}))
    page = ai_gateway_web.web_fetch('https://input-url')
    assert page.url == 'https://input-url'
    assert page.content == 'x'
    assert page.title == ''
    assert page.truncated is False


def test_web_fetch_returns_empty_on_non_dict_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(200, ['strange']))
    page = ai_gateway_web.web_fetch('https://a')
    assert page.url == 'https://a'
    assert page.content == ''


# --- HTTP errors surface as HTTPStatusError ----------------------------------


def test_search_raises_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(500, {}))
    with pytest.raises(httpx.HTTPStatusError):
        ai_gateway_web.web_search('q')


def test_fetch_raises_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch)
    monkeypatch.setattr(ai_gateway_web.httpx, 'post', lambda *a, **k: _FakeResp(404, {}))
    with pytest.raises(httpx.HTTPStatusError):
        ai_gateway_web.web_fetch('https://a')


# --- Trailing-slash on base URL is normalised -------------------------------


def test_trailing_slash_on_base_does_not_double(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_gateway(monkeypatch, base='https://gw.example/')
    captured: dict[str, Any] = {}

    def _fake_post(url: str, **_kw: Any) -> _FakeResp:
        captured['url'] = url
        return _FakeResp(200, {'results': []})

    monkeypatch.setattr(ai_gateway_web.httpx, 'post', _fake_post)
    ai_gateway_web.web_search('q')
    assert captured['url'] == 'https://gw.example/v1/search'
