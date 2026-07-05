"""Tests for the best-effort AgentRun.status.targetPR report-back (C1)."""

from __future__ import annotations

from typing import Any

import pytest

import gate.agent.agentrun_status as ars


@pytest.mark.asyncio
async def test_noop_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_AGENTRUN_STATUS', raising=False)
    # Must not raise and must not touch k8s (no flag → early return).
    await ars.patch_pr_number(47)


@pytest.mark.asyncio
async def test_noop_when_pr_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'true')
    monkeypatch.setenv('AGENT_RUN_NAME', 'run-1')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')
    await ars.patch_pr_number(None)


@pytest.mark.asyncio
async def test_patches_status_subresource(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'true')
    monkeypatch.setenv('AGENT_RUN_NAME', 'run-1')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')

    captured: dict[str, Any] = {}

    class FakeCustom:
        def __init__(self, _api: Any) -> None: ...

        async def patch_namespaced_custom_object_status(self, **kw: Any) -> None:
            captured.update(kw)

    class FakeApiClient:
        async def __aenter__(self) -> FakeApiClient:
            return self

        async def __aexit__(self, *_a: Any) -> bool:
            return False

    from kubernetes_asyncio import client, config

    monkeypatch.setattr(config, 'load_incluster_config', lambda: None)
    monkeypatch.setattr(client, 'ApiClient', FakeApiClient)
    monkeypatch.setattr(client, 'CustomObjectsApi', FakeCustom)

    await ars.patch_pr_number(47)

    assert captured['name'] == 'run-1'
    assert captured['namespace'] == 'jx-staging'
    assert captured['group'] == 'agent.leartech.io'
    assert captured['plural'] == 'agentruns'
    assert captured['body'] == {'status': {'targetPR': '47'}}
    assert captured['_content_type'] == 'application/merge-patch+json'


@pytest.mark.asyncio
async def test_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LEARTECH_AGENTRUN_STATUS', 'true')
    monkeypatch.setenv('AGENT_RUN_NAME', 'run-1')
    monkeypatch.setenv('AGENT_RUN_NAMESPACE', 'jx-staging')

    from kubernetes_asyncio import config

    def _boom() -> None:
        raise RuntimeError('no cluster')

    monkeypatch.setattr(config, 'load_incluster_config', _boom)
    # Best-effort: must swallow and never raise.
    await ars.patch_pr_number(47)
