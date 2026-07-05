"""Create AgentRun CRs so the Go control plane (leartech-orchestrator-controller)
spawns + tracks the agent Job.

Slice B of the Orch+Agent rewrite: this REPLACES the in-process job_runner. The
mechanical spawn (build the Job) and tracking (Job → terminal → status) move to
the Go controller; this module only creates the AgentRun that describes the work
and reads its status back for projection into initiative_runs.

The per-language AgentType (the runtime image) is ensured create-or-patch from the
image the caller already resolved (_pick_image_for_initiative), so no separate
GitOps activation is needed — the control plane's command/env contract is fixed.
"""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)

_GROUP = 'agent.leartech.io'
_VERSION = 'v1alpha1'
_AGENTRUNS = 'agentruns'
_AGENTTYPES = 'agenttypes'


def _agent_type_name(language: str | None) -> str:
    """The AgentType (per-language runtime) an initiative maps to."""
    return f'leartech-agent-{(language or "py").strip().lower()}'


async def _custom_api() -> Any:
    from kubernetes_asyncio import client, config

    config.load_incluster_config()
    return client.CustomObjectsApi(client.ApiClient())


async def ensure_agent_type(
    *, language: str | None, image: str, env: dict[str, str] | None = None, service_account: str = 'default'
) -> str:
    """Create-or-patch the cluster-scoped AgentType for `language` with `image`.

    Returns the AgentType name. Idempotent: patches the image on an existing one so
    a fleet image bump flows through without manual AgentType edits.
    """
    from kubernetes_asyncio.client.exceptions import ApiException

    name = _agent_type_name(language)
    spec: dict[str, Any] = {'image': image, 'serviceAccountName': service_account}
    if env:
        spec['env'] = env
    body = {'apiVersion': f'{_GROUP}/{_VERSION}', 'kind': 'AgentType', 'metadata': {'name': name}, 'spec': spec}
    api = await _custom_api()
    try:
        try:
            await api.create_cluster_custom_object(group=_GROUP, version=_VERSION, plural=_AGENTTYPES, body=body)
            _logger.info('created AgentType %s (image=%s)', name, image)
        except ApiException as exc:
            if exc.status != 409:  # already exists → patch the image
                raise
            await api.patch_cluster_custom_object(
                group=_GROUP,
                version=_VERSION,
                plural=_AGENTTYPES,
                name=name,
                body={'spec': spec},
                _content_type='application/merge-patch+json',
            )
    finally:
        await api.api_client.close()
    return name


async def create_agent_run(
    *, run_id: str, namespace: str, agent_type: str, repo: str, inputs: dict[str, Any], tenant_id: str | None = None
) -> str:
    """Create an AgentRun named `run_id`; the Go controller spawns + tracks it.

    `run_id` == the AgentRun name == LEARTECH_RUN_ID in the Job, so the DB run row
    and the CR share one identity.
    """
    spec: dict[str, Any] = {'agentType': agent_type, 'repo': repo, 'inputs': inputs}
    if tenant_id:
        spec['tenant'] = tenant_id
    body = {
        'apiVersion': f'{_GROUP}/{_VERSION}',
        'kind': 'AgentRun',
        'metadata': {'name': run_id, 'namespace': namespace},
        'spec': spec,
    }
    api = await _custom_api()
    try:
        await api.create_namespaced_custom_object(
            group=_GROUP, version=_VERSION, namespace=namespace, plural=_AGENTRUNS, body=body
        )
        _logger.info('created AgentRun %s (agentType=%s repo=%s)', run_id, agent_type, repo)
    finally:
        await api.api_client.close()
    return run_id


async def list_agent_runs(namespace: str) -> list[dict[str, Any]]:
    """List AgentRuns in `namespace` (for the status projection)."""
    api = await _custom_api()
    try:
        resp = await api.list_namespaced_custom_object(
            group=_GROUP, version=_VERSION, namespace=namespace, plural=_AGENTRUNS
        )
        items: list[dict[str, Any]] = resp.get('items', [])
        return items
    finally:
        await api.api_client.close()


async def delete_agent_run(name: str, namespace: str) -> None:
    """Delete an AgentRun; Background propagation cascades to its owned Job (which
    SIGTERMs the agent pod → the preStop crash-sticky). 404 is treated as success
    (already gone)."""
    from kubernetes_asyncio.client.exceptions import ApiException

    api = await _custom_api()
    try:
        await api.delete_namespaced_custom_object(
            group=_GROUP,
            version=_VERSION,
            namespace=namespace,
            plural=_AGENTRUNS,
            name=name,
            propagation_policy='Background',
        )
    except ApiException as exc:
        if exc.status != 404:
            raise
    finally:
        await api.api_client.close()
