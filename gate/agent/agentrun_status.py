"""Best-effort: patch this run's AgentRun.status.targetPR (C1 report-back).

When the Go controller spawned us with LEARTECH_AGENTRUN_STATUS=true (+ AGENT_RUN_NAME
/ AGENT_RUN_NAMESPACE), the Job SA holds a narrow agentruns/status:patch Role and we
write the PR number straight onto the CR status subresource — push, atomic, no
log-scrape. Entirely best-effort: any failure is logged and swallowed so it never
affects the run outcome, and it is a no-op when the flag/identity is absent (so
`run_initiative` behaviour is unchanged off the controller path).
"""

from __future__ import annotations

import logging
import os

_logger = logging.getLogger(__name__)

_GROUP = 'agent.leartech.io'
_VERSION = 'v1alpha1'
_PLURAL = 'agentruns'


def _enabled() -> tuple[str, str] | None:
    """Return (name, namespace) when self-reporting is on + identity is present."""
    if os.environ.get('LEARTECH_AGENTRUN_STATUS') != 'true':
        return None
    name = os.environ.get('AGENT_RUN_NAME')
    namespace = os.environ.get('AGENT_RUN_NAMESPACE')
    if not name or not namespace:
        _logger.warning('agentrun status-patch skipped: AGENT_RUN_NAME/NAMESPACE unset')
        return None
    return name, namespace


async def patch_pr_number(pr_number: int | None) -> None:
    """Patch AgentRun.status.targetPR = str(pr_number). No-op unless enabled + resolvable."""
    if pr_number is None:
        return
    identity = _enabled()
    if identity is None:
        return
    name, namespace = identity
    try:
        from kubernetes_asyncio import client, config

        config.load_incluster_config()  # synchronous in kubernetes_asyncio
        async with client.ApiClient() as api:
            custom = client.CustomObjectsApi(api)
            await custom.patch_namespaced_custom_object_status(
                group=_GROUP,
                version=_VERSION,
                namespace=namespace,
                plural=_PLURAL,
                name=name,
                body={'status': {'targetPR': str(pr_number)}},
            )
        _logger.info('patched AgentRun %s/%s status.targetPR=%s', namespace, name, pr_number)
    except Exception as exc:  # noqa: BLE001 — best-effort; must never break the run
        _logger.warning('agentrun status-patch failed (non-fatal): %s', exc)
