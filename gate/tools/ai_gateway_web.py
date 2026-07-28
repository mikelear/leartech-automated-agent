"""Web research helpers backed by leartech-ai-gateway (BA agent).

The BA agent (``gate.agent.ba_agent``) needs "look up the state of the world"
capability for reasoning about a brief before authoring Plan CRDs — e.g.
"has upstream fixed this crash class?", "what's the current release cadence
for repo X?". Rather than hard-wire an external search provider, the ai-gateway
already brokers ``/v1/search`` + ``/v1/fetch`` for this exact use-case (its
tool-agnostic web layer). We call it as an HTTP client here so the BA agent
gets the same governance / metering as the LLM turns.

## Contract

- Base URL: ``ANTHROPIC_BASE_URL`` (the same env the Claude Agent SDK reads for
  the gateway repoint — see AI-GATEWAY-AND-PORTABILITY.md). Missing → the caller
  should treat this helper as "not available" and skip web research; we log a
  warning and raise.
- Bearer key: ``AI_GATEWAY_API_KEY`` if set (the provider-neutral secret name at
  the GitOps cutover), else fall back to ``ANTHROPIC_API_KEY`` (same value in
  cluster today — the SDK reads ``ANTHROPIC_API_KEY`` and the gateway accepts it).

## Portability

These helpers deliberately do NOT go through ``gate.llm`` — ``/v1/search`` and
``/v1/fetch`` are gateway-native endpoints (not the Anthropic Messages API), so
they belong in a separate seam. When we swap the LLM backend the search /
fetch surface stays the same (it's an ai-gateway concern, not a provider
concern). See AI-GATEWAY-AND-PORTABILITY.md — "no scattered anthropic imports
in business logic" is preserved because we use ``httpx``, not the SDK.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

# Timeouts kept modest — a runaway search shouldn't stall the BA agent's turn.
# The gateway itself enforces per-call ceilings, but the client-side one is a
# defence in depth.
DEFAULT_SEARCH_TIMEOUT = 30.0
DEFAULT_FETCH_TIMEOUT = 60.0

# Default max results for a search — enough context for the agent, small enough
# to keep the round-trip fast + within token budget.
DEFAULT_MAX_RESULTS = 10


class AIGatewayWebUnavailableError(RuntimeError):
    """Raised when the gateway config is absent (no ``ANTHROPIC_BASE_URL``).

    Callers (BA agent, tests) can treat this as "web research off" rather than
    a hard failure — the agent should still produce a Plan without live search.
    """


@dataclass(frozen=True)
class WebSearchResult:
    """One search hit from the gateway's ``/v1/search`` endpoint."""

    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class WebFetchResult:
    """A single fetched page (extracted text + metadata) from ``/v1/fetch``."""

    url: str
    title: str
    content: str
    # Cheap "is the content non-trivial" signal so callers can decide to skip
    # near-empty pages without recomputing.
    truncated: bool = False


def _base_url() -> str:
    """Resolve the gateway base URL from env (``ANTHROPIC_BASE_URL``).

    Raises ``AIGatewayWebUnavailableError`` when unset — the BA agent decides how to
    degrade (skip web research, still author the plan).
    """
    base = os.environ.get('ANTHROPIC_BASE_URL', '').strip().rstrip('/')
    if not base:
        raise AIGatewayWebUnavailableError(
            'ANTHROPIC_BASE_URL is unset — the ai-gateway is not reachable; '
            'BA web research disabled. See AI-GATEWAY-AND-PORTABILITY.md.'
        )
    return base


def _api_key() -> str:
    """Bearer key: ``AI_GATEWAY_API_KEY`` preferred, ``ANTHROPIC_API_KEY`` fallback.

    Cluster has both today (they resolve to the same virtual key). The precedence
    matches the provider-neutral naming convention documented in the migration
    plan — code that ONLY reads ``AI_GATEWAY_API_KEY`` still works once the
    ``ANTHROPIC_API_KEY`` alias is phased out (Phase 3).
    """
    key = os.environ.get('AI_GATEWAY_API_KEY') or os.environ.get('ANTHROPIC_API_KEY') or ''
    if not key:
        raise AIGatewayWebUnavailableError(
            'No AI_GATEWAY_API_KEY / ANTHROPIC_API_KEY set — cannot authenticate '
            'against ai-gateway; BA web research disabled.'
        )
    return key


def _headers() -> dict[str, str]:
    return {
        'Authorization': f'Bearer {_api_key()}',
        'Content-Type': 'application/json',
    }


def _parse_search_payload(payload: Any) -> list[WebSearchResult]:
    """Turn the ai-gateway ``/v1/search`` JSON body into typed results.

    The gateway returns ``{"results": [{"title", "url", "snippet"}, ...]}``.
    Missing fields degrade to empty strings so a single malformed hit doesn't
    poison the entire result set.
    """
    if not isinstance(payload, dict):
        return []
    results_raw = payload.get('results')
    if not isinstance(results_raw, list):
        return []
    hits: list[WebSearchResult] = []
    for item in results_raw:
        if not isinstance(item, dict):
            continue
        hits.append(
            WebSearchResult(
                title=str(item.get('title', '')),
                url=str(item.get('url', '')),
                snippet=str(item.get('snippet', '')),
            )
        )
    return hits


def _parse_fetch_payload(payload: Any, *, fallback_url: str) -> WebFetchResult:
    """Turn the ai-gateway ``/v1/fetch`` JSON body into a typed result."""
    if not isinstance(payload, dict):
        return WebFetchResult(url=fallback_url, title='', content='', truncated=False)
    return WebFetchResult(
        url=str(payload.get('url', fallback_url)),
        title=str(payload.get('title', '')),
        content=str(payload.get('content', '')),
        truncated=bool(payload.get('truncated', False)),
    )


def web_search(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
) -> list[WebSearchResult]:
    """POST to ``<base>/v1/search`` and return a list of hits.

    Synchronous — the BA agent invokes this via a tool call from within its
    async SDK loop, but the gateway HTTP itself is short and the SDK schedules
    the tool call on a thread if needed. Simpler to keep the helper sync so
    both sync callers (tests) and async callers (MCP wrapper) share it.
    """
    base = _base_url()
    resp = httpx.post(
        f'{base}/v1/search',
        headers=_headers(),
        json={'query': query, 'max_results': max_results},
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_search_payload(resp.json())


def web_fetch(
    url: str,
    *,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> WebFetchResult:
    """POST to ``<base>/v1/fetch`` and return the extracted page text + metadata."""
    base = _base_url()
    resp = httpx.post(
        f'{base}/v1/fetch',
        headers=_headers(),
        json={'url': url},
        timeout=timeout,
    )
    resp.raise_for_status()
    return _parse_fetch_payload(resp.json(), fallback_url=url)


def is_available() -> bool:
    """Cheap "should the caller offer web tools?" check — no network call.

    Returns True iff ``ANTHROPIC_BASE_URL`` AND (``AI_GATEWAY_API_KEY`` or
    ``ANTHROPIC_API_KEY``) are set. Callers can gate the MCP tool registration
    on this without incurring an unauth failure at first call.
    """
    try:
        _base_url()
        _api_key()
    except AIGatewayWebUnavailableError:
        return False
    return True
