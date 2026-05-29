"""Cleanup primitives for the Phase B admin endpoint.

Two cleanup surfaces are combined behind one endpoint (B.3 + B.4):

  - ``reconcile_orphaned_runs`` — thin re-export of
    :func:`app.state.reconcile_orphaned_runs` with the age filter wired
    through. Keeps the imperative orphan-detection in one place (the
    state module) while presenting the admin endpoint a clear seam.
  - ``cancel_superseded_pipelineruns`` — list Lighthouse-spawned Tekton
    PipelineRuns labelled with the agent's own repo, cancel those whose
    ``lastCommitSHA`` no longer matches the PR's tip.

The endpoint in ``app/routers/admin.py`` is the only caller; everything
else stays in this module so it can be exercised in isolation with
mocked K8s clients.

Cluster credentials: same in-cluster ServiceAccount the rest of the
service uses. ``kubernetes_asyncio.config.load_incluster_config`` is
synchronous in the library — must NOT be awaited (PR #50 lesson).
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from app.state import reconcile_orphaned_runs as _reconcile

logger = logging.getLogger(__name__)

# Repo whose Lighthouse-spawned PipelineRuns we sweep when callers do not
# pass ``repo`` explicitly. Defaults to the deployed agent's own repo so
# that the wasted PipelineRuns documented in the B.4 roadmap (created by
# the agent's own self-push churn) are the default target.
DEFAULT_REPO = 'mikelear/leartech-automated-agent'

# Lighthouse adds these labels to every PipelineRun it spawns. See
# leartech-pipeline-catalog and the
# ``detect-catalog-side-trigger-disappearance`` lesson for the
# enumeration.
_LABEL_REFS_REPO = 'lighthouse.jenkins-x.io/refs.repo'
_LABEL_REFS_PULL = 'lighthouse.jenkins-x.io/refs.pull'
_LABEL_LAST_COMMIT = 'lighthouse.jenkins-x.io/lastCommitSHA'

# Tekton CustomResourceDefinition coordinates. v1 is the GA channel; both
# clusters have migrated. v1beta1 is still mounted by some pipelines (see
# .lighthouse/jenkins-x/*.yaml) but the CRD list/patch verbs target v1.
_TEKTON_GROUP = 'tekton.dev'
_TEKTON_VERSION = 'v1'
_TEKTON_PLURAL = 'pipelineruns'

# Terminal Tekton conditions — anything with one of these reasons is
# already done and not a candidate for the cancel patch. We rely on the
# Run condition's reason rather than the more nuanced status fields
# because the latter differ between v1beta1 and v1.
_TERMINAL_REASONS = frozenset(
    {'Succeeded', 'Failed', 'Completed', 'PipelineRunCancelled', 'Cancelled', 'PipelineRunTimeout'}
)


async def reconcile_orphaned_runs(older_than_seconds: int) -> int:
    """Mark stuck in-flight ``initiative_runs`` rows as ``orphaned``.

    Delegates to :func:`app.state.reconcile_orphaned_runs` so the
    imperative orphan-detection lives in one place. The age filter is
    forwarded — only candidates older than ``older_than_seconds`` are
    eligible to be marked orphaned.

    Returns the count of rows updated.
    """
    return await _reconcile(older_than_seconds=older_than_seconds)


def _pr_head_sha(repo: str, pr_number: str) -> str | None:
    """Resolve the current HEAD SHA of ``repo``'s ``pr_number``.

    Shells out to ``gh api`` so the same auth path the rest of the agent
    uses (GH_TOKEN env or `gh auth`) is honoured. Returns ``None`` on
    any failure — the caller must treat that as "don't cancel" so a
    transient API blip never cascades into wrongful cancellation of a
    healthy run.
    """
    try:
        result = subprocess.run(  # noqa: S603 — `gh` is on PATH; args are sanitised below
            ['gh', 'api', f'repos/{repo}/pulls/{pr_number}', '--jq', '.head.sha'],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning('cleanup._pr_head_sha: gh api errored for %s#%s: %s', repo, pr_number, exc)
        return None
    if result.returncode != 0:
        logger.warning(
            'cleanup._pr_head_sha: gh api failed for %s#%s (exit %d): %s',
            repo,
            pr_number,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    sha = result.stdout.strip()
    return sha or None


def _pipelinerun_is_terminal(pipelinerun: Any) -> bool:
    """Returns True if the PipelineRun has already reached a terminal condition.

    We must NOT submit a cancel patch against a terminal Run — the API
    returns 422 / Conflict and that's pointless noise in the operator
    summary. Tekton surfaces the rollup state via
    ``status.conditions[type=Succeeded].reason``; we treat anything in
    :data:`_TERMINAL_REASONS` as already-done.
    """
    status = pipelinerun.get('status') if isinstance(pipelinerun, dict) else None
    if not status:
        return False
    conditions = status.get('conditions') or []
    for cond in conditions:
        if cond.get('type') != 'Succeeded':
            continue
        reason = cond.get('reason')
        if reason in _TERMINAL_REASONS:
            return True
        # status=False with a non-pending reason also counts as terminal —
        # Tekton emits e.g. {type: Succeeded, status: False, reason: Failed}.
        if cond.get('status') == 'False' and reason and reason not in {'Running', 'Pending', 'Started'}:
            return True
    return False


async def cancel_superseded_pipelineruns(namespace: str, *, repo: str = DEFAULT_REPO) -> int:
    """Cancel Tekton PipelineRuns whose head SHA is no longer the PR's tip.

    Iterates PipelineRuns in ``namespace`` labelled
    ``lighthouse.jenkins-x.io/refs.repo=<repo>``. For each non-terminal
    Run, looks up the open PR's HEAD via ``gh api`` and compares against
    the Run's ``lighthouse.jenkins-x.io/lastCommitSHA`` label. When the
    SHAs disagree, the Run is for a superseded push and gets patched
    with ``spec.status: PipelineRunCancelled``.

    Returns the number of PipelineRuns successfully patched. Failures on
    individual PipelineRuns are logged but do NOT abort the sweep — one
    misbehaving Run shouldn't strand the rest.

    Per-PR HEAD lookups are cached for the duration of one call so we
    don't hammer the GitHub API when the same PR has multiple
    PipelineRuns (a near-universal case — every push opens N parallel
    pipelines).
    """
    # Late imports: kubernetes_asyncio is only needed on this code path;
    # keeping the import here means in-process tests that don't exercise
    # the K8s surface can still ``import gate.admin.cleanup``.
    from kubernetes_asyncio import client as k8s_client
    from kubernetes_asyncio import config as k8s_config
    from kubernetes_asyncio.client.api_client import ApiClient

    k8s_config.load_incluster_config()  # synchronous — do NOT await
    cancelled = 0
    head_sha_cache: dict[str, str | None] = {}

    async with ApiClient() as api:
        custom = k8s_client.CustomObjectsApi(api)
        resp = await custom.list_namespaced_custom_object(
            group=_TEKTON_GROUP,
            version=_TEKTON_VERSION,
            namespace=namespace,
            plural=_TEKTON_PLURAL,
            label_selector=f'{_LABEL_REFS_REPO}={repo}',
        )
        items: list[Any] = (resp or {}).get('items') or []

        for pr_run in items:
            metadata = pr_run.get('metadata') or {}
            name = metadata.get('name')
            labels = metadata.get('labels') or {}
            pull = labels.get(_LABEL_REFS_PULL)
            last_sha = labels.get(_LABEL_LAST_COMMIT)
            if not name or not pull or not last_sha:
                logger.debug('cleanup.cancel_superseded_pipelineruns: skipping %s (missing labels)', name)
                continue

            if _pipelinerun_is_terminal(pr_run):
                continue

            if pull not in head_sha_cache:
                head_sha_cache[pull] = _pr_head_sha(repo, pull)
            head_sha = head_sha_cache[pull]
            if head_sha is None:
                # Couldn't resolve — treat as live (don't cancel) and move on.
                continue
            if last_sha == head_sha:
                continue

            try:
                await custom.patch_namespaced_custom_object(
                    group=_TEKTON_GROUP,
                    version=_TEKTON_VERSION,
                    namespace=namespace,
                    plural=_TEKTON_PLURAL,
                    name=name,
                    body={'spec': {'status': 'PipelineRunCancelled'}},
                )
                cancelled += 1
                logger.info(
                    'cleanup.cancel_superseded_pipelineruns: cancelled PR#%s superseded run %s '
                    '(label sha=%s, PR head=%s)',
                    pull,
                    name,
                    last_sha,
                    head_sha,
                )
            except Exception as exc:  # noqa: BLE001 — one bad PR shouldn't strand the sweep
                logger.warning('cleanup.cancel_superseded_pipelineruns: patch failed for %s: %s', name, exc)

    return cancelled


def _resolve_namespace() -> str:
    """Return the namespace the cleanup operates in (``POD_NAMESPACE``).

    Raises ``RuntimeError`` when unset so the router surfaces a 500 to the
    operator instead of silently sweeping nothing. Production deployment
    injects POD_NAMESPACE via the chart's downward-API fieldRef.
    """
    ns = os.environ.get('POD_NAMESPACE')
    if not ns:
        raise RuntimeError(
            'POD_NAMESPACE env var is required for /admin/cleanup; '
            'chart deployment must inject it via fieldRef metadata.namespace.'
        )
    return ns


__all__ = [
    'DEFAULT_REPO',
    'cancel_superseded_pipelineruns',
    'reconcile_orphaned_runs',
]
