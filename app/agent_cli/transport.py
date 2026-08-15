"""HTTP transport for the ``leartech-agent`` CLI.

Picks a base URL by precedence:

1. Explicit ``--url`` flag (highest).
2. ``--cluster <name>`` flag → looked up in the user's
   ``~/.config/leartech-agent/config.yaml`` (merged over built-in
   staging defaults). Short forms (e.g. ``gcp``) prefix-match the
   canonical name (e.g. ``gcp-staging``); see
   :meth:`app.agent_cli.config.CliConfig.resolve_cluster`.
3. ``LEARTECH_AGENT_URL`` env var.
4. ``http://localhost:8080`` (laptop fallback).

The :func:`build_client` function returns a configured ``httpx.Client``
the command modules use. Keeping it isolated here lets the CliRunner
tests monkey-patch one symbol instead of every command's own client
construction.

History: this module previously hard-coded a separate ``gcp``/``az``
cluster map pointing at ``*.product-first.com`` / ``*.modern-burro.com``
URLs, which diverged from the authoritative ``config show`` output
(``gcp-staging`` / ``az-staging`` pointing at the ``jx-staging``
ingresses). That contract mismatch produced the
``Invalid value for '--cluster'`` / DNS-resolution-error pair fixed in
PR ``cli-fix-cluster-flag-name-mismatch``. The cluster map now lives in
exactly one place — :mod:`app.agent_cli.config` — and is keyed by the
canonical ``<cloud>-<env>`` names. An earlier revision also carried an
``orch_url`` per cluster (paired with the now-decommissioned
``leartech-orchestrator`` service); that field was removed from
``ClusterConfig`` in ``chore/drop-orch-cli-client``.
"""

from __future__ import annotations

import os

import httpx

from app.agent_cli.config import load_config

DEFAULT_URL = 'http://localhost:8080'


def resolve_base_url(url: str | None, cluster: str | None) -> str:
    """Resolve the base URL the CLI should target this invocation.

    ``cluster`` may be the canonical name (``gcp-staging``) or any
    unambiguous prefix (``gcp``); resolution goes through the merged
    config map so the same name that ``config show`` reports is the one
    accepted on the flag.

    When ``--cluster`` is supplied, it wins over the un-suffixed
    ``LEARTECH_AGENT_URL`` env var — operators reach for ``--cluster``
    precisely to override the ambient env on a one-off invocation. When
    no cluster is named, env wins over the localhost fallback.
    """
    if url:
        return url
    if cluster:
        cfg = load_config()
        return cfg.resolve_cluster(cluster).agent_url
    return os.environ.get('LEARTECH_AGENT_URL', DEFAULT_URL)


def build_client(url: str | None = None, cluster: str | None = None) -> httpx.Client:
    """Construct an ``httpx.Client`` against the resolved URL.

    Caller owns the lifecycle. The CLI's top-level group resolves once at
    invocation time and stows the client in the Click context.
    """
    base_url = resolve_base_url(url, cluster)
    return httpx.Client(base_url=base_url, timeout=30.0)
