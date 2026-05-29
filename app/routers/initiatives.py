"""Initiative endpoints — start, status, list, cancel.

Phase B v2: async state ops wired through write-through-DB. Each
POST /initiatives spawns an asyncio.Task that runs
`gate.agent.initiative.run_initiative`. State lives in `app.state` —
durable in Postgres when `LEARTECH_INITIATIVE_DB_DSN` is set, in-memory
fallback when not (dev/CI/preview).

Catalog-first resolution (feat: catalog-fire-fallback):
  POST /initiatives checks the DB catalog FIRST (when `is_db_enabled()`),
  materialises the yaml_body to /tmp/agent-catalog/<name>.yaml, and passes
  that path to run_initiative. If not in DB, falls back to the baked-in
  filesystem initiatives/*.yaml. DB-stored entries WIN over same-named
  filesystem entries — DB is the live editable source of truth, filesystem
  is the "starter pack" from the current image.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from sqlite3 import ProgrammingError as SQLiteProgrammingError

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import ProgrammingError as SAProgrammingError

from app.db import is_db_enabled
from app.db import session as db_session
from app.db.initiative_catalog import get_initiative
from app.db.initiative_catalog import list_initiatives as list_db_initiatives
from app.state import (
    InitiativeRecord,
    list_records,
    new_id,
    now,
    register,
    update,
)
from app.state import (
    cancel as cancel_initiative_task,
)
from app.state import (
    get as get_record,
)
from gate.agent.initiative import run_initiative
from gate.agent.self_retrospect import (
    fetch_ai_review_verdict,
    fetch_gate_state,
    fetch_pr_diff,
    file_issue_with_findings,
    retrospect_after_ready,
)
from gate.initiatives.loader import Initiative, load_initiative, load_initiative_from_yaml

logger = logging.getLogger(__name__)

router = APIRouter()

# CLUSTER identifies which Kubernetes cluster this service instance runs on.
# Set via the chart's Deployment env — falls back to 'unknown' in local/CI runs.
_CLUSTER = os.environ.get('CLUSTER', 'unknown')

# Directory where DB-resolved initiatives are materialised so run_initiative
# can consume them as a plain Path.
_CATALOG_TMP_DIR = Path('/tmp/agent-catalog')  # noqa: S108 — intentional service-internal tmp dir

# Phase D.4 — dual-path runtime selector. 'asyncio' (default) keeps today's
# behaviour: the run lives inside the API pod's event loop. 'job' delegates
# to D.3's spawn_initiative_job — the run lives in its own K8s Job pod and
# survives API pod restarts. Read once per POST so a chart edit + rollout
# can flip a cluster without bouncing in-flight runs.
_RUNTIME_ENV = 'LEARTECH_INITIATIVE_RUNTIME'


def _current_runtime_mode() -> str:
    """Returns 'asyncio' or 'job' based on LEARTECH_INITIATIVE_RUNTIME.

    Unknown values fall back to 'asyncio' (the safe, today's-default path) —
    a typo in chart values should NOT silently switch a cluster to a path
    the operator didn't intend.
    """
    mode = os.environ.get(_RUNTIME_ENV, 'asyncio').lower()
    return mode if mode in {'asyncio', 'job'} else 'asyncio'


def _pick_image_for_initiative(initiative_name: str) -> str:
    """Return the container image to spawn for ``initiative_name``.

    D.4 stub: always returns the default variant image. Phase E.1 will
    add language-detection routing (Go/Python/Angular/Rust variants) by
    inspecting the initiative's primary repo. For now, one image fits
    every initiative — same as the asyncio path's behaviour today.

    Sourced from ``LEARTECH_INITIATIVE_DEFAULT_IMAGE``, which the chart
    deployment.yaml renders to ``image.repository:image.tag`` (the API
    pod's own image) by default — keeping API and Job code in lock-step.
    No silent hardcoded fallback: if the env var is unset, raise so the
    caller surfaces a 500 instead of spawning a Job that ErrImagePulls
    forever on a bogus default (D.4.2 incident on GCP).
    """
    _ = initiative_name  # unused until E.1
    image = os.environ.get('LEARTECH_INITIATIVE_DEFAULT_IMAGE')
    if not image:
        raise RuntimeError(
            'LEARTECH_INITIATIVE_DEFAULT_IMAGE is not set; chart deployment.yaml '
            'is supposed to render it from jobs.defaultImage or image.{repository,tag}. '
            'Cannot spawn Job pod without a real image reference.'
        )
    return image


# Plain env vars forwarded from the API pod into spawned Job pods.
# Each entry is the env-var name; values come from the API pod's own
# environment at spawn time. Keeping the list explicit (vs a wildcard)
# means a Job never inherits an accidental local variable that doesn't
# belong in the agent loop.
_JOB_FORWARDED_ENV_KEYS = (
    'LEARTECH_REPO_ROOT',
    'VERSION',
    'LEARTECH_AGENT_MODEL',
    'CLUSTER',
    'LEARTECH_INITIATIVES_DIR',
    'LEARTECH_AGENT_SELF_RETROSPECT',
)


def _initiative_env() -> dict[str, str]:
    """Plain env vars to propagate from the API pod into a spawned Job pod.

    Only non-sensitive configuration the agent loop reads at startup is
    forwarded. Secrets (API keys, DSNs) flow as `secret_refs` via
    :func:`_initiative_secret_refs` so the Job pod resolves them itself
    from the same K8s Secret the API pod uses.
    """
    return {k: os.environ[k] for k in _JOB_FORWARDED_ENV_KEYS if k in os.environ}


def _initiative_secret_refs() -> dict[str, dict[str, str]]:
    """Secret references the Job pod needs — same secrets the API pod reads.

    Defaults match the chart's ``secrets.*`` values; the API pod's
    Deployment can override per-cluster via the ``LEARTECH_JOB_*_SECRET_*``
    env vars (chart values flip these). Returns a mapping of
    ``ENV_VAR -> {secret, key}`` consumed verbatim by D.3's job_runner.
    """
    refs: dict[str, dict[str, str]] = {
        'ANTHROPIC_API_KEY': {
            'secret': os.environ.get('LEARTECH_JOB_ANTHROPIC_SECRET_NAME', 'ai-review-api-keys'),
            'key': os.environ.get('LEARTECH_JOB_ANTHROPIC_SECRET_KEY', 'CLAUDE_API_KEY'),
        },
        'GH_TOKEN': {
            'secret': os.environ.get('LEARTECH_JOB_GH_TOKEN_SECRET_NAME', 'tekton-git'),
            'key': os.environ.get('LEARTECH_JOB_GH_TOKEN_SECRET_KEY', 'password'),
        },
    }
    # Propagate the Postgres DSN only when the API pod knows about it
    # (gated on the chart's postgresql.enabled). Without it the Job
    # falls back to filesystem-only mode, same as the API pod.
    dsn_secret = os.environ.get('LEARTECH_JOB_DB_DSN_SECRET_NAME')
    dsn_key = os.environ.get('LEARTECH_JOB_DB_DSN_SECRET_KEY')
    if dsn_secret and dsn_key:
        refs['LEARTECH_INITIATIVE_DB_DSN'] = {'secret': dsn_secret, 'key': dsn_key}
    return refs


def _initiatives_dir() -> Path:
    """Where YAML initiatives live. Configurable via env in a later slice."""
    candidate = Path.cwd() / 'initiatives'
    if candidate.exists():
        return candidate
    raise HTTPException(
        status_code=500,
        detail=f'Initiatives directory not found at {candidate}. '
        'Set the working directory or wire LEARTECH_INITIATIVES_DIR (v1.5).',
    )


async def _resolve_yaml_path(name: str) -> Path | None:
    """Resolve an initiative by name — DB catalog first, filesystem fallback.

    DB-stored entries win over same-named filesystem entries: the DB is the
    live editable source of truth; the filesystem is the starter pack baked
    into each image release.

    When found in the DB the yaml_body is materialised to
    ``/tmp/agent-catalog/<name>.yaml`` (overwritten each call so a PUT via
    the catalog API is reflected on the next fire without any TTL games).

    Returns the Path to the YAML if found, or None if not found in either
    source. Never raises — callers map None to 404.
    """
    if is_db_enabled():
        try:
            async with db_session() as sess:
                record = await get_initiative(sess, name)
            if record is not None:
                _CATALOG_TMP_DIR.mkdir(parents=True, exist_ok=True)
                tmp_path = _CATALOG_TMP_DIR / f'{name}.yaml'
                tmp_path.write_text(record.yaml_body)
                return tmp_path
        except Exception:  # noqa: BLE001 — DB errors fall through to filesystem
            logger.warning('DB lookup for initiative %r failed; falling back to filesystem', name)

    # Filesystem fallback — also the only path when DB is disabled.
    try:
        fs_path = _initiatives_dir() / f'{name}.yaml'
    except HTTPException:
        return None
    return fs_path if fs_path.exists() else None


async def _available_names() -> list[str]:
    """Return all known initiative names — DB union filesystem, sorted.

    Used to populate the 404 detail so callers know what's available without
    a separate discovery call.
    """
    names: set[str] = set()
    if is_db_enabled():
        try:
            async with db_session() as sess:
                records = await list_db_initiatives(sess)
            names.update(r.name for r in records)
        except Exception:  # noqa: BLE001 — best-effort; don't crash a 404 response
            logger.warning('Could not list DB initiatives for 404 detail')
    try:
        fs_dir = _initiatives_dir()
        names.update(p.stem for p in fs_dir.glob('*.yaml') if not p.stem.startswith('_'))
    except HTTPException:
        pass
    return sorted(names)


class StartInitiativeRequest(BaseModel):
    initiative: str = Field(..., description='Initiative YAML name (without .yaml)')


async def _run_self_retrospect(initiative_id: str) -> None:
    """Post-success retrospective: ask the LLM what we should have caught locally.

    Gated by ``LEARTECH_AGENT_SELF_RETROSPECT`` (default ``true``; set to
    ``false`` to disable per-cluster via chart values). Non-blocking —
    any failure here is swallowed because the PR is already
    merge-eligible; the retrospective is enrichment, not part of the
    success criterion.

    Reads the final state of the run record (post-update) to recover
    pr_repo + pr_number, then calls into ``gate.agent.self_retrospect``.
    """
    if os.environ.get('LEARTECH_AGENT_SELF_RETROSPECT', 'true').lower() != 'true':
        logger.info('self_retrospect disabled via LEARTECH_AGENT_SELF_RETROSPECT — skipping')
        return

    record = await get_record(initiative_id)
    if record is None or record.pr_repo is None or record.pr_number is None:
        logger.info(
            'self_retrospect skipped for %s: pr_repo/pr_number not set (record=%s)',
            initiative_id,
            record,
        )
        return

    pr_diff = await fetch_pr_diff(record.pr_repo, record.pr_number)
    if not pr_diff:
        logger.info('self_retrospect skipped for %s: empty PR diff', initiative_id)
        return

    ai_review = await fetch_ai_review_verdict(record.pr_repo, record.pr_number)
    gate_state = await fetch_gate_state(record.pr_repo, record.pr_number)

    findings = await retrospect_after_ready(
        pr_repo=record.pr_repo,
        pr_number=record.pr_number,
        pr_diff=pr_diff,
        ai_review_verdict=ai_review,
        final_gate_state=gate_state,
    )
    if not findings:
        logger.info('self_retrospect for %s: no actionable findings', initiative_id)
        return

    issue_url = await file_issue_with_findings(
        pr_repo=record.pr_repo,
        pr_number=record.pr_number,
        findings=findings,
    )
    if issue_url:
        logger.info('self_retrospect filed Issue for %s: %s', initiative_id, issue_url)


async def _run_and_track(initiative_id: str, yaml_path: Path) -> None:
    """Background task body — runs the initiative, updates state on completion."""
    await update(initiative_id, status='running')
    try:
        summary = await run_initiative(yaml_path)
        # Re-fetch the record so we can preserve pr_repo through the
        # completion update. pr_repo is set at register time from
        # loaded.primary.qualified_repo, but the primary fix lives in
        # app.state.register (now passes it to create_run at INSERT). This
        # is a belt-and-braces preservation: if some future code path
        # nulls pr_repo or starts the row without it, the completion
        # update still carries the in-memory value through. Do NOT drop
        # this — the self_retrospect hook needs pr_repo + pr_number on
        # the final record to file Issues (regression on run
        # 44120e445abd 2026-05-28: pr_repo arrived None and every run
        # skipped retrospect).
        current = await get_record(initiative_id)
        pr_repo = current.pr_repo if current is not None else None
        await update(
            initiative_id,
            status='complete' if summary.exit_code == 0 else 'failed',
            finished_at=now(),
            turns=summary.turns,
            cost_usd=summary.cost_usd,
            pr_number=summary.pr_number,
            pr_repo=pr_repo,
        )
        logger.info('initiative %s finished with exit_code=%d', initiative_id, summary.exit_code)
        # Post-success retrospective — non-blocking, best-effort. See
        # gate/agent/self_retrospect.py for the rationale (PR #46/#1
        # incident 2026-05-28).
        #
        # The success status was written above, so we must NOT let a
        # cancellation in the retrospect propagate to the outer
        # CancelledError handler — that would re-mark this 'complete'
        # run as 'cancelled' just because TestClient/pod teardown
        # interrupted the enrichment. Swallow CancelledError here
        # (the only place this exception to "always re-raise" is OK,
        # because the run itself already succeeded).
        if summary.exit_code == 0:
            try:
                await _run_self_retrospect(initiative_id)
            except asyncio.CancelledError:
                logger.info('self_retrospect cancelled for %s — status already complete', initiative_id)
            except Exception as exc:  # noqa: BLE001 — must never affect main flow
                logger.warning('self_retrospect failed (non-blocking) for %s: %s', initiative_id, exc)
    except asyncio.CancelledError:
        # Best-effort: if the engine is gone (pod shutdown raced with cancellation),
        # the next pod's orphan-detection on startup will mark this run terminal —
        # don't crash here. Two disposal surfaces are tolerated:
        #   * RuntimeError — session factory was cleared (PR #35).
        #   * (SA|SQLite)ProgrammingError — engine connection is closed mid-cleanup
        #     under cluster contention (GCP release tb8t6, 2026-05-27).
        try:
            await update(initiative_id, status='cancelled', finished_at=now())
        except (RuntimeError, SAProgrammingError, SQLiteProgrammingError) as exc:
            logger.warning(
                'cancel-cleanup skipped — DB engine already disposed (will be marked orphaned on next pod start): %s',
                exc,
            )
        logger.info('initiative %s cancelled', initiative_id)
        raise
    except Exception as exc:  # noqa: BLE001 — we want to surface any agent failure to the consumer
        await update(initiative_id, status='failed', error=str(exc), finished_at=now())
        logger.exception('initiative %s failed', initiative_id)


@router.post('', response_model=InitiativeRecord, status_code=202)
async def start_initiative(request: StartInitiativeRequest) -> InitiativeRecord:
    """Validate the initiative YAML and spawn a background task to execute it.

    Resolution order: DB catalog first (when LEARTECH_INITIATIVE_DB_DSN is set),
    filesystem fallback. DB-stored entries win over same-named filesystem entries.

    Returns 202 with the initial record (status=queued). Poll
    GET /initiatives/{id} for terminal status.
    """
    yaml_path = await _resolve_yaml_path(request.initiative)
    if yaml_path is None:
        available = await _available_names()
        raise HTTPException(
            status_code=404,
            detail={'message': f'Initiative {request.initiative!r} not found', 'available': available},
        )

    try:
        loaded = load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc

    initiative_id = new_id()
    runtime_mode = _current_runtime_mode()

    if runtime_mode == 'job':
        # Phase D.4 — Job-per-run path. The run executes in its own K8s
        # Job pod and survives API pod restarts. The structural fix for
        # pod-restart-kills-run.
        #
        # We do not create an asyncio.Task here: there's nothing to await
        # locally. The Job's lifecycle is owned by K8s; status reconciliation
        # back into the DB is D.5's surface (a watcher that observes Job
        # terminal state and patches initiative_runs.status).
        from gate.agent.job_runner import spawn_initiative_job

        namespace = os.environ.get('POD_NAMESPACE')
        if not namespace:
            raise HTTPException(
                status_code=500,
                detail='POD_NAMESPACE env var is required when '
                f'{_RUNTIME_ENV}=job; chart deployment must inject it via '
                'fieldRef metadata.namespace.',
            )
        try:
            # Inline the YAML body so the Job pod doesn't need DB access
            # to resolve the initiative. yaml_path was just loaded above so
            # the read is essentially free (filesystem cache hot).
            job_name, _ns = await spawn_initiative_job(
                initiative_name=request.initiative,
                run_id=initiative_id,
                image=_pick_image_for_initiative(request.initiative),
                namespace=namespace,
                env=_initiative_env(),
                secret_refs=_initiative_secret_refs(),
                yaml_body=yaml_path.read_text(),
                # D.7 — propagate qualified repo so the Job's preStop hook
                # can post a "cancelled" sticky to the PR when an operator
                # cancels mid-run. The agent itself writes /tmp/run_pr_number
                # once it resolves the PR; the hook reads from there.
                pr_repo=loaded.primary.qualified_repo,
            )
        except Exception as exc:  # noqa: BLE001 — surface spawn failures as 502 so the consumer sees them
            logger.exception('Job spawn failed for initiative %s', initiative_id)
            raise HTTPException(status_code=502, detail=f'Failed to spawn initiative Job: {exc}') from exc
        record = InitiativeRecord(
            id=initiative_id,
            initiative=request.initiative,
            status='queued',
            started_at=now(),
            pr_repo=loaded.primary.qualified_repo,
            cluster=_CLUSTER,
            runtime='job',
            job_name=job_name,
        )
        await register(record, task=None)
        logger.info(
            'initiative %s queued (runtime=job): %s — Job %s/%s',
            initiative_id,
            request.initiative,
            namespace,
            job_name,
        )
        return record

    # Default asyncio path — unchanged from pre-D.4 behaviour.
    record = InitiativeRecord(
        id=initiative_id,
        initiative=request.initiative,
        status='queued',
        started_at=now(),
        pr_repo=loaded.primary.qualified_repo,
        cluster=_CLUSTER,
        runtime='asyncio',
    )
    task = asyncio.create_task(_run_and_track(initiative_id, yaml_path))
    await register(record, task)
    logger.info('initiative %s queued (runtime=asyncio): %s', initiative_id, request.initiative)
    return record


@router.get('', response_model=list[InitiativeRecord])
async def list_initiatives() -> list[InitiativeRecord]:
    """List all initiatives this process has seen — running, complete, or terminal."""
    return await list_records()


@router.get('/{initiative_id}', response_model=InitiativeRecord)
async def get_initiative_status(initiative_id: str) -> InitiativeRecord:
    """Get current status of a queued / running / completed initiative."""
    record = await get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')
    return record


@router.get('/{initiative_id}/logs', response_class=PlainTextResponse)
async def get_initiative_logs(initiative_id: str, tail_lines: int = 500) -> PlainTextResponse:
    """Tail logs for a run.

    Phase D.6 — surfaces the spawned Job pod's stdout/stderr through the same
    API as the catalog so operators don't need direct ``kubectl logs`` access
    via ``scripts/tail_agent_log.sh``.

    For ``runtime=job`` runs: looks up the run's pod via the
    ``leartech.io/run-id=<id>`` label in ``POD_NAMESPACE`` and streams the
    tail back as text/plain. Uses the same in-cluster credentials the
    job_reconciler uses (D.5) — the API pod's ServiceAccount is bound to
    the job-runner Role which already grants ``pods/log get``.

    For ``runtime=asyncio`` runs: returns 501. These runs share the API
    pod's stdout/stderr so per-run isolation isn't possible from here;
    operators can use ``scripts/tail_agent_log.sh --run <id>`` directly
    against the API pod.

    NOTE: ``kubernetes_asyncio.config.load_incluster_config`` is synchronous
    in this library (do NOT await it). The async sibling is
    ``load_kube_config`` (file-based). See PR #50 root cause + the
    ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.
    """
    record = await get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    if record.runtime != 'job':
        raise HTTPException(
            status_code=501,
            detail=(
                f'Log streaming endpoint only supports runtime=job runs '
                f'(this run is runtime={record.runtime!r}). For asyncio-mode '
                'runs, use scripts/tail_agent_log.sh --run <id> against the API pod.'
            ),
        )

    namespace = os.environ.get('POD_NAMESPACE')
    if not namespace:
        raise HTTPException(
            status_code=500,
            detail='POD_NAMESPACE env var is required to read Job pod logs; '
            'chart deployment must inject it via fieldRef metadata.namespace.',
        )

    # Late import: kubernetes_asyncio is only needed on this code path; keeps
    # `from app.routers.initiatives import ...` cheap for in-process tests
    # that don't exercise the logs endpoint.
    from kubernetes_asyncio import client as k8s_client
    from kubernetes_asyncio import config as k8s_config
    from kubernetes_asyncio.client.api_client import ApiClient

    k8s_config.load_incluster_config()  # synchronous — do NOT await
    try:
        async with ApiClient() as api:
            core = k8s_client.CoreV1Api(api)
            pods = await core.list_namespaced_pod(
                namespace=namespace,
                label_selector=f'leartech.io/run-id={initiative_id}',
            )
            if not pods.items:
                raise HTTPException(
                    status_code=404,
                    detail=f'No pod found for run-id {initiative_id!r} in namespace {namespace!r}',
                )
            pod_name = pods.items[0].metadata.name
            log_text: str = await core.read_namespaced_pod_log(
                name=pod_name,
                namespace=namespace,
                tail_lines=tail_lines,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface K8s API failures as 502 so operators see them
        logger.exception('logs fetch failed for initiative %s', initiative_id)
        raise HTTPException(status_code=502, detail=f'Failed to read pod logs: {exc}') from exc

    return PlainTextResponse(content=log_text)


@router.post('/{initiative_id}/cancel', response_model=InitiativeRecord)
async def cancel_initiative(initiative_id: str) -> InitiativeRecord:
    """Request cancellation of a running initiative. Idempotent for terminal records.

    Phase D.7 — dual-path cancel.

    For ``runtime=asyncio`` runs (the historical path) we still call
    ``app.state.cancel`` which targets the in-process asyncio.Task; the
    ``_run_and_track`` CancelledError handler writes the terminal status.

    For ``runtime=job`` runs the API pod holds no in-process task — the run
    lives in its own K8s Job pod. We delete the Job (propagationPolicy
    Background so the pod is GC'd asynchronously); K8s will SIGTERM the
    pod, give it ``terminationGracePeriodSeconds`` to write the preStop
    "cancelled" sticky comment to the PR (see D.7 preStop hook in
    ``gate/agent/job_runner.py``), then kill it. We immediately write the
    cancelled status to the DB so the next ``GET`` reflects the operator's
    intent — the reconciler (D.5) sees the now-terminal row and skips it,
    so the brief race where the Job has gone but the pod is still posting
    its preStop sticky does NOT mark the row 'failed'.

    NOTE: ``kubernetes_asyncio.config.load_incluster_config`` is synchronous
    in this library (do NOT await it). Same pattern as the logs endpoint
    and ``spawn_initiative_job``; see PR #50 + the
    ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.
    """
    record = await get_record(initiative_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    if record.status in {'cancelled', 'complete', 'failed', 'orphaned', 'timed_out'}:
        # Idempotent: terminal records short-circuit. Mirrors the asyncio
        # path's behaviour before D.7 (cancel_initiative_task returned False
        # but the 409 was suppressed for terminal records).
        return record

    if record.runtime == 'job':
        # Delete the K8s Job — Kubernetes propagates SIGTERM to the pod,
        # respects terminationGracePeriodSeconds (set on the Job manifest by
        # D.7), then kills. The preStop hook posts a "cancelled" sticky to
        # the PR (when one was resolved by the mid-run write to
        # /tmp/run_pr_number) before the pod is GC'd.
        namespace = os.environ.get('POD_NAMESPACE')
        if not namespace:
            raise HTTPException(
                status_code=500,
                detail='POD_NAMESPACE env var is required to cancel runtime=job runs; '
                'chart deployment must inject it via fieldRef metadata.namespace.',
            )
        if not record.job_name:  # pragma: no cover — invariant: runtime=job ⇒ job_name set at register()
            raise HTTPException(
                status_code=500,
                detail=f'runtime=job record {initiative_id!r} is missing job_name; cannot delete K8s Job.',
            )

        # Late import: kubernetes_asyncio is only needed on this code path; keeps
        # `from app.routers.initiatives import ...` cheap for in-process tests.
        from kubernetes_asyncio import client as k8s_client
        from kubernetes_asyncio import config as k8s_config
        from kubernetes_asyncio.client.api_client import ApiClient
        from kubernetes_asyncio.client.exceptions import ApiException

        k8s_config.load_incluster_config()  # synchronous — do NOT await
        try:
            async with ApiClient() as api:
                batch = k8s_client.BatchV1Api(api)
                await batch.delete_namespaced_job(
                    name=record.job_name,
                    namespace=namespace,
                    propagation_policy='Background',
                )
        except ApiException as exc:
            # 404 means the Job is already gone (TTL'd out, or a prior
            # cancel + delete completed). Treat as success — the operator's
            # intent ("this run should be cancelled") is already satisfied.
            if exc.status != 404:
                logger.exception('Job delete failed for initiative %s', initiative_id)
                raise HTTPException(status_code=502, detail=f'Failed to delete initiative Job: {exc}') from exc
            logger.info(
                'initiative %s: Job %s already absent — recording cancelled status', initiative_id, record.job_name
            )
        except Exception as exc:  # noqa: BLE001 — surface K8s API failures as 502
            logger.exception('Job delete failed for initiative %s', initiative_id)
            raise HTTPException(status_code=502, detail=f'Failed to delete initiative Job: {exc}') from exc

        # Synchronously write terminal status. The reconciler (D.5) sees
        # this on its next pass and skips the run — no race where it sees
        # the Job gone and writes 'failed'.
        await update(initiative_id, status='cancelled', finished_at=now())
        logger.info(
            'initiative %s cancelled (runtime=job): Job %s/%s deleted', initiative_id, namespace, record.job_name
        )

        refreshed = await get_record(initiative_id)
        if refreshed is None:  # pragma: no cover — record was just confirmed above
            raise HTTPException(status_code=500, detail='Record disappeared between get and refresh')
        return refreshed

    # runtime=asyncio path — unchanged from pre-D.7 behaviour.
    cancelled = await cancel_initiative_task(initiative_id)
    if not cancelled and record.status not in {'cancelled', 'complete', 'failed'}:
        raise HTTPException(
            status_code=409,
            detail=f'Initiative {initiative_id!r} is in status {record.status!r}; cannot cancel',
        )

    refreshed = await get_record(initiative_id)
    if refreshed is None:  # pragma: no cover — record was just confirmed above
        raise HTTPException(status_code=500, detail='Record disappeared between get and refresh')
    return refreshed


def _summary_of(loaded: Initiative) -> dict[str, object]:
    """Build the validation summary dict for an Initiative.

    Shared by the name-based GET /_validate/{name} and body-based
    POST /_validate endpoints so both routes return identical shapes.
    Returns a plain dict rather than the Initiative model itself because
    the model carries both new (`repos: [...]`) and legacy
    (`repo`, `branch`, `base`) shapes after normalization, which trips
    re-validation when re-serialized.
    """
    primary = loaded.primary
    return {
        'name': loaded.name,
        'description': loaded.description,
        'repos': [{'repo': r.repo, 'branch': r.branch, 'base': r.base} for r in loaded.repos],
        'primary': {'repo': primary.repo, 'branch': primary.branch, 'base': primary.base},
        'gate_marks': loaded.gate_marks,
        'max_iterations': loaded.max_iterations,
    }


@router.get('/_validate/{initiative}')
async def validate_initiative(initiative: str) -> dict[str, object]:
    """Resolve and parse an initiative YAML by name, returning a summary dict.

    No side effects. Useful for callers (Tekton task, CRD controller) to
    verify YAML correctness before POST.
    """
    yaml_path = await _resolve_yaml_path(initiative)
    if yaml_path is None:
        raise HTTPException(status_code=404, detail=f'Initiative {initiative!r} not found')
    try:
        loaded = load_initiative(yaml_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
    return _summary_of(loaded)


@router.post('/_validate')
async def validate_initiative_body(body: str = Body(..., media_type='text/plain')) -> dict[str, object]:
    """Parse + validate an initiative YAML body, returning the same summary
    shape as ``GET /_validate/{name}``.

    No side effects — does NOT register the initiative in the catalog or
    spawn any background task. Useful for pre-flight validation of a draft
    YAML body before POSTing to ``/initiatives/catalog`` or ``/initiatives``,
    so operators don't need to spin up the Python loader locally.
    """
    if not body or not body.strip():
        raise HTTPException(status_code=422, detail='Invalid initiative YAML: empty body')
    try:
        loaded = load_initiative_from_yaml(body)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
    return _summary_of(loaded)
