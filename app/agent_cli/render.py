"""Shared rich-rendering helpers for the CLI command modules.

Keeping a single rich ``Console`` instance + a couple of small helpers
makes the per-command modules thinner and ensures every error path
prints the same way.
"""

from __future__ import annotations

import httpx
from rich.console import Console

console = Console()


def print_http_error(response: httpx.Response) -> None:
    """Render an HTTP error response in the standard ``HTTP nnn: detail`` style.

    Tries JSON first (FastAPI's default), falls back to the raw response
    body when that fails.
    """
    try:
        body = response.json()
        detail = body.get('detail', response.text)
    except Exception:  # noqa: BLE001 — fall back to raw text on any parse error
        detail = response.text
    console.print(f'[red]HTTP {response.status_code}:[/red] {detail}')


def client_from_ctx(ctx_obj: dict[str, object]) -> httpx.Client:
    """Narrow the Click ctx.obj['client'] back to httpx.Client for mypy."""
    client = ctx_obj['client']
    assert isinstance(client, httpx.Client)  # noqa: S101 — narrows for mypy
    return client
