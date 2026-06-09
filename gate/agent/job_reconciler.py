"""Job status reconciler — D.5 surface.

Phase F: every initiative runs as its own K8s Job. The API pod holds
no in-process task for the run, so it has no in-process hook to update
`initiative_runs.status` on completion. Without something else
watching, the DB row stays at `running` forever even though the agent
finished cleanly.

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
import json
import logging
import os
import re
import subprocess
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_runs import list_runs
from app.routers.initiatives import _run_self_retrospect
from app.state import get as get_record
from app.state import update
from gate.agent.initiative import _post_crash_sticky

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.environ.get('LEARTECH_JOB_RECONCILER_POLL_SECONDS', '15'))
LABEL_SELECTOR = 'leartech.io/component=initiative-runner'
TERMINAL_STATUSES = frozenset({'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'})
LOG_TAIL_LINES = 200
# Crash-sticky body shows the last 50 log lines — short enough to stay readable
# in a PR comment, long enough to capture the actual stack / cause for the
# common crash shapes (OOMKilled signal, Python traceback, image-pull error).
CRASH_LOG_TAIL_LINES = 50

# `--- turns=10  in=15  out=2945  cost=$0.5230` — per-turn summary lines.
# The FINAL line emitted by run_initiative (after PR resolution) appends
# `  pr=N` when a PR was opened. Capture both turns/cost and optional pr.
# Floats + ints both accepted on cost.
_SUMMARY_RE = re.compile(
    r'^---\s*turns=(?P<turns>\d+)\s+in=\S+\s+out=\S+\s+cost=\$(?P<cost>\d+(?:\.\d+)?)'
    r'(?:\s+pr=(?P<pr>\d+))?',
    re.MULTILINE,
)

# D.5.1.4 — early-emit marker line written by ``run_initiative`` as soon as a
# PR URL appears in any tool result, BEFORE the long blocking waits. The final
# summary line is the authoritative source when present, but if the run exits
# abnormally (wait_for_terminal blocks past pod SIGTERM, SDK exception
# mid-loop) it may never be emitted — the marker is then the only log-side
# signal carrying pr=N, sparing us the GH-fallback subprocess call.
_PR_OPEN_RE = re.compile(r'^---\s*pr_open\s+pr=(?P<pr>\d+)', re.MULTILINE)


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


async def _fetch_pod_crash_info(core: client.CoreV1Api, namespace: str, run_id: str) -> tuple[str, str]:
    """Best-effort fetch of ``(exit_reason, log_tail)`` for a crashed pod.

    Used only on the ``terminal=='failed'`` branch to compose the crash sticky.
    The preStop hook in ``crash_sticky.py`` only fires on graceful SIGTERM —
    for hard crashes (OOMKilled, Error, ImagePullBackOff) the pod is killed
    before any user-defined hook runs, so this reconciler-side path is the
    only signal that surfaces the crash to the PR thread.

    Exit reason comes from ``pod.status.container_statuses[0].state.terminated.reason``
    which K8s populates for OOMKilled / Error / ContainerCannotRun / etc. We
    fall back to ``'unknown'`` if the field is missing — the sticky is still
    posted (log tail is the primary signal anyway).

    The log fetch is a SEPARATE call from ``_fetch_pod_log_tail`` because:

    * Crash logs need only the last 50 lines (PR comment readability) while
      summary parsing needs 200 (the ``--- turns=...`` line may be far back).
    * The failed-path is rare relative to the complete-path; the duplicate
      API call's cost is negligible and keeps the helper boundaries clean.

    Returns ``('unknown', '')`` on any failure rather than raising — the
    reconciler-row update still succeeds, the crash sticky just becomes
    partial / skipped. Better than blocking row-status patching on best-
    effort log fetches that may flake during cluster pressure.
    """
    exit_reason = 'unknown'
    log_text = ''
    try:
        pods = await core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f'leartech.io/run-id={run_id}',
        )
        if not pods.items:
            return exit_reason, log_text
        pod = pods.items[0]
        statuses = getattr(pod.status, 'container_statuses', None) or []
        if statuses:
            terminated = getattr(statuses[0].state, 'terminated', None) if statuses[0].state else None
            if terminated is not None:
                reason = getattr(terminated, 'reason', None)
                if reason:
                    exit_reason = reason
        try:
            log_text = await core.read_namespaced_pod_log(
                name=pod.metadata.name,
                namespace=namespace,
                tail_lines=CRASH_LOG_TAIL_LINES,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort log fetch
            logger.debug('reconciler: crash log fetch failed for %s: %s', run_id, exc)
    except Exception as exc:  # noqa: BLE001 — best-effort pod lookup
        logger.debug('reconciler: crash pod lookup failed for %s: %s', run_id, exc)
    return exit_reason, log_text


def _build_job_crash_sticky_body(*, run_id: str, exit_reason: str, log_tail: str) -> str:
    """Render the Job-pod crash sticky markdown.

    Different shape from ``gate.agent.initiative._build_crash_sticky_body`` —
    that one is posted in-process when the SDK loop raises (we have turn /
    cost context). This one is posted by the reconciler when the pod crashed
    hard before the SDK could report anything (no turn / cost context); we
    surface the K8s exit reason + the last 50 log lines instead.

    Marker is shared so future tooling can find both shapes via the same
    ``<!-- leartech-agent-run -->`` anchor.
    """
    stripped = log_tail.strip()
    tail_block = stripped if stripped else '(no log output captured)'
    return (
        '<!-- leartech-agent-run -->\n'
        '## ⚠ Agent Job pod crashed\n\n'
        f'**Run**: {run_id}\n\n'
        f'**Pod exit reason**: {exit_reason}\n\n'
        '**Last log lines**:\n\n'
        '```\n'
        f'{tail_block}\n'
        '```\n'
    )


def _lookup_pr_by_branch(qualified_repo: str, branch: str, run_id: str) -> int | None:
    """Fallback PR resolver — query GitHub by the initiative's branch.

    D.5.1.1 — the agent's ``wait_for_terminal`` may block until the pod is
    SIGTERM-killed before the final ``--- turns=...  pr=N`` summary line is
    emitted. Today every job-mode run completes with ``pr_number=None`` even
    though a PR actually exists, which means D.5.2's self_retrospect path
    silently skips ("pr_repo/pr_number not set"). Closing that gap is the
    point of this helper.

    D.5.1.2 — the original D.5.1.1 version derived the branch from
    ``f'agent/{initiative}'``. That convention doesn't match the YAML
    (e.g. initiative ``agent-f-default-job-drop-asyncio`` declares branch
    ``agent/f-default-job-drop-asyncio`` — the prefix gets doubled), so the
    fallback always missed. We now read the authoritative branch from the
    DB row (persisted at register time) and pass it in directly.

    ``gh pr list --head <branch> --state open`` returns the open PR for
    that branch if one exists. We pick the first (limit=1) since the
    convention is one PR per branch.

    Returns ``None`` on any failure — caller leaves ``pr_number`` as-is.
    Failures are logged at DEBUG (this is a best-effort enrichment; the row
    update has already committed the rest of the run state).
    """
    try:
        result = subprocess.run(
            [
                'gh',
                'pr',
                'list',
                '--repo',
                qualified_repo,
                '--head',
                branch,
                '--state',
                'open',
                '--json',
                'number',
                '--limit',
                '1',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort lookup
        logger.debug('reconciler: pr fallback lookup failed for %s: %s', run_id, exc)
        return None
    if result.returncode != 0:
        logger.debug(
            'reconciler: pr fallback lookup non-zero exit for %s: rc=%s stderr=%s',
            run_id,
            result.returncode,
            result.stderr.strip() if result.stderr else '',
        )
        return None
    try:
        rows = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as exc:
        logger.debug('reconciler: pr fallback json decode failed for %s: %s', run_id, exc)
        return None
    if not rows:
        return None
    number = rows[0].get('number')
    return int(number) if number is not None else None


def _parse_summary(log_text: str) -> tuple[int | None, float | None, int | None]:
    """Extract (turns, cost_usd, pr_number) from the trailing agent summary.

    The agent's `gate.agent.initiative` CLI emits a final summary line after
    PR resolution: `--- turns=X  in=...  out=...  cost=$W  pr=N` (pr=N is
    present only when a PR was opened). We pick the LAST `--- turns=` line
    so per-turn summaries don't overshadow the final authoritative one.

    D.5.1.4 — when the final summary is missing or lacks ``pr=N`` (e.g. the
    agent exited via SDK exception, or ``wait_for_terminal`` blocked past pod
    SIGTERM), we additionally consult the early-emit ``--- pr_open pr=N``
    marker. The summary's pr wins when both are present (same value in
    practice; precedence keeps the summary as the canonical contract); the
    marker is the fallback that turns the GH-side ``_lookup_pr_by_branch``
    subprocess call into a rare last resort instead of the steady-state path.
    """
    matches = list(_SUMMARY_RE.finditer(log_text))
    turns: int | None = None
    cost: float | None = None
    pr_number: int | None = None
    if matches:
        last = matches[-1]
        turns = int(last.group('turns'))
        cost = float(last.group('cost'))
        pr_raw = last.group('pr')
        if pr_raw:
            pr_number = int(pr_raw)
    if pr_number is None:
        marker = _PR_OPEN_RE.search(log_text)
        if marker:
            pr_number = int(marker.group('pr'))
    return turns, cost, pr_number


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
            # D.5.1.1 fallback — log parse missed `pr=N` (agent never emitted
            # the final post-PR-resolution summary line before pod termination).
            # Query GitHub by the initiative's declared branch (persisted on
            # the DB row by D.5.1.2) so the row still gets the pr_number it
            # would have had from the log. Without this, D.5.2's
            # self_retrospect silently skips every job-mode run.
            #
            # Skip the lookup entirely when the row has no branch
            # (`record.branch is None`) — that's a pre-D.5.1.2 row from
            # before the column existed; log-parse is all we have for those.
            if pr_number is None and record.pr_repo and record.branch:
                pr_number = _lookup_pr_by_branch(record.pr_repo, record.branch, run_id)
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
            # Post-success retrospective (D.5.2). The reconciler is the
            # only signal path for completion now (Phase F removed the
            # in-process asyncio path that previously fired this hook).
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
            # D.crash-detection — post a crash sticky on hard pod termination
            # (OOMKilled, Error, ImagePullBackOff, etc.). The preStop hook
            # only fires on graceful SIGTERM; hard crashes skip it entirely.
            # The reconciler is the only signal path for those, and we have
            # to do it here because the API pod no longer holds a task that
            # could observe the failure.
            #
            # Idempotency: same row-status short-circuit as retrospect — the
            # ``record.status in TERMINAL_STATUSES`` guard above ensures
            # each terminal Job triggers the sticky path at most once.
            if terminal == 'failed':
                # Prefer the freshly-parsed pr_number (from the same log we
                # just summarised) — that's the most up-to-date signal. Fall
                # back to whatever was already on the DB row in case parsing
                # missed it (e.g. agent opened the PR but crashed before the
                # final summary line landed in the tail window).
                pr_for_sticky = pr_number if pr_number is not None else record.pr_number
                pr_repo = record.pr_repo
                if pr_for_sticky is None or not pr_repo:
                    logger.info(
                        'reconciler: crash sticky skipped for %s — no PR resolved (pr=%s repo=%s)',
                        run_id,
                        pr_for_sticky,
                        pr_repo,
                    )
                else:
                    exit_reason, log_tail = await _fetch_pod_crash_info(core, namespace, run_id)
                    body = _build_job_crash_sticky_body(
                        run_id=run_id,
                        exit_reason=exit_reason,
                        log_tail=log_tail,
                    )
                    try:
                        _post_crash_sticky(
                            qualified_repo=pr_repo,
                            pr_number=pr_for_sticky,
                            body=body,
                        )
                    except Exception as exc:  # noqa: BLE001 — best-effort
                        logger.warning('reconciler: crash sticky post failed for %s: %s', run_id, exc)
    # V5 D2 — third pass: orphan `running` rows whose backing Job has
    # vanished. The V4 stall (Job 8b837153bfda deleted externally, DB row
    # stuck in `running` for 95 minutes) surfaced this gap: the Job-
    # iteration loop above only walks Jobs that still exist in K8s. A row
    # whose Job has been deleted (operator cleanup, TTL race, namespace
    # eviction) is invisible to that loop and stays in `running` forever.
    #
    # The fix mirrors `_enrich_cancelled_rows_missing_pr` shape: a separate
    # helper that walks the DB and detects orphans. Passes the live job-
    # name set captured above so a single namespace LIST satisfies both
    # passes — no extra K8s API calls.
    updates += await _orphan_running_rows_with_missing_jobs(jobs)

    # D.5.1.5 — second pass: enrich `cancelled` rows that finalised with
    # `pr_number=null`. The cancel endpoint (POST /initiatives/{id}/cancel)
    # deletes the K8s Job via Background propagation (gone immediately from
    # the API) AND writes `status='cancelled'` synchronously — so the Job-
    # iteration above never sees these runs. But the agent may have already
    # pushed + run `gh pr create` before SIGTERM (push+create typically
    # completes in ~30s, well inside the terminationGracePeriodSeconds=30
    # grace window), leaving an open PR with no link in the DB row.
    #
    # The D.5.1.1 fallback `_lookup_pr_by_branch` recovers the link from
    # GitHub's side by querying `gh pr list --head <branch>`. Run it for
    # every cancelled+missing-pr row whose pr_repo + branch are populated
    # (pre-migration NULL branches skipped, same contract as Job-iteration
    # path). Status stays `cancelled` — this only enriches metadata.
    #
    # Live hit example: leartech-orchestrator PR #4 (run 36465f844cc0,
    # 2026-05-30) — agent was cancelled mid-flight (dynamic-scan hang),
    # PR #4 existed + later merged, but the run row showed pr_number=null.
    # Backfilling that historical row is out of scope; future cancels are
    # covered by this fallback.
    updates += await _enrich_cancelled_rows_missing_pr()
    return updates


async def _orphan_running_rows_with_missing_jobs(jobs: list[Any]) -> int:
    """V5 D2 — flip `running` rows whose backing K8s Job has vanished.

    Sweep companion to the main Job-iteration loop in `reconcile_once`. The
    V4 stall (95-minute stuck row, Job ``8b837153bfda`` deleted externally)
    demonstrated that walking K8s Jobs alone is insufficient: when a Job
    disappears, the DB row is invisible to the loop and never reaches a
    terminal state.

    This helper takes the `jobs` list already retrieved by the loop (no
    extra K8s API call), derives the live job-name set, and flips any
    `running` row whose `job_name` is NOT in that set to `orphaned` with
    ``error='job_deleted_externally'``.

    Skip conditions:
      - DB not configured (in-memory store has no parallel sweep need)
      - row.runtime != 'job' (pre-Phase-F asyncio rows have no Job)
      - row.job_name falsy (defensive; new rows always carry one)

    Returns the count of rows orphaned. No-op (returns 0) when DB is
    disabled, mirroring `_enrich_cancelled_rows_missing_pr`'s contract.
    """
    if not is_db_enabled():
        return 0
    live_job_names = {job.metadata.name for job in jobs}
    async with db_session() as s:
        running = await list_runs(s, status='running')
    patched = 0
    for record in running:
        # Defensive attribute access — `InitiativeRunRecord` from the DB
        # always carries `status`/`runtime`/`job_name`, but some test
        # fixtures across this codebase mock `list_runs` with thinner
        # SimpleNamespace rows. Skipping cleanly on missing attrs (and
        # re-asserting `status == 'running'`) keeps this sweep safe to
        # add without forcing every downstream mock to grow new fields.
        if getattr(record, 'status', None) != 'running':
            continue
        if getattr(record, 'runtime', None) != 'job':
            continue
        job_name = getattr(record, 'job_name', None)
        if not job_name or job_name in live_job_names:
            continue
        await update(
            record.id,
            status='orphaned',
            finished_at=_now(),
            error='job_deleted_externally',
        )
        patched += 1
        logger.warning(
            'reconciler: V5 D2 orphaned %s — Job %s deleted externally (DB row stuck in running)',
            record.id,
            job_name,
        )
    return patched


async def _enrich_cancelled_rows_missing_pr() -> int:
    """Walk DB for cancelled rows missing pr_number; patch via GH lookup.

    Idempotency: once ``pr_number`` is set, the row falls out of the
    cancelled+missing-pr filter and the next pass skips it. A failed
    lookup (returns None) also no-ops — the row stays in the filter and
    we'll retry on the next pass, which is fine (GH `pr list` is cheap
    and cancellation is rare).

    Returns the count of rows patched. No-op (returns 0) when DB is not
    configured — the in-memory fallback store has no separate iteration
    path because tests targeting cancel always exercise the DB path.
    """
    if not is_db_enabled():
        return 0
    async with db_session() as s:
        cancelled = await list_runs(s, status='cancelled')
    patched = 0
    for record in cancelled:
        if record.pr_number is not None:
            continue
        if not record.pr_repo or not record.branch:
            continue
        pr_number = _lookup_pr_by_branch(record.pr_repo, record.branch, record.id)
        if pr_number is None:
            continue
        await update(record.id, pr_number=pr_number)
        patched += 1
        logger.info(
            'reconciler: D.5.1.5 enriched cancelled row %s with pr=%s (repo=%s branch=%s)',
            record.id,
            pr_number,
            record.pr_repo,
            record.branch,
        )
    return patched


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
