"""Contract test: the CR bodies agentrun_client builds MUST satisfy the committed
CRD openAPIV3Schema. This is the one blind spot the fake k8s client can't catch —
it accepts any dict, so a client that drifts from the CRD (missing a required field,
wrong type) ships green until it 422s on a real apiserver. The CRDs are vendored from
the controller's config/crd/bases; regenerate them (controller-gen) if the schema drifts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
import yaml

import gate.agent.agentrun_client as arc

CRD_DIR = Path(__file__).parent / 'testdata' / 'crds'


def _spec_schema(crd_file: str) -> dict[str, Any]:
    doc = yaml.safe_load((CRD_DIR / crd_file).read_text())
    schema: dict[str, Any] = doc['spec']['versions'][0]['schema']['openAPIV3Schema']['properties']['spec']
    return schema


class _CaptureApi:
    """Captures the CR body the client builds (no real k8s call)."""

    def __init__(self) -> None:
        self.body: dict[str, Any] | None = None
        self.api_client = SimpleNamespace(close=self._close)

    async def _close(self) -> None: ...

    async def create_namespaced_custom_object(self, **kw: Any) -> None:
        self.body = kw['body']

    async def create_cluster_custom_object(self, **kw: Any) -> None:
        self.body = kw['body']


async def _capture_body(monkeypatch: pytest.MonkeyPatch, build: Any) -> dict[str, Any]:
    fake = _CaptureApi()

    async def _api() -> Any:
        return fake

    monkeypatch.setattr(arc, '_custom_api', _api)
    await build()
    assert fake.body is not None
    return fake.body


@pytest.mark.asyncio
async def test_agentrun_body_satisfies_crd_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _capture_body(
        monkeypatch,
        lambda: arc.create_agent_run(
            run_id='r1',
            namespace='ns',
            agent_type='leartech-agent-py',
            repo='mikelear/x',
            inputs={'name': 'i', 'goal': 'g'},
            tenant_id='t1',
        ),
    )
    jsonschema.validate(instance=body['spec'], schema=_spec_schema('agent.leartech.io_agentruns.yaml'))


@pytest.mark.asyncio
async def test_agenttype_body_satisfies_crd_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    body = await _capture_body(
        monkeypatch,
        lambda: arc.ensure_agent_type(language='py', image='img:1', env={'LEARTECH_AGENT_MODEL': 'm'}),
    )
    jsonschema.validate(instance=body['spec'], schema=_spec_schema('agent.leartech.io_agenttypes.yaml'))


def test_contract_bites_on_drift() -> None:
    # Proof the guard actually fails on drift: an AgentRun spec missing the
    # required agentType must be rejected by the schema.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance={'inputs': {}}, schema=_spec_schema('agent.leartech.io_agentruns.yaml'))
