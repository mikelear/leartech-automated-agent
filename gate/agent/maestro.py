"""Best-effort push of ``run.pr_opened`` to Maestro.

Maestro is the org-wide event bus (the ``bus-common`` client the Go
controller uses in ``leartech-orchestrator-controller``). When the
agent captures the PR number authoritatively (from ``gh pr create``'s
return), we PUSH a ``run.pr_opened`` event so reactive consumers —
Infra Agent, chat sticky-posters, dashboards — react immediately
without polling ``AgentRun.status.targetPR`` on the CR.

Design constraints (all enforced by tests):

  * **Gated on config** — if ``LEARTECH_MAESTRO_URL`` is empty, this
    module is a silent no-op. Matches the controller's gating on the
    ``MAESTRO_URL`` config value being set. Off the cluster (laptop
    runs, unit tests) the env var is absent → zero network traffic.

  * **Best-effort, non-fatal** — every failure mode (network,
    non-2xx, timeout, JSON error, missing httpx) is caught + logged
    at WARN and swallowed. The CR status write
    (:func:`gate.agent.agentrun_status.patch_pr_number`) IS the
    source of truth; the event is a reactive-consumer optimisation.
    A failed announce MUST NOT propagate a raise out of this module
    or the SDK loop would abort mid-run.

  * **Number is passed in, never re-derived** — the caller passes the
    PR number it observed from the specific ``gh pr create``
    invocation (or the branch-scoped ``_resolve_pr_number`` fallback).
    This module never scrapes prose or re-queries GitHub.

Env vars:

  * ``LEARTECH_MAESTRO_URL`` — base URL of the Maestro HTTP publish
    endpoint. Absent → no-op.
  * ``LEARTECH_MAESTRO_TOKEN`` — optional bearer token for
    authorisation.

Wire format is deliberately a small JSON blob keyed on the fields
called out by the initiative goal: ``{topic, run, tenant, repo,
pr_number, head_branch}``. Adding a new field is source-compatible;
removing one is a consumer-contract change.
"""

from __future__ import annotations

import logging
import os
from typing import TypedDict

_logger = logging.getLogger(__name__)


class _RunPrOpenedPayload(TypedDict):
    """Wire shape of the ``run.pr_opened`` push payload.

    Kept as a :class:`TypedDict` (not ``dict[str, Any]``) so mypy
    validates all required fields are present at call sites, and so
    reviewers reading the field list have one authoritative source.
    Adding a field to the wire format is a source-compatible edit
    (add here + populate at the emit call site); removing one is a
    consumer-contract change (bump the topic name).
    """

    topic: str
    run: str | None
    tenant: str | None
    repo: str
    pr_number: int
    head_branch: str


_MAESTRO_URL_ENV = 'LEARTECH_MAESTRO_URL'
_MAESTRO_TOKEN_ENV = 'LEARTECH_MAESTRO_TOKEN'  # noqa: S105 — env var name, not a secret literal
_TOPIC = 'run.pr_opened'

# HTTP timeout for the publish. 5s is generous — Maestro pushes are a
# hot-path event bus, if it's not responsive in seconds the announce
# is worth abandoning. Bounded so a hung endpoint can't stall the SDK
# loop for its full 12h timeout window.
_TIMEOUT_SECONDS: float = 5.0


def _config() -> tuple[str, str | None] | None:
    """Return ``(url, token)`` when Maestro is configured; None otherwise.

    Gated on ``LEARTECH_MAESTRO_URL`` being both present AND non-empty
    after ``strip()``. This mirrors the controller's "check that the
    URL is materially set, not just the env var is defined" behaviour
    — a value of ``""`` from an empty ConfigMap key must still no-op.
    """
    url = (os.environ.get(_MAESTRO_URL_ENV) or '').strip()
    if not url:
        return None
    token = (os.environ.get(_MAESTRO_TOKEN_ENV) or '').strip() or None
    return url, token


async def emit_run_pr_opened(
    *,
    run: str | None,
    tenant: str | None,
    repo: str,
    pr_number: int,
    head_branch: str,
) -> None:
    """Best-effort: push a ``run.pr_opened`` event to Maestro. Never raises.

    No-op when :func:`_config` returns None (env unset or empty). Any
    failure — network, 5xx, timeout, JSON error, missing httpx — is
    caught + logged at WARN and swallowed. Callers MUST NOT depend on
    successful delivery; the CR status write is the source of truth.

    Payload shape:

        {
          "topic": "run.pr_opened",
          "run": "<agentrun-name-or-null>",
          "tenant": "<tenant-slug-or-null>",
          "repo": "mikelear/example-svc",
          "pr_number": 42,
          "head_branch": "agent/example-fix"
        }
    """
    cfg = _config()
    if cfg is None:
        return
    url, token = cfg
    payload: _RunPrOpenedPayload = {
        'topic': _TOPIC,
        'run': run,
        'tenant': tenant,
        'repo': repo,
        'pr_number': pr_number,
        'head_branch': head_branch,
    }
    try:
        # Local import so a missing httpx (should never happen — it's a
        # base dep — but keeps the announce path best-effort even in a
        # broken-import edge case) doesn't fail-fast at module load.
        import httpx

        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            _logger.warning(
                'maestro %s announce non-2xx (status=%s body=%s)',
                _TOPIC,
                resp.status_code,
                resp.text[:200],
            )
            return
        _logger.info(
            'maestro %s announce ok (pr=%s repo=%s run=%s)',
            _TOPIC,
            pr_number,
            repo,
            run,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort; must never break the run
        _logger.warning('maestro %s announce failed (non-fatal): %s', _TOPIC, exc)
