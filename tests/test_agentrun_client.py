"""Tests for the AgentRun/AgentType k8s client (Slice B spawn path)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

import gate.agent.agentrun_client as arc


class _FakeApi:
    def __init__(
        self,
        *,
        create_ct_status: int | None = None,
        delete_status: int | None = None,
        get_status_body: dict[str, Any] | None = None,
        get_status_exc: Exception | None = None,
        patch_status_exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._create_ct_status = create_ct_status
        self._delete_status = delete_status
        self._get_status_body = get_status_body or {}
        self._get_status_exc = get_status_exc
        self._patch_status_exc = patch_status_exc
        self.closed = False
        self.api_client = SimpleNamespace(close=self._close)

    async def _close(self) -> None:
        self.closed = True

    async def get_namespaced_custom_object_status(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(('get_status', kw))
        if self._get_status_exc is not None:
            raise self._get_status_exc
        return self._get_status_body

    async def patch_namespaced_custom_object_status(self, **kw: Any) -> None:
        self.calls.append(('patch_status', kw))
        if self._patch_status_exc is not None:
            raise self._patch_status_exc

    async def create_cluster_custom_object(self, **kw: Any) -> None:
        self.calls.append(('create_ct', kw))
        if self._create_ct_status:
            raise ApiException(status=self._create_ct_status)

    async def patch_cluster_custom_object(self, **kw: Any) -> None:
        self.calls.append(('patch_ct', kw))

    async def create_namespaced_custom_object(self, **kw: Any) -> None:
        self.calls.append(('create_ar', kw))

    async def list_namespaced_custom_object(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(('list', kw))
        return {'items': [{'metadata': {'name': 'r1'}}]}

    async def delete_namespaced_custom_object(self, **kw: Any) -> None:
        self.calls.append(('delete', kw))
        if self._delete_status:
            raise ApiException(status=self._delete_status)


def _patch_api(monkeypatch: pytest.MonkeyPatch, fake: _FakeApi) -> None:
    async def _fake_custom_api() -> Any:
        return fake

    monkeypatch.setattr(arc, '_custom_api', _fake_custom_api)


def test_agent_type_name() -> None:
    assert arc._agent_type_name('go') == 'leartech-agent-go'
    assert arc._agent_type_name(None) == 'leartech-agent-py'
    assert arc._agent_type_name('  NG ') == 'leartech-agent-ng'


@pytest.mark.asyncio
async def test_ensure_agent_type_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, fake)
    name = await arc.ensure_agent_type(language='py', image='img:1', env={'LEARTECH_AGENT_MODEL': 'm'})
    assert name == 'leartech-agent-py'
    verb, kw = fake.calls[0]
    assert verb == 'create_ct'
    assert kw['body']['spec']['image'] == 'img:1'
    assert kw['body']['spec']['env'] == {'LEARTECH_AGENT_MODEL': 'm'}


@pytest.mark.asyncio
async def test_ensure_agent_type_patches_on_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(create_ct_status=409)
    _patch_api(monkeypatch, fake)
    await arc.ensure_agent_type(language='go', image='img:2')
    verbs = [c[0] for c in fake.calls]
    assert verbs == ['create_ct', 'patch_ct']
    assert fake.calls[1][1]['_content_type'] == 'application/merge-patch+json'


@pytest.mark.asyncio
async def test_ensure_agent_type_reraises_non_409(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(create_ct_status=500))
    with pytest.raises(ApiException):
        await arc.ensure_agent_type(language='py', image='img')


@pytest.mark.asyncio
async def test_create_agent_run_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, fake)
    rid = await arc.create_agent_run(
        run_id='abc',
        namespace='ns',
        agent_type='leartech-agent-py',
        repo='mikelear/x',
        inputs={'name': 'i'},
        tenant_id='t1',
    )
    assert rid == 'abc'
    _, kw = fake.calls[0]
    body = kw['body']
    assert body['metadata']['name'] == 'abc'
    assert body['spec'] == {
        'agentType': 'leartech-agent-py',
        'repo': 'mikelear/x',
        'inputs': {'name': 'i'},
        'tenant': 't1',
    }


@pytest.mark.asyncio
async def test_list_agent_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi())
    items = await arc.list_agent_runs('ns')
    assert items == [{'metadata': {'name': 'r1'}}]


@pytest.mark.asyncio
async def test_delete_agent_run_swallows_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(delete_status=404))
    await arc.delete_agent_run('abc', 'ns')  # must not raise


@pytest.mark.asyncio
async def test_delete_agent_run_reraises_other(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(delete_status=500))
    with pytest.raises(ApiException):
        await arc.delete_agent_run('abc', 'ns')


@pytest.mark.asyncio
async def test_get_target_pr_returns_string(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(get_status_body={'status': {'targetPR': '42'}})
    _patch_api(monkeypatch, fake)
    assert await arc.get_target_pr('abc', 'ns') == '42'
    verb, kw = fake.calls[0]
    assert verb == 'get_status'
    assert kw['plural'] == arc._AGENTRUNS
    assert kw['name'] == 'abc'
    assert fake.closed  # api client always closed


@pytest.mark.asyncio
async def test_get_target_pr_empty_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(get_status_body={'status': {}}))
    assert await arc.get_target_pr('abc', 'ns') is None


@pytest.mark.asyncio
async def test_get_target_pr_none_when_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(get_status_body={}))
    assert await arc.get_target_pr('abc', 'ns') is None


@pytest.mark.asyncio
async def test_get_target_pr_swallows_api_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(get_status_exc=ApiException(status=404))
    _patch_api(monkeypatch, fake)
    assert await arc.get_target_pr('abc', 'ns') is None  # must not raise
    assert fake.closed


@pytest.mark.asyncio
async def test_get_target_pr_swallows_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(get_status_exc=RuntimeError('boom')))
    assert await arc.get_target_pr('abc', 'ns') is None  # must not raise


@pytest.mark.asyncio
async def test_patch_target_pr_merge_patch_string_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi()
    _patch_api(monkeypatch, fake)
    await arc.patch_target_pr('abc', 'ns', 42)
    verb, kw = fake.calls[0]
    assert verb == 'patch_status'
    assert kw['plural'] == arc._AGENTRUNS
    assert kw['name'] == 'abc'
    assert kw['_content_type'] == 'application/merge-patch+json'
    assert kw['body'] == {'status': {'targetPR': '42'}}  # STRING in the CRD
    assert fake.closed


@pytest.mark.asyncio
async def test_patch_target_pr_swallows_api_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeApi(patch_status_exc=ApiException(status=403))
    _patch_api(monkeypatch, fake)
    await arc.patch_target_pr('abc', 'ns', 42)  # must not raise
    assert fake.closed


@pytest.mark.asyncio
async def test_patch_target_pr_swallows_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_api(monkeypatch, _FakeApi(patch_status_exc=RuntimeError('boom')))
    await arc.patch_target_pr('abc', 'ns', 42)  # must not raise
