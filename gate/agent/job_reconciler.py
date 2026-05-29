"""Job status reconciler — D.5 surface.

When `LEARTECH_INITIATIVE_RUNTIME=job`, D.3+D.4 spawn each initiative as
its own K8s Job. The API pod no longer holds an asyncio.task for the
run, so it has no in-process hook to update `initiative_runs.status` on
completion. Without something else watching, the DB row stays at
`queued` forever even though the agent ran successfully.

This module fills that gap. A background asyncio task polls Jobs labelled
`leartech.io/component=initiative-runner` in `POD_NAMESPACE`, and for any
Job that has reached terminal state (`Complete` or `Failed`) while the DB
row is still non-terminal, it:

  1. Reads the latest pod's log tail
  2. Parses the trailing `--- turns=X  in=Y  out=Z  cost=$W` summary
  3. Greps the log for a `gh pr create` PR URL
  4. Calls `app.state.update()` with the derived fields

Idempotency: the reconciler checks the DB row's current status before
patching. Already-terminal rows are skipped — repeated polls do not
re-emit updates. Job-name === run_id (D.3 contract) so no label-selector
query needed for the lookup.

Authentication: in-cluster ServiceAccount; same `load_incluster_config`
the spawn path uses (synchronous in `kubernetes_asyncio`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient

from app.routers.initiatives import _run_self_retrospect
from app.state import get as get_record
from app.state import update

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.environ.get('LEARTECH_JOB_RECONCILER_POLL_SECONDS', '15'))
LABEL_SELECTOR = 'leartech.io/component=initiative-runner'
TERMINAL_STATUSES = frozenset({'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'})
LOG_TAIL_LINES = 200

# `--- turns=10  in=15  out=2945  cost=$0.5230` — per-turn summary lines.
# The FINAL line emitted by run_initiative (after PR resolution) appends
# `  pr=N` when a PR was opened. Capture both turns/cost and optional pr.
# Floats + ints both accepted on cost.
_SUMMARY_RE = re.compile(
    r'^---\s*turns=(?P<turns>\d+)\s+in=\S+\s+out=\S+\s+cost=\$(?P<cost>\d+(?:\.\d+)?)'
    r'(?:\s+pr=(?P<pr>\d+))?',
    re.MULTILINE,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def _list_runner_jobs(batch: client.BatchV1Api, namespace: str) -> list[Any]:
    resp = await batch.list_namespaced_job(namespace=namespace, label_selector=LABEL_SELECTOR)
    items: list[Any] = list(resp.items)
    return items


def _job_terminal_state(job: Any) -> str | None:
    """Returns 'complete' / 'failed' / None.

    K8s Jobs surface terminal state via `.status.conditions[]` — a condition
    of type Complete (True) means success; type Failed (True) means failure.
    BackoffLimit=0 (our spawn default) means the first failed pod ends the Job.
    """
    conditions = (job.status.conditions if job.status else None) or []
    for cond in conditions:
        if getattr(cond, 'status', None) != 'True':
            continue
        ctype = getattr(cond, 'type', None)
        if ctype == 'Complete':
            return 'complete'
        if ctype == 'Failed':
            return 'failed'
    return None


async def _fetch_pod_log_tail(core: client.CoreV1Api, namespace: str, job_name: str) -> str:
    """Best-effort log fetch for the Job's pod.

    Returns '' on any failure — the reconciler still patches status even
    when logs are unavailable (pod GC'd, log driver hiccup). Turns/cost
    will be None in that case; status + finished_at always make it through.
    """
    try:
        pods = await core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f'leartech.io/run-id={job_name}',
        )
        if not pods.items:
            return ''
        pod_name = pods.items[0].metadata.name
        log_text: str = await core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=LOG_TAIL_LINES,
        )
        return log_text
    except Exception as exc:  # noqa: BLE001 — logs are best-effort
        logger.debug('reconciler: log fetch failed for %s: %s', job_name, exc)
        return ''


def _parse_summary(log_text: str) -> tuple[int | None, float | None, int | None]:
    """Extract (turns, cost_usd, pr_number) from the trailing agent summary.

    The agent's `gate.agent.initiative` CLI emits a final summary line after
    PR resolution: `--- turns=X  in=...  out=...  cost=$W  pr=N` (pr=N is
    present only when a PR was opened). We pick the LAST `--- turns=` line
    so per-turn summaries don't overshadow the final authoritative one.
    """
    matches = list(_SUMMARY_RE.finditer(log_text))
    if not matches:
        return None, None, None
    last = matches[-1]
    pr_raw = last.group('pr')
    return (
        int(last.group('turns')),
        float(last.group('cost')),
        int(pr_raw) if pr_raw else None,
    )


async def reconcile_once(namespace: str) -> int:
    """One pass over runner Jobs. Returns the number of rows updated."""
    config.load_incluster_config()
    updates = 0
    async with ApiClient() as api:
        batch = client.BatchV1Api(api)
        core = client.CoreV1Api(api)
        jobs = await _list_runner_jobs(batch, namespace)
        for job in jobs:
            terminal = _job_terminal_state(job)
            if terminal is None:
                continue
            run_id = job.metadata.name  # D.3 contract: job_name == run_id
            record = await get_record(run_id)
            if record is None:
                logger.debug('reconciler: no DB row for terminal Job %s; skipping', run_id)
                continue
            if record.status in TERMINAL_STATUSES:
                continue
            log_text = await _fetch_pod_log_tail(core, namespace, run_id)
            turns, cost, pr_number = _parse_summary(log_text)
            await update(
                run_id,
                status=terminal,
                finished_at=_now(),
                turns=turns,
                cost_usd=cost,
                pr_number=pr_number,
            )
            updates += 1
            logger.info(
                'reconciler: patched %s -> %s (turns=%s cost=%s pr=%s)',
                run_id,
                terminal,
                turns,
                cost,
                pr_number,
            )
            # Post-success retrospective for Job-mode runs (D.5.2).
            # The asyncio path's `_run_and_track` already fires this on
            # successful completion; Job-mode goes through the reconciler
            # and would otherwise skip the post-success Issue filing.
            # Idempotency: the `record.status in TERMINAL_STATUSES` guard
            # above means each run flips non-terminal -> terminal at most
            # once per reconcile_once invocation, so retrospect fires at
            # most once too. Best-effort — never let a retrospect failure
            # poison the reconciler loop (the run row is already patched).
            if terminal == 'complete':
                try:
                    await _run_self_retrospect(run_id)
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning('reconciler: self_retrospect failed for %s: %s', run_id, exc)
    return updates


async def reconciler_loop(namespace: str, *, interval_seconds: int = POLL_INTERVAL_SECONDS) -> None:
    """Forever loop. Cancellable via task.cancel() during shutdown.

    Each iteration is wrapped: a single-pass exception logs + sleeps;
    the loop body never propagates an error that would kill the task.
    """
    logger.info(
        'reconciler: starting (namespace=%s, interval=%ds, label=%s)',
        namespace,
        interval_seconds,
        LABEL_SELECTOR,
    )
    while True:
        try:
            await reconcile_once(namespace)
        except asyncio.CancelledError:
            logger.info('reconciler: cancelled — exiting cleanly')
            raise
        except Exception as exc:  # noqa: BLE001 — never let one pass kill the loop
            logger.warning('reconciler: pass failed: %s', exc)
        await asyncio.sleep(interval_seconds)
