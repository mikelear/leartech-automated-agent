"""Initiative endpoints — start, status, list, cancel.

Phase F: every POST /initiatives spawns a K8s Job pod (Job-per-run is
the only runtime now; the in-process asyncio task path was removed in
this phase). State lives in `app.state` — durable in Postgres when
`LEARTECH_INITIATIVE_DB_DSN` is set, in-memory fallback when not
(dev/CI/preview).

Catalog-first resolution (feat: catalog-fire-fallback):
  POST /initiatives checks the DB catalog FIRST (when `is_db_enabled()`),
  materialises the yaml_body to /tmp/agent-catalog/<name>.yaml, and passes
  that path to the spawned Job pod. If not in DB, falls back to the
  baked-in filesystem initiatives/*.yaml. DB-stored entries WIN over
  same-named filesystem entries — DB is the live editable source of
  truth, filesystem is the "starter pack" from the current image.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator

from app.auth import AuthenticatedUser, get_current_tenant_id, require_service_caller
from app.db import is_db_enabled
from app.db import session as db_session
from app.db.agent_run_commands import (
    AgentRunCommandRecord,
    UnknownCommandTypeError,
    insert_command,
    list_commands,
)
from app.db.initiative_catalog import get_initiative
from app.db.initiative_catalog import list_initiatives as list_db_initiatives
from app.db.models import AGENT_RUN_COMMAND_TYPES
from app.state import (
    InitiativeRecord,
    list_records,
    new_id,
    now,
    register,
    update,
)
from app.state import (
    get as get_record,
)
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

# Directory where DB-resolved initiatives are materialised so the spawned
# Job pod can consume them as a plain Path.
_CATALOG_TMP_DIR = Path('/tmp/agent-catalog')  # noqa: S108  # nosec B108 — intentional service-internal tmp dir


# Phase E.1 — known language hints. Each entry maps the language string
# (as it appears in initiative YAML or as detected from repo manifests) to
# the short image name in the leartech-agent fleet. Full image URL is
# composed at spawn time via :func:`_compose_image_url` using the chart-
# rendered LEARTECH_JOB_IMAGE_REGISTRY_PREFIX + LEARTECH_JOB_IMAGE_TAG
# env vars. Adding a new language is one new entry here + a matching
# image in leartech-dockerfiles. Unknown languages return None and the
# caller falls back to LEARTECH_INITIATIVE_DEFAULT_IMAGE.
_LANGUAGE_TO_IMAGE: dict[str, str] = {
    # Short suffixes match what leartech-dockerfiles actually builds —
    # `leartech-agent-py` (not `-python`), `leartech-agent-ng` (not `-angular`).
    # `node` aliases to the angular image since the same Node toolchain
    # underpins both. Dotnet has no published image yet; falls back to
    # LEARTECH_INITIATIVE_DEFAULT_IMAGE via the None-return path.
    'go': 'leartech-agent-go',
    'python': 'leartech-agent-py',
    'py': 'leartech-agent-py',
    'angular': 'leartech-agent-ng',
    'node': 'leartech-agent-ng',
    'ng': 'leartech-agent-ng',
    'rust': 'leartech-agent-rust',
}

# Module-level cache for repo -> detected-language results, populated by
# :func:`_detect_language_from_repo`. Keyed by ``qualified_repo`` (e.g.
# ``mikelear/foo``). Caches BOTH positive hits (e.g. ``'go'``) and confirmed
# negatives (``None`` — manifests not recognised) so the GitHub API isn't
# re-hit on every POST for repos we've already classified. Transient
# fetch failures are NOT cached (they return None without recording, so a
# follow-up call retries). Cache survives the process lifetime; a pod
# restart re-fetches once per repo.
_LANGUAGE_CACHE: dict[str, str | None] = {}


def _image_for_language(language: str) -> str | None:
    """Map a language hint to a leartech-agent short image name.

    Returns the SHORT image name (no registry prefix, no tag) so the caller
    composes the final URL with :func:`_compose_image_url`. Unknown / empty
    languages return ``None``; the caller falls back to the env default.
    The lookup is case-insensitive — initiative YAML authors sometimes
    write ``Python`` or ``GO``.
    """
    if not language:
        return None
    return _LANGUAGE_TO_IMAGE.get(language.strip().lower())


def _gh_api_list_repo_root(qualified_repo: str) -> list[str] | None:
    """List the file names at the root of ``qualified_repo`` via ``gh api``.

    Returns ``None`` on any failure (auth, network, repo not found,
    timeout, malformed JSON) so the caller can fall back cleanly. The
    happy path returns the list of ``name`` strings from the GitHub
    Contents API. We shell out to ``gh`` rather than calling the REST
    endpoint directly to inherit the same token-resolution + retry path
    the rest of the agent uses (``GH_TOKEN`` env or ``gh auth``).
    """
    # `gh` is on PATH in the API pod image (same approach used across
    # gate/agent/self_retrospect.py and gate/agent/initiative.py); the
    # qualified_repo string is constrained upstream by the loader to
    # ``<owner>/<name>`` shape so the URL path is safe. S603 / S607 are
    # suppressed for this whole module in pyproject.toml.
    try:
        result = subprocess.run(
            [
                'gh',
                'api',
                f'repos/{qualified_repo}/contents/',
                '--jq',
                '[.[] | .name]',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning('_detect_language: gh api errored for %r: %s', qualified_repo, exc)
        return None
    if result.returncode != 0:
        logger.warning(
            '_detect_language: gh api failed for %r (exit %d): %s',
            qualified_repo,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    try:
        payload = json.loads(result.stdout or '[]')
    except json.JSONDecodeError as exc:
        logger.warning('_detect_language: gh api returned non-JSON for %r: %s', qualified_repo, exc)
        return None
    if not isinstance(payload, list):
        return None
    return [str(name) for name in payload]


def _detect_language_from_repo(qualified_repo: str) -> str | None:
    """Sniff manifest files at the repo root to guess the primary language.

    Detection order (highest priority first — the first hit wins):

    1. ``go.mod`` → ``'go'``
    2. ``pyproject.toml`` → ``'python'``
    3. ``requirements.txt`` → ``'python'``
    4. ``package.json`` + ``angular.json`` → ``'angular'``
    5. ``package.json`` (no ``angular.json``) → ``'node'``
    6. ``Cargo.toml`` → ``'rust'``
    7. any ``*.csproj`` → ``'dotnet'``

    Returns ``None`` when no recognised manifest is present OR when the
    GitHub API call fails. Results are cached in :data:`_LANGUAGE_CACHE`
    so repeated POSTs for the same repo are free. Fetch failures (the API
    returned ``None``) are NOT cached — the next call retries.
    """
    if qualified_repo in _LANGUAGE_CACHE:
        return _LANGUAGE_CACHE[qualified_repo]

    names = _gh_api_list_repo_root(qualified_repo)
    if names is None:
        # Transient: don't cache — let the next call retry.
        return None

    names_set = set(names)
    detected: str | None = None
    if 'go.mod' in names_set:
        detected = 'go'
    elif 'pyproject.toml' in names_set:
        detected = 'python'
    elif 'requirements.txt' in names_set:
        detected = 'python'
    elif 'package.json' in names_set:
        detected = 'angular' if 'angular.json' in names_set else 'node'
    elif 'Cargo.toml' in names_set:
        detected = 'rust'
    elif any(n.endswith('.csproj') for n in names):
        detected = 'dotnet'

    _LANGUAGE_CACHE[qualified_repo] = detected
    return detected


def _compose_image_url(short_image: str) -> str | None:
    """Compose ``<prefix>/<short_image>:<tag>`` from chart-rendered env vars.

    ``LEARTECH_JOB_IMAGE_REGISTRY_PREFIX`` and ``LEARTECH_JOB_IMAGE_TAG`` are
    rendered by the chart's ``deployment.yaml`` from the same expression
    used for ``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` (the API image's
    repository minus the ``/leartech-automated-agent`` suffix, and the
    Chart.Version respectively). Returns ``None`` when either is unset so
    the caller falls back to ``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` — this
    handles the rollout gap where new code runs against an older chart.
    """
    prefix = os.environ.get('LEARTECH_JOB_IMAGE_REGISTRY_PREFIX')
    tag = os.environ.get('LEARTECH_JOB_IMAGE_TAG')
    if not prefix or not tag:
        return None
    return f'{prefix.rstrip("/")}/{short_image}:{tag}'


def _pick_image_for_initiative(
    initiative_name: str,
    language: str | None = None,
    image_override: str | None = None,
    qualified_repo: str | None = None,
) -> str:
    """Return the container image to spawn for ``initiative_name``.

    Precedence (highest to lowest):

    1. ``image_override`` (Phase E.3) — fully-qualified image ref from the
       initiative YAML's ``image:`` field. Short-circuits all other paths.
    2. ``language`` (Phase E.2) — explicit language hint from the
       initiative YAML's ``language:`` field. Routes to the matching
       ``leartech-agent-<lang>`` image.
    3. Repo manifest auto-detect (Phase E.1) — sniff the primary repo's
       root for ``go.mod`` / ``pyproject.toml`` / ``package.json`` / etc.
    4. ``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` env (D.4.4 default).

    The ``LEARTECH_INITIATIVE_DEFAULT_IMAGE`` env is rendered by the chart
    deployment.yaml from ``image.repository:image.tag`` (the API pod's own
    image) — keeping API and Job code in lock-step. No silent hardcoded
    fallback: if the env var is unset AND no override / language applies,
    raise so the caller surfaces a 500 instead of spawning a Job that
    ErrImagePulls forever on a bogus default (D.4.2 incident on GCP).

    Layers 2/3 require ``LEARTECH_JOB_IMAGE_REGISTRY_PREFIX`` and
    ``LEARTECH_JOB_IMAGE_TAG`` to be set (chart-rendered). If either is
    missing — e.g. running with an older chart release — we degrade to
    the env default rather than failing, with a warning. This is the
    same rollout-gap pattern D.4.4 uses.
    """
    _ = initiative_name  # accepted so callers stay stable; future routing may consume it

    # Phase E.3 — per-initiative override wins over everything else.
    # Empty string is normalised to None by the loader, but be defensive
    # against callers that bypass the loader (e.g. constructing the model
    # directly in tests). A whitespace-only override falls through.
    if image_override and image_override.strip():
        return image_override

    # Resolve language: explicit hint (E.2) wins over repo auto-detect (E.1).
    detected_language: str | None = None
    if language and language.strip():
        detected_language = language.strip().lower()
    elif qualified_repo:
        detected_language = _detect_language_from_repo(qualified_repo)

    if detected_language:
        short = _image_for_language(detected_language)
        if short:
            composed = _compose_image_url(short)
            if composed:
                return composed
            # Chart-rollout gap — new env vars not rendered yet; degrade
            # to the default image so the spawn succeeds instead of
            # raising. The operator sees the warning and bumps the chart.
            logger.warning(
                '_pick_image_for_initiative: language %r resolved to %r but '
                'LEARTECH_JOB_IMAGE_REGISTRY_PREFIX / LEARTECH_JOB_IMAGE_TAG '
                'are not set; falling back to LEARTECH_INITIATIVE_DEFAULT_IMAGE',
                detected_language,
                short,
            )

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
    # Gateway repoint (Phase 1): when the API pod is pointed at leartech-ai-gateway
    # via ANTHROPIC_BASE_URL, spawned plan-runner Jobs MUST inherit it too — else
    # the Job's Claude Agent SDK / anthropic client calls Anthropic directly,
    # unmetered and ungoverned. The virtual-key VALUE flows as a secret_ref
    # (ANTHROPIC_API_KEY, see _initiative_secret_refs); this forwards only the
    # non-secret base-URL. Absent (direct-Anthropic clusters) → nothing forwarded,
    # Job behaves exactly as before. See AI-GATEWAY-AND-PORTABILITY.md.
    'ANTHROPIC_BASE_URL',
    # Tool-model overrides (spec_suggester / video_review) so a repointed Job
    # runs those raw-anthropic helpers through the gateway on the same logical
    # models as the API pod. Harmless when unset.
    'LEARTECH_SPEC_SUGGESTER_MODEL',
    'LEARTECH_VIDEO_REVIEW_MODEL',
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


async def _resolve_yaml_path(name: str, *, tenant_id: str | None = None) -> Path | None:
    """Resolve an initiative by name — DB catalog first, filesystem fallback.

    DB-stored entries win over same-named filesystem entries: the DB is the
    live editable source of truth; the filesystem is the starter pack baked
    into each image release.

    v7-P1 step 5 — when ``tenant_id`` is set, only entries visible to that
    tenant resolve (tenant's own + global). Cross-tenant lookups fall
    through to the filesystem (which is itself tenant-agnostic) — if the
    name also isn't on disk the caller maps None to 404.

    When found in the DB the yaml_body is materialised to
    ``/tmp/agent-catalog/<name>.yaml`` (overwritten each call so a PUT via
    the catalog API is reflected on the next fire without any TTL games).

    Returns the Path to the YAML if found, or None if not found in either
    source. Never raises — callers map None to 404.
    """
    if is_db_enabled():
        try:
            async with db_session() as sess:
                record = await get_initiative(sess, name, tenant_id=tenant_id)
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


async def _available_names(*, tenant_id: str | None = None) -> list[str]:
    """Return all known initiative names — DB union filesystem, sorted.

    v7-P1 step 5 — when ``tenant_id`` is set, the DB half is filtered to
    only that tenant's own + global entries. Filesystem entries are
    always included (tenant-agnostic source-of-truth).

    Used to populate the 404 detail so callers know what's available without
    a separate discovery call.
    """
    names: set[str] = set()
    if is_db_enabled():
        try:
            async with db_session() as sess:
                records = await list_db_initiatives(sess, tenant_id=tenant_id)
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
    """Request body for ``POST /initiatives``.

    Either ``initiative`` (catalog name) OR ``initiative_body`` (raw YAML)
    must be set — never both. Mirrors the orchestrator's ``StartPlanRequest``
    either/or shape so the inline-body fire path doesn't require polluting
    the catalog with throwaway entries.
    """

    initiative: str | None = Field(
        default=None,
        description='Catalog initiative name (without .yaml). Mutually exclusive with initiative_body.',
    )
    initiative_body: str | None = Field(
        default=None,
        description=(
            'Raw initiative YAML body. Used directly without catalog lookup. Mutually exclusive with initiative.'
        ),
        max_length=200_000,
    )

    @model_validator(mode='after')
    def _exactly_one(self) -> StartInitiativeRequest:
        # XOR: exactly one of the two MUST be set. Both-None and both-set
        # are rejected at validation time so handlers don't have to guard.
        if (self.initiative is None) == (self.initiative_body is None):
            raise ValueError('Specify exactly one of `initiative` or `initiative_body`.')
        return self


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


@router.post('', response_model=InitiativeRecord, status_code=202)
async def start_initiative(
    request: StartInitiativeRequest,
    http_request: Request,
    # Auth-hardening C1 — `POST /initiatives` is the primary s2s path
    # (orchestrator → agent). A user session must NOT be able to drive
    # the fire path directly; that would let a compromised dashboard
    # cookie spawn arbitrary K8s Jobs. `require_service_caller` enforces
    # the ``leartechapi.internal_services`` scope; a token minted for a
    # human session (audience-bound + issuer-bound but without the s2s
    # scope) is rejected with 403 before the handler body runs.
    _caller: AuthenticatedUser | None = Depends(require_service_caller),
) -> InitiativeRecord:
    """Validate the initiative YAML and spawn a K8s Job to execute it.

    Phase F: runtime is always 'job' — the run lives in its own K8s Job
    pod and survives API pod restarts. The asyncio in-process path was
    removed in this phase. The Job's lifecycle is owned by K8s; status
    reconciliation back into the DB is the reconciler's surface
    (``gate/agent/job_reconciler.py``).

    Resolution order: DB catalog first (when ``LEARTECH_INITIATIVE_DB_DSN``
    is set), filesystem fallback. DB-stored entries win over same-named
    filesystem entries.

    Returns 202 with the initial record (status=running once the Job has
    been accepted by K8s). Poll ``GET /initiatives/{id}`` for terminal
    status.

    Two firing modes:

    - ``initiative: <name>`` — catalog lookup (DB-first, FS-fallback).
    - ``initiative_body: <raw YAML>`` — parsed verbatim, no catalog touch.
      The parsed ``name:`` field is used for bookkeeping (labels, DB rows).
    """
    # v7-P1 step 5 — caller's authenticated tenant_id from the middleware
    # (extracted in step 2 from the bearer's tenant_id claim, with the
    # service-to-service X-Tenant-Id relay applied when the bearer is a
    # system-tenant service token). Used both for catalog scoping (tenant
    # only sees their own + global initiatives) AND row stamping (the
    # spawned run's tenant_id matches the caller's).
    tenant_id = get_current_tenant_id(http_request)

    if request.initiative_body is not None:
        # Inline-body path — no catalog lookup. The parsed `name:` field
        # is what shows up on the K8s Job's `leartech.io/initiative` label
        # and on the DB row's `initiative` column, so logs/queries can
        # still group by a stable identifier.
        yaml_body = request.initiative_body
        try:
            loaded = load_initiative_from_yaml(yaml_body)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
        initiative_name = loaded.name
    else:
        # Catalog path — backwards-compatible with every existing caller.
        # The XOR validator guarantees `request.initiative` is set when
        # `initiative_body` is None.
        assert request.initiative is not None  # noqa: S101 — guaranteed by validator
        initiative_name = request.initiative
        yaml_path = await _resolve_yaml_path(initiative_name, tenant_id=tenant_id)
        if yaml_path is None:
            available = await _available_names(tenant_id=tenant_id)
            raise HTTPException(
                status_code=404,
                detail={'message': f'Initiative {initiative_name!r} not found', 'available': available},
            )
        try:
            loaded = load_initiative(yaml_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f'Invalid initiative YAML: {exc}') from exc
        yaml_body = yaml_path.read_text()

    initiative_id = new_id()

    # Phase F — Job-per-run is the only path. The run executes in its
    # own K8s Job pod and survives API pod restarts. The structural fix
    # for pod-restart-kills-run.
    #
    # We do not create an asyncio.Task here: there's nothing to await
    # locally. The Job's lifecycle is owned by K8s; status reconciliation
    # back into the DB is the reconciler's surface (a watcher that
    # observes Job terminal state and patches initiative_runs.status).
    import yaml as _yaml

    from gate.agent.agentrun_client import create_agent_run, ensure_agent_type

    namespace = os.environ.get('POD_NAMESPACE')
    if not namespace:
        raise HTTPException(
            status_code=500,
            detail='POD_NAMESPACE env var is required to create an AgentRun; '
            'chart deployment must inject it via fieldRef metadata.namespace.',
        )
    try:
        # Slice B: create an AgentRun; the Go control plane
        # (leartech-orchestrator-controller) owns the mechanical spawn + tracking
        # (build the Job, watch it terminal, report the PR onto status). Image
        # routing (E.1/E.2/E.3 — image override > language hint > repo auto-detect)
        # picks the per-language runtime, ensured as an AgentType create-or-patch;
        # the initiative rides in spec.inputs (the controller inlines it as
        # LEARTECH_INITIATIVE_YAML). run_id == the AgentRun name == LEARTECH_RUN_ID.
        image = _pick_image_for_initiative(
            initiative_name,
            language=loaded.language,
            image_override=loaded.image,
            qualified_repo=loaded.primary.qualified_repo,
        )
        agent_type = await ensure_agent_type(language=loaded.language, image=image, env=_initiative_env())
        await create_agent_run(
            run_id=initiative_id,
            namespace=namespace,
            agent_type=agent_type,
            repo=loaded.primary.qualified_repo,
            inputs=_yaml.safe_load(yaml_body),
        )
        job_name = initiative_id
    except Exception as exc:  # noqa: BLE001 — surface spawn failures as 502 so the consumer sees them
        logger.exception('AgentRun creation failed for initiative %s', initiative_id)
        raise HTTPException(status_code=502, detail=f'Failed to create AgentRun: {exc}') from exc
    record = InitiativeRecord(
        id=initiative_id,
        initiative=initiative_name,
        status='queued',
        started_at=now(),
        pr_repo=loaded.primary.qualified_repo,
        cluster=_CLUSTER,
        runtime='job',
        job_name=job_name,
        # Phase D.5.1.2 — the YAML's declared branch, persisted so the
        # job_reconciler's GH-side PR fallback can `gh pr list --head
        # <branch>` without name-mangling `record.initiative`.
        branch=loaded.primary.branch,
        # v7-P1 step 5 — stamp the run with the caller's tenant_id.
        # Cross-tenant probes (GET /initiatives/{id} from another
        # tenant) will 404 once this is persisted. Tenant=None for
        # unauthenticated dev/CI traffic or the system tenant.
        tenant_id=tenant_id,
    )
    await register(record)
    # Phase D.5.3 — reflect that the Job is in flight. Without this, the
    # DB record sits at 'queued' until the reconciler patches it to
    # terminal (`complete` / `failed`), so the catalog never shows a
    # 'running' state even while the Job pod has been executing for
    # minutes.
    await update(initiative_id, status='running')
    record = record.model_copy(update={'status': 'running'})
    logger.info(
        'initiative %s running: %s — Job %s/%s',
        initiative_id,
        initiative_name,
        namespace,
        job_name,
    )
    return record


@router.get('', response_model=list[InitiativeRecord])
async def list_initiatives(http_request: Request) -> list[InitiativeRecord]:
    """List initiatives visible to the caller — running, complete, or terminal.

    v7-P1 step 5 — tenant-scoped: returns the caller's own runs plus
    legacy / unauthenticated NULL-tenant runs. System-tenant / no-auth
    contexts see everything.
    """
    tenant_id = get_current_tenant_id(http_request)
    return await list_records(tenant_id=tenant_id)


@router.get('/{initiative_id}', response_model=InitiativeRecord)
async def get_initiative_status(initiative_id: str, http_request: Request) -> InitiativeRecord:
    """Get current status of a queued / running / completed initiative.

    v7-P1 step 5 — tenant-scoped: cross-tenant lookups return 404 so
    existence isn't leaked to a caller that has no business knowing the
    row exists.
    """
    tenant_id = get_current_tenant_id(http_request)
    record = await get_record(initiative_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')
    return record


@router.get('/{initiative_id}/logs', response_class=PlainTextResponse)
async def get_initiative_logs(initiative_id: str, http_request: Request, tail_lines: int = 500) -> PlainTextResponse:
    """Tail logs for a run.

    Phase D.6 — surfaces the spawned Job pod's stdout/stderr through the same
    API as the catalog so operators don't need direct ``kubectl logs`` access
    via ``scripts/tail_agent_log.sh``.

    Looks up the run's pod via the ``leartech.io/run-id=<id>`` label in
    ``POD_NAMESPACE`` and streams the tail back as text/plain. Uses the
    same in-cluster credentials the job_reconciler uses (D.5) — the API
    pod's ServiceAccount is bound to the job-runner Role which already
    grants ``pods/log get``.

    Phase F: every run is runtime='job' so the historical
    asyncio-runtime 501 branch was removed. Legacy DB rows that still
    carry ``runtime='asyncio'`` (created pre-F before this PR landed) are
    treated as 'job' for log-fetching — they may have no backing K8s
    Job, in which case the pod-lookup returns 404 and operators are
    pointed at ``scripts/tail_agent_log.sh --run <id>`` via the error
    detail.

    NOTE: ``kubernetes_asyncio.config.load_incluster_config`` is synchronous
    in this library (do NOT await it). The async sibling is
    ``load_kube_config`` (file-based). See PR #50 root cause + the
    ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.

    v7-P1 step 5 — tenant-scoped: a tenant cannot read another tenant's
    pod logs. Cross-tenant access returns 404.
    """
    tenant_id = get_current_tenant_id(http_request)
    record = await get_record(initiative_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

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
async def cancel_initiative(initiative_id: str, http_request: Request) -> InitiativeRecord:
    """Request cancellation of a running initiative. Idempotent for terminal records.

    Phase F: cancellation deletes the run's K8s Job (propagationPolicy
    Background so the pod is GC'd asynchronously); K8s SIGTERMs the pod,
    gives it ``terminationGracePeriodSeconds`` to write the preStop
    "cancelled" sticky comment to the PR (see preStop hook in
    ``gate/agent/job_runner.py``), then kills it. We immediately write the
    cancelled status to the DB so the next ``GET`` reflects the operator's
    intent — the reconciler sees the now-terminal row and skips it, so
    the brief race where the Job has gone but the pod is still posting
    its preStop sticky does NOT mark the row 'failed'.

    NOTE: ``kubernetes_asyncio.config.load_incluster_config`` is synchronous
    in this library (do NOT await it). Same pattern as the logs endpoint
    and ``spawn_initiative_job``; see PR #50 + the
    ``feedback_kubernetes_asyncio_load_incluster_is_sync`` memory.

    v7-P1 step 5 — tenant-scoped: a tenant cannot cancel another tenant's
    run. Cross-tenant access returns 404.
    """
    tenant_id = get_current_tenant_id(http_request)
    record = await get_record(initiative_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {initiative_id!r}')

    if record.status in {'cancelled', 'complete', 'failed', 'orphaned', 'timed_out'}:
        # Idempotent: terminal records short-circuit.
        return record

    # Delete the K8s Job — Kubernetes propagates SIGTERM to the pod,
    # respects terminationGracePeriodSeconds (set on the Job manifest),
    # then kills. The preStop hook posts a "cancelled" sticky to the PR
    # (when one was resolved by the mid-run write to /tmp/run_pr_number)
    # before the pod is GC'd.
    namespace = os.environ.get('POD_NAMESPACE')
    if not namespace:
        raise HTTPException(
            status_code=500,
            detail='POD_NAMESPACE env var is required to cancel a run; '
            'chart deployment must inject it via fieldRef metadata.namespace.',
        )
    # Slice B: delete the AgentRun (named == initiative_id); the Go controller's
    # owner-ref cascades to its Job → SIGTERM → the preStop crash-sticky. 404 is
    # treated as success (already gone) inside delete_agent_run.
    from gate.agent.agentrun_client import delete_agent_run

    try:
        await delete_agent_run(initiative_id, namespace)
    except Exception as exc:  # noqa: BLE001 — surface K8s API failures as 502
        logger.exception('AgentRun delete failed for initiative %s', initiative_id)
        raise HTTPException(status_code=502, detail=f'Failed to cancel AgentRun: {exc}') from exc

    # Synchronously write terminal status. The reconciler sees this on its
    # next pass and skips the run — no race where it sees the run gone and
    # writes 'failed'.
    await update(initiative_id, status='cancelled', finished_at=now())
    logger.info('initiative %s cancelled: AgentRun %s/%s deleted', initiative_id, namespace, initiative_id)

    # Re-fetch with the same tenant scoping so a state.update race between
    # threads (or a future cross-tenant invariant violation) still returns
    # 404 to the caller. The just-cancelled row is owned by ``tenant_id``,
    # so this reads the row we just wrote.
    refreshed = await get_record(initiative_id, tenant_id=tenant_id)
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
async def validate_initiative(initiative: str, http_request: Request) -> dict[str, object]:
    """Resolve and parse an initiative YAML by name, returning a summary dict.

    No side effects. Useful for callers (Tekton task, CRD controller) to
    verify YAML correctness before POST.

    v7-P1 step 5 — tenant-scoped: validates the caller's own initiative
    or a global one. Cross-tenant names 404.
    """
    tenant_id = get_current_tenant_id(http_request)
    yaml_path = await _resolve_yaml_path(initiative, tenant_id=tenant_id)
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


@router.post('/_validate_body')
async def validate_initiative_body_alias(
    body: str = Body(..., media_type='text/plain'),
) -> dict[str, object]:
    """Alias of ``POST /_validate`` — same payload contract, same response.

    Provided so callers can pre-flight a body destined for the new
    ``initiative_body`` field on ``POST /initiatives`` against an
    explicitly-named endpoint. Implementation delegates to
    :func:`validate_initiative_body` so the two routes can never drift.
    """
    return await validate_initiative_body(body=body)


# ─── Bidirectional command queue ───────────────────────────────────────


class SubmitCommandRequest(BaseModel):
    """Request body for ``POST /initiatives/{run_id}/commands``.

    ``payload`` is a free-form JSON object whose shape is per-command:

      - ``cancel`` — ``{"reason": "<text>"}`` (optional)
      - ``pause`` — ignored
      - ``resume`` — ignored
      - ``inject_guidance`` — ``{"text": "<text>"}`` (required)

    The endpoint validates ``command_type`` against the catalog
    vocabulary but leaves payload shape to the agent-side command
    handler. The motivation: payload schemas evolve faster than the
    REST contract — e.g. a future ``cancel`` payload may carry
    ``{"reason": "...", "preserve_snapshot": true}`` and we don't want
    every CLI / orchestrator update to require an endpoint version bump.
    """

    command_type: str = Field(
        description='One of: cancel, pause, resume, inject_guidance.',
        min_length=1,
        max_length=32,
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description='Free-form per-command payload. See command_type docs.',
    )


class SubmitCommandResponse(BaseModel):
    """Response from ``POST /initiatives/{run_id}/commands``.

    Returns just the IDs the operator needs to confirm delivery — the
    full record is available via the list endpoint. Keeping the
    response thin lets the CLI's ack-print path stay one line.
    """

    command_id: int
    submitted_at: datetime


class CommandRecordResponse(BaseModel):
    """Response shape for the list endpoint — mirrors
    :class:`AgentRunCommandRecord` so FastAPI can serialise the
    dataclass directly with no extra conversion."""

    id: int
    run_id: str
    command_type: str
    payload: Any | None = None
    submitted_at: datetime
    acked_at: datetime | None = None
    ack_message: str | None = None

    @classmethod
    def from_record(cls, record: AgentRunCommandRecord) -> CommandRecordResponse:
        return cls(
            id=record.id,
            run_id=record.run_id,
            command_type=record.command_type,
            payload=record.payload,
            submitted_at=record.submitted_at,
            acked_at=record.acked_at,
            ack_message=record.ack_message,
        )


def _require_db_enabled_for_commands() -> None:
    """Both command endpoints require the DB.

    The agent loop polls the same table; if the DB is disabled the
    operator's command would never reach the agent. Surface a 503 with
    a clear message so the operator knows it isn't a generic outage.
    """
    if not is_db_enabled():
        raise HTTPException(
            status_code=503,
            detail='Command queue requires LEARTECH_INITIATIVE_DB_DSN. '
            'This service instance is running in filesystem-only mode.',
        )


@router.post('/{run_id}/commands', response_model=SubmitCommandResponse, status_code=201)
async def submit_command(run_id: str, request: SubmitCommandRequest, http_request: Request) -> SubmitCommandResponse:
    """Queue a command for the agent driving ``run_id`` to process.

    Lookups happen in two stages:

    1. The run is fetched from ``initiative_runs`` (404 if missing).
    2. If the run is terminal, return 409 — commands against a
       finished run are no-ops that would confuse operators.
    3. The command is appended; the agent's next-turn poll picks it up
       within one turn boundary (~5-15s typical).

    Validation:

      - Unknown ``command_type`` → 422.
      - ``inject_guidance`` without payload.text → 422 (an empty
        injection is almost always a CLI mistake; the user wanted to
        say something).

    Idempotency: NOT enforced. The operator can intentionally inject
    the same guidance twice (e.g. emphasising it). If the client wants
    idempotency it can carry an opaque ID in the payload and the agent
    handler can dedupe — but the storage layer treats every POST as a
    new row.
    """
    _require_db_enabled_for_commands()

    if request.command_type not in AGENT_RUN_COMMAND_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f'Unknown command_type {request.command_type!r}; expected one of {sorted(AGENT_RUN_COMMAND_TYPES)}'
            ),
        )
    if request.command_type == 'inject_guidance':
        text = (request.payload or {}).get('text')
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(
                status_code=422,
                detail='inject_guidance requires payload.text to be a non-empty string.',
            )

    # v7-P1 step 5 — tenant-scoped: a tenant can only queue commands on
    # their own run. Cross-tenant lookups 404.
    tenant_id = get_current_tenant_id(http_request)
    record = await get_record(run_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {run_id!r}')
    if record.status in {'complete', 'failed', 'cancelled', 'orphaned', 'timed_out'}:
        raise HTTPException(
            status_code=409,
            detail=(
                f'Run {run_id!r} is already terminal (status={record.status!r}); '
                'commands cannot be queued against a finished run.'
            ),
        )

    try:
        async with db_session() as sess:
            inserted = await insert_command(
                sess,
                run_id=run_id,
                command_type=request.command_type,
                payload=request.payload,
            )
    except UnknownCommandTypeError as exc:
        # Defence in depth — the request-level validation above should
        # have caught this; re-raising as 422 keeps the contract clean
        # in case a future caller bypasses the model layer.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        'command queued for run %s: type=%s id=%d',
        run_id,
        inserted.command_type,
        inserted.id,
    )
    return SubmitCommandResponse(command_id=inserted.id, submitted_at=inserted.submitted_at)


@router.get('/{run_id}/commands', response_model=list[CommandRecordResponse])
async def list_run_commands(
    run_id: str,
    http_request: Request,
    unacked_only: bool = False,
) -> list[CommandRecordResponse]:
    """Return queued commands for ``run_id`` (newest first per submission).

    Operator surface for the bidirectional queue — pairs with the
    POST endpoint so the CLI's ``leartech-agent ops`` group can list
    pending commands as well as queue them. ``unacked_only=true``
    matches the agent's poll query exactly and is useful for "is my
    cancel still pending?" checks.

    Returns an empty list (NOT 404) when the run has no commands —
    the absence of any command is itself meaningful information and
    deserves a 200 with ``[]`` rather than an error.
    """
    _require_db_enabled_for_commands()

    # v7-P1 step 5 — tenant-scoped: a tenant can only list commands on
    # their own run. Cross-tenant lookups 404.
    tenant_id = get_current_tenant_id(http_request)
    record = await get_record(run_id, tenant_id=tenant_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f'No initiative with id {run_id!r}')

    async with db_session() as sess:
        records = await list_commands(sess, run_id=run_id, unacked_only=unacked_only)
    return [CommandRecordResponse.from_record(r) for r in records]
