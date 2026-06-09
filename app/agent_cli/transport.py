"""HTTP transport for the ``leartech-agent`` CLI.

Picks a base URL by precedence:

1. Explicit ``--url`` flag (highest).
2. ``--cluster gcp|az`` flag → known ingress URLs.
3. ``LEARTECH_AGENT_URL`` env var.
4. ``http://localhost:8080`` (laptop fallback).

The :func:`build_client` function returns a configured ``httpx.Client``
the command modules use. Keeping it isolated here lets the CliRunner
tests monkey-patch one symbol instead of every command's own client
construction.

In-cluster fallback (``kubectl exec``-style port-forward) is on the
roadmap but not wired today — see the design memo
``project_operator_cli_design.md``. For MVP, operators on a workstation
hit the ingress URL directly; in-cluster invocations set
``LEARTECH_AGENT_URL=http://leartech-automated-agent`` against the
ClusterIP service.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_URL = 'http://localhost:8080'

# Public ingress URLs per cluster. Kept here (not in mcp_catalog.yaml)
# because they're discovery-only — no secrets, no per-environment auth.
# Operators using a different ingress override via --url or
# LEARTECH_AGENT_URL.
_CLUSTER_URLS: dict[str, str] = {
    'gcp': 'https://leartech-automated-agent.product-first.com',
    'az': 'https://leartech-automated-agent.modern-burro.com',
}


def resolve_base_url(url: str | None, cluster: str | None) -> str:
    """Resolve the base URL the CLI should target this invocation."""
    if url:
        return url
    if cluster:
        if cluster not in _CLUSTER_URLS:
            raise ValueError(f'Unknown cluster {cluster!r}; expected one of {sorted(_CLUSTER_URLS)}.')
        return _CLUSTER_URLS[cluster]
    return os.environ.get('LEARTECH_AGENT_URL', DEFAULT_URL)


def build_client(url: str | None = None, cluster: str | None = None) -> httpx.Client:
    """Construct an ``httpx.Client`` against the resolved URL.

    Caller owns the lifecycle. The CLI's top-level group resolves once at
    invocation time and stows the client in the Click context.
    """
    base_url = resolve_base_url(url, cluster)
    return httpx.Client(base_url=base_url, timeout=30.0)
