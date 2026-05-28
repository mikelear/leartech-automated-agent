"""Spawn a K8s Job per initiative — D.3 infra primitive.

Used by POST /initiatives once `LEARTECH_INITIATIVE_RUNTIME=job` is set
(D.4 wires the call site behind that feature flag). This module is pure
function: build a `batch/v1` Job manifest and submit it. No watching,
no log streaming, no status reconciliation — D.5 owns that surface.

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
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
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
    # Inline the YAML body so the Job pod can resolve the initiative
    # without DB access. Keep this last so it sorts predictably at the
    # tail of the env list in test assertions.
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
                            'securityContext': {
                                'runAsNonRoot': True,
                                'runAsUser': 1000,
                                'allowPrivilegeEscalation': False,
                                'capabilities': {'drop': ['ALL']},
                            },
                            'env': env_list,
                            'resources': resources,
                        }
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
    secret_refs: dict[str, dict[str, str]] | None = None,
    service_account: str = DEFAULT_SERVICE_ACCOUNT,
    resources: dict[str, dict[str, str]] | None = None,
    ttl_seconds_after_finished: int = DEFAULT_TTL_SECONDS_AFTER_FINISHED,
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
            ttl_seconds_after_finished=ttl_seconds_after_finished,
        )
        resp = await batch.create_namespaced_job(namespace=namespace, body=manifest)
        return resp.metadata.name, resp.metadata.namespace
