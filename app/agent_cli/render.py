"""Shared rich-rendering helpers for the CLI command modules.

Keeping a single rich ``Console`` instance + a couple of small helpers
makes the per-command modules thinner and ensures every error path
prints the same way.
"""

from __future__ import annotations

import json

import httpx
from rich.console import Console

console = Console()

# Exceptions that JSON parsing of an HTTP response can plausibly raise.
# Caught narrowly so SystemExit / KeyboardInterrupt are NOT swallowed
# during a rich operator session.
_JSON_PARSE_ERRORS = (json.JSONDecodeError, UnicodeDecodeError, ValueError, AttributeError)


def print_http_error(response: httpx.Response) -> None:
    """Render an HTTP error response in the standard ``HTTP nnn: detail`` style.

    Tries JSON first (FastAPI's default), falls back to the raw response
    body when that fails. Narrow exception list so an interrupted CLI
    invocation still propagates.
    """
    try:
        body = response.json()
        detail = body.get('detail', response.text)
    except _JSON_PARSE_ERRORS:
        detail = response.text
    console.print(f'[red]HTTP {response.status_code}:[/red] {detail}')


def client_from_ctx(ctx_obj: dict[str, object]) -> httpx.Client:
    """Narrow the Click ctx.obj['client'] back to httpx.Client for mypy."""
    client = ctx_obj['client']
    assert isinstance(client, httpx.Client)  # noqa: S101 — narrows for mypy
    return client
