"""Spawn a K8s Job per initiative — D.3 infra primitive.

Used by POST /initiatives unconditionally (Phase F made Job-per-run the
only path). This module is pure function: build a `batch/v1` Job
manifest and submit it. No watching, no log streaming, no status
reconciliation — D.5 owns that surface.

The manifest shape mirrors the chart's named template at
`charts/leartech-automated-agent/templates/_job-template.tpl` (added in
D.2). Keep the two in sync: labels, securityContext, env-shape,
resource defaults all must match. The chart template is the canonical
reference — when in doubt, re-read it and re-mirror.

Why we don't `tpl`-render the chart template from Python:
  - kubernetes-asyncio submits dicts directly to the API; rendering a
    string template via Helm at spawn time would mean shelling out to
    `helm template` on every run. Slower and adds a Helm install
    dependency to the API pod.
  - The Python builder is ~60 lines and easy to test in isolation.
  - The chart template stays useful as the canonical spec + as the
    rendered RBAC + ServiceAccount carrier (those still need Helm).

If the chart template shape changes, this module must be updated to
match. The `test_job_runner_manifest_matches_chart_shape` test in
`tests/test_job_runner.py` is the early-warning system for drift.
"""

from __future__ import annotations

from typing import Any

from kubernetes_asyncio import client, config
from kubernetes_asyncio.client.api_client import ApiClient

DEFAULT_SERVICE_ACCOUNT = 'leartech-automated-agent-job-runner'
DEFAULT_TTL_SECONDS_AFTER_FINISHED = 86400  # 24 h — matches values.yaml default

# Phase D.7 — grace window for the preStop hook to post a "cancelled" sticky
# comment to the PR before K8s SIGKILLs the pod. The hook itself is fast
# (one gh API call) but we give 30s of headroom for transient network blips
# / gh CLI retries. Cancel from the API pod must NOT block longer than this
# either — operators expect ``POST /initiatives/{id}/cancel`` to return
# promptly with the row already marked 'cancelled' in the DB.
DEFAULT_TERMINATION_GRACE_PERIOD_SECONDS = 30


def _default_resources() -> dict[str, dict[str, str]]:
    """Fallback resource policy when caller doesn't override.

    Matches `values.yaml::jobs.resources` defaults (D.2). Higher than
    API-pod defaults because initiative runs hit Anthropic SDK memory
    peaks well above 1 GiB on substantial refactors — see the
    mortgages-gw OOMKilled incident referenced in values.yaml.
    """
    return {
        'requests': {'cpu': '500m', 'memory': '1Gi'},
        'limits': {'cpu': '4', 'memory': '8Gi'},
    }


def _build_job_manifest(
    *,
    run_id: str,
    initiative: str,
    image: str,
    namespace: str,
    service_account: str,
    env: dict[str, str],
    secret_refs: dict[str, dict[str, str]],
    resources: dict[str, dict[str, str]],
    yaml_body: str,
    pr_repo: str = '',
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    termination_grace_period_seconds: int = DEFAULT_TERMINATION_GRACE_PERIOD_SECONDS,
) -> dict[str, Any]:
    """Construct the V1Job dict submitted to batch/v1.

    Mirrors `_job-template.tpl` field-for-field; see that template's
    leading comment for input semantics. The structural test
    `test_job_runner_manifest_matches_chart_shape` enforces parity.

    The initiative YAML body is injected via `LEARTECH_INITIATIVE_YAML`
    env var. The container's command writes it to /tmp/initiative.yaml
    then execs `python -m gate.agent.initiative <path>` — matching the
    agent CLI's positional INITIATIVE_PATH argument. Inlining the YAML
    body avoids needing the Job pod to query the catalog DB (which would
    require forwarding the DSN secret and waiting on Postgres at startup).
    """
    labels = {
        'leartech.io/initiative': initiative,
        'leartech.io/run-id': run_id,
        'leartech.io/component': 'initiative-runner',
    }

    # env entries: plain `value` form for env, `valueFrom.secretKeyRef` for secret_refs.
    env_list: list[dict[str, Any]] = [{'name': name, 'value': value} for name, value in env.items()]
    # V5 D2.2 — the agent loop reads ``LEARTECH_RUN_ID`` to populate
    # ``initiative_runs.started_executing_at`` on the first SDK turn. The
    # Job's metadata.name equals run_id by the D.3 contract, but the pod
    # itself can't trivially extract it from $HOSTNAME (pods are named
    # ``<job-name>-<hash>``), so we forward it explicitly.
    env_list.append({'name': 'LEARTECH_RUN_ID', 'value': run_id})
    # D.7 — propagate the qualified repo so the preStop hook can post a
    # "cancelled" sticky to the PR. PR number isn't known at spawn time
    # (resolved by run_initiative near end-of-run), so the agent writes it
    # to /tmp/run_pr_number mid-run and the preStop sh wrapper reads it
    # from there. Empty string is the well-defined "no repo to post to"
    # case — crash_sticky.py treats it the same as missing.
    env_list.append({'name': 'LEARTECH_PR_REPO', 'value': pr_repo})
    # Inline the YAML body so the Job pod can resolve the initiative
    # without DB access. Keep this last (before secret_refs) so it sorts
    # predictably at the tail of the env list in test assertions.
    env_list.append({'name': 'LEARTECH_INITIATIVE_YAML', 'value': yaml_body})
    for name, ref in secret_refs.items():
        env_list.append(
            {
                'name': name,
                'valueFrom': {'secretKeyRef': {'name': ref['secret'], 'key': ref['key']}},
            }
        )

    return {
        'apiVersion': 'batch/v1',
        'kind': 'Job',
        'metadata': {
            'name': run_id,
            'namespace': namespace,
            'labels': labels,
        },
        'spec': {
            'backoffLimit': 0,
            'ttlSecondsAfterFinished': ttl_seconds_after_finished,
            'template': {
                'metadata': {'labels': labels},
                'spec': {
                    'serviceAccountName': service_account,
                    'restartPolicy': 'Never',
                    'securityContext': {
                        'runAsNonRoot': True,
                        'runAsUser': 1000,
                        'fsGroup': 1000,
                    },
                    'terminationGracePeriodSeconds': termination_grace_period_seconds,
                    'containers': [
                        {
                            'name': 'agent',
                            'image': image,
                            'imagePullPolicy': 'Always',
                            # sh -c bootstrap writes the inlined YAML to /tmp/initiative.yaml
                            # then execs the agent CLI with the positional path it expects.
                            # `printf '%s'` (not echo) preserves the YAML verbatim including
                            # leading dashes + multiline blocks. `exec` keeps PID 1 as python
                            # so K8s sees Job completion when the agent exits.
                            'command': ['sh', '-c'],
                            'args': [
                                'printf "%s" "$LEARTECH_INITIATIVE_YAML" > /tmp/initiative.yaml '
                                '&& exec python -m gate.agent.initiative /tmp/initiative.yaml'
                            ],
                            # D.7 — preStop hook posts a "cancelled" sticky to the PR
                            # before K8s SIGKILLs the pod. Reads the PR number from
                            # /tmp/run_pr_number (written by run_initiative when it
                            # resolves the PR mid-run); LEARTECH_PR_REPO is set at
                            # spawn time. Wrapped in `|| true` so a failure here never
                            # blocks pod termination — we're already shutting down.
                            # Uses sh expansion `$(cat ...)` so missing file → empty
                            # string → crash_sticky skips gracefully.
                            'lifecycle': {
                                'preStop': {
                                    'exec': {
                                        'command': [
                                            'sh',
                                            '-c',
                                            'python -m gate.agent.crash_sticky '
                                            '--reason cancelled '
                                            '--repo "$LEARTECH_PR_REPO" '
                                            '--pr "$(cat /tmp/run_pr_number 2>/dev/null || true)" '
                                            '|| true',
                                        ],
                                    },
                                },
                            },
                            'securityContext': {
                                'runAsNonRoot': True,
                                'runAsUser': 1000,
                                'allowPrivilegeEscalation': False,
                                'capabilities': {'drop': ['ALL']},
                            },
                            'env': env_list,
                            'resources': resources,
                            # Writable workspace at /workspace — agent loop clones
                            # consumer repos into here (see initiative.py::_clone_repo).
                            # The image's /workspace path is not writable by UID 1000;
                            # emptyDir gives a per-Job scratch space with fsGroup=1000.
                            'volumeMounts': [
                                {'name': 'workspace', 'mountPath': '/workspace'},
                            ],
                        }
                    ],
                    'volumes': [
                        {'name': 'workspace', 'emptyDir': {}},
                    ],
                },
            },
        },
    }


async def spawn_initiative_job(
    *,
    initiative_name: str,
    run_id: str,
    image: str,
    namespace: str,
    env: dict[str, str],
    yaml_body: str,
    pr_repo: str = '',
    secret_refs: dict[str, dict[str, str]] | None = None,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
    resources: dict[str, dict[str, str]] | None = None,
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    termination_grace_period_seconds: int = DEFAULT_TERMINATION_GRACE_PERIOD_SECONDS,
) -> tuple[str, str]:
    """Spawn a K8s Job for one initiative run.

    Returns `(job_name, namespace)`. Job name is exactly `run_id`, so
    callers can recover the Job by name later without a label-selector
    query.

    Idempotency: if a Job with this `run_id` already exists in the
    namespace, the API server returns 409 Conflict and this function
    re-raises it as `kubernetes_asyncio.client.ApiException`. Callers
    should treat 409 as "this run is already in flight" rather than as
    a transient error — retrying with the same run_id will keep
    failing until the prior Job is cleaned up (TTL or manual delete).

    Requires in-cluster credentials (the API pod's ServiceAccount).
    Loads via `load_incluster_config()`; not usable from a developer
    laptop without further plumbing. NOTE: in `kubernetes_asyncio`,
    `load_incluster_config` is synchronous — do NOT await it. The async
    counterpart is `load_kube_config` (file-based). See PR #50 root cause.
    """
    config.load_incluster_config()
    async with ApiClient() as api:
        batch = client.BatchV1Api(api)
        manifest = _build_job_manifest(
            run_id=run_id,
            initiative=initiative_name,
            image=image,
            namespace=namespace,
            service_account=service_account,
            env=env,
            secret_refs=secret_refs or {},
            resources=resources or _default_resources(),
            yaml_body=yaml_body,
            pr_repo=pr_repo,
            ttl_seconds_after_finished=ttl_seconds_after_finished,
            termination_grace_period_seconds=termination_grace_period_seconds,
        )
        resp = await batch.create_namespaced_job(namespace=namespace, body=manifest)
        return resp.metadata.name, resp.metadata.namespace
