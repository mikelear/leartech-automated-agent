"""Admin endpoints — admin-token-gated cleanup operations.

Phase B (B.3 + B.4) — single endpoint that an operator (or a Cron) can
invoke to reconcile two classes of accumulated state:

  - Stuck DB rows: in-flight ``initiative_runs`` whose backing Job is
    gone but the row stays in ``queued`` / ``running``.
  - Superseded PipelineRuns: Lighthouse-spawned Tekton PipelineRuns
    against earlier SHAs of an agent-authored PR; once the PR's tip
    moves forward these are wasting cluster etcd + queue slots.

The imperative work lives in :mod:`gate.admin.cleanup`. This module
restricts itself to HTTP-side concerns (auth header gating, env
resolution, response shape).

Auth: a static admin token from the chart-rendered env
``LEARTECH_ADMIN_TOKEN``. ExternalSecret materialises it from
Vault/GSM. Requests without a matching ``X-Admin-Token`` header are
rejected with 401. The token is purposefully a static shared secret
rather than a per-operator JWT — the surface is internal-only,
infrequently invoked, and adding identity plumbing is out of scope
for B.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Header, HTTPException

from gate.admin.cleanup import (
    DEFAULT_REPO,
    cancel_superseded_pipelineruns,
    reconcile_orphaned_runs,
)

logger = logging.getLogger(__name__)

router = APIRouter()


ADMIN_TOKEN_ENV = 'LEARTECH_ADMIN_TOKEN'  # noqa: S105 — env-var name, not a secret
DEFAULT_OLDER_THAN_SECONDS = 86400  # 24h — operator default; conservative


def _resolve_namespace() -> str:
    """Return the namespace to operate in, raising 500 when POD_NAMESPACE is unset."""
    ns = os.environ.get('POD_NAMESPACE')
    if not ns:
        raise HTTPException(
            status_code=500,
            detail=(
                'POD_NAMESPACE env var is required for /admin/cleanup; '
                'chart deployment must inject it via fieldRef metadata.namespace.'
            ),
        )
    return ns


def _check_admin_token(provided: str | None) -> None:
    """Reject the request when the provided token doesn't match the chart-rendered env.

    A missing env on the server side is treated as "auth not configured" —
    return 401 too, so an operator hitting the endpoint on a cluster that
    hasn't populated the secret yet sees a clear refusal instead of an
    accidental wide-open path.
    """
    expected = os.environ.get(ADMIN_TOKEN_ENV)
    if not expected or not provided or provided != expected:
        raise HTTPException(status_code=401, detail='invalid admin token')


@router.post('/admin/cleanup')
async def admin_cleanup(
    x_admin_token: str | None = Header(default=None, alias='X-Admin-Token'),
    older_than_seconds: int = DEFAULT_OLDER_THAN_SECONDS,
    repo: str = DEFAULT_REPO,
) -> dict[str, int]:
    """Run the orphan-run + superseded-PipelineRun cleanup sweep.

    Returns a JSON summary ``{stuck_runs_marked, pipelineruns_cancelled}``.

    Path is admin-token-gated. The endpoint is idempotent — re-running it
    immediately yields zero on both counters once the steady state is
    clean.
    """
    _check_admin_token(x_admin_token)
    namespace = _resolve_namespace()

    stuck = await reconcile_orphaned_runs(older_than_seconds=older_than_seconds)
    psns = await cancel_superseded_pipelineruns(namespace=namespace, repo=repo)
    logger.info(
        'admin_cleanup completed: stuck_runs_marked=%d pipelineruns_cancelled=%d '
        '(older_than_seconds=%d, repo=%s, namespace=%s)',
        stuck,
        psns,
        older_than_seconds,
        repo,
        namespace,
    )
    return {'stuck_runs_marked': stuck, 'pipelineruns_cancelled': psns}
