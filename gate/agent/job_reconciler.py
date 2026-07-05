"""Project AgentRun status → initiative_runs (Slice B of the Orch+Agent rewrite).

The Go control plane (leartech-orchestrator-controller) now owns the mechanical
spawn + tracking: it builds the Job, watches it to terminal, respawns on deadline,
and the agent self-reports the PR onto AgentRun.status.targetPR. This loop simply
mirrors each AgentRun's status onto its initiative_runs DB row so the existing
API / UI / CLI keep working — replacing the old Job-listing + pod-log-scrape
reconciler entirely.

Deferred to the Go controller (backlog — dropped from the log-scrape era; this path
is not yet load-bearing so the regression is acceptable per the rewrite plan):
crash-stickies on hard pod death, fast ImagePull/stuck-pod detection, orphan-row
cleanup, and turns/cost metrics.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.routers.initiatives import _run_self_retrospect
from app.state import get as get_record
from app.state import update
from gate.agent.agentrun_client import list_agent_runs

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.environ.get('LEARTECH_JOB_RECONCILER_POLL_SECONDS', '15'))
TERMINAL_STATUSES = frozenset({'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'})

# AgentRun.status.phase (Go controller) → initiative_runs.status (DB).
_PHASE_TO_STATUS: dict[str, str] = {
    'Succeeded': 'complete',
    'Failed': 'failed',
    'Cancelled': 'cancelled',
    'Running': 'running',
    'Iterating': 'running',
    'Pending': 'queued',
    'Queued': 'queued',
}


def _now() -> datetime:
    return datetime.now(UTC)


def _pr_from_status(status: dict[str, Any]) -> int | None:
    raw = status.get('targetPR')
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def reconcile_once(namespace: str) -> int:
    """Mirror each AgentRun's status onto its initiative_runs row. Returns rows updated."""
    updates = 0
    for run in await list_agent_runs(namespace):
        run_id = run.get('metadata', {}).get('name')
        status = run.get('status') or {}
        db_status = _PHASE_TO_STATUS.get(status.get('phase', ''))
        if not run_id or db_status is None:
            continue
        record = await get_record(run_id)
        if record is None or record.status in TERMINAL_STATUSES or record.status == db_status:
            continue
        pr_number = _pr_from_status(status)
        # Explicit kwargs (not **fields) so the Python-side pr_number writer stays
        # greppable — the state-persistence audit pins that pr_number is written
        # via the SQLAlchemy update() helper, not a bash psql subprocess.
        if db_status in TERMINAL_STATUSES:
            await update(run_id, status=db_status, pr_number=pr_number, finished_at=_now())
        else:
            await update(run_id, status=db_status, pr_number=pr_number)
        updates += 1
        logger.info('reconciler: %s -> %s (pr=%s)', run_id, db_status, pr_number)
        if db_status == 'complete':
            try:
                await _run_self_retrospect(run_id)
            except Exception as exc:  # noqa: BLE001 — best-effort, never poison the loop
                logger.warning('reconciler: self_retrospect failed for %s: %s', run_id, exc)
    return updates


async def reconciler_loop(namespace: str) -> None:
    """Poll-project AgentRun status onto initiative_runs until cancelled."""
    logger.info('AgentRun status reconciler started (namespace=%s, interval=%ss)', namespace, POLL_INTERVAL_SECONDS)
    while True:
        try:
            await reconcile_once(namespace)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception('reconcile_once pass failed; continuing')
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
