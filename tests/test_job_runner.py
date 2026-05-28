"""Tests for `gate.agent.job_runner` — D.3 Job-spawn primitive.

The unit under test is a pure function whose only side-effect is calling
the K8s BatchV1 API. So tests pin two surfaces:

1. **Manifest shape** — the dict produced by `_build_job_manifest` must
   match the chart's `_job-template.tpl` (labels, securityContext, env
   carriage, resource policy). Drift between Python and the chart
   template is the most likely future bug; the manifest tests are the
   tripwire.

2. **API plumbing** — `spawn_initiative_job` must load incluster
   config, instantiate BatchV1Api, call `create_namespaced_job` with
   the right namespace, and surface 409 conflicts unchanged so callers
   can detect duplicate-run-id as a distinct case.

We mock `kubernetes_asyncio.{client,config}` rather than reaching for
a fake cluster — the test surface is the manifest dict, not real K8s
behaviour.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from gate.agent.job_runner import (
    DEFAULT_SERVICE_ACCOUNT,
    DEFAULT_TTL_SECONDS_AFTER_FINISHED,
    _build_job_manifest,
    _default_resources,
    spawn_initiative_job,
)

# ---------------------------------------------------------------------------
# _build_job_manifest — pure structural tests, no async, no mocks.
# ---------------------------------------------------------------------------


def _baseline_manifest(**overrides: Any) -> dict[str, Any]:
    """Standard manifest for assertion fixtures. Overrides let individual
    tests vary one field while holding the rest constant."""
    defaults: dict[str, Any] = {
        'run_id': 'run-abc123',
        'initiative': 'demo-feature',
        'image': 'ghcr.io/mikelear/leartech-automated-agent:0.2.0',
        'namespace': 'jx-staging',
        'service_account': DEFAULT_SERVICE_ACCOUNT,
        'env': {'LEARTECH_REPO_ROOT': '/workspace'},
        'secret_refs': {},
        'resources': _default_resources(),
    }
    defaults.update(overrides)
    return _build_job_manifest(**defaults)


def test_manifest_has_required_labels_on_both_job_and_pod_template() -> None:
    """Labels are the primary index for queries: `leartech.io/initiative`,
    `leartech.io/run-id`, and `leartech.io/component=initiative-runner`
    must appear on BOTH the Job metadata and the pod template metadata
    (Selector queries on label can target either; missing one half
    means kubectl label-selectors miss pods/jobs depending on the
    intent of the query)."""
    m = _baseline_manifest()
    expected_labels = {
        'leartech.io/initiative': 'demo-feature',
        'leartech.io/run-id': 'run-abc123',
        'leartech.io/component': 'initiative-runner',
    }
    assert m['metadata']['labels'] == expected_labels
    assert m['spec']['template']['metadata']['labels'] == expected_labels


def test_manifest_job_name_equals_run_id() -> None:
    """Run-id is the Job's name so callers can `get` it by name later
    without label selectors. If this ever changes, the status
    reconciler (D.5) and any external observability tooling that pivots
    off run_id will need updating."""
    m = _baseline_manifest(run_id='run-xyz999')
    assert m['metadata']['name'] == 'run-xyz999'


def test_manifest_uses_specified_namespace() -> None:
    m = _baseline_manifest(namespace='jx-preview-foo')
    assert m['metadata']['namespace'] == 'jx-preview-foo'


def test_manifest_plain_env_vars_use_value_form() -> None:
    """Plain env vars carry `value`, NOT `valueFrom`. Mixing the two
    forms in a single entry is a K8s API error."""
    m = _baseline_manifest(
        env={
            'LEARTECH_REPO_ROOT': '/workspace',
            'INITIATIVE_RUN_ID': 'run-abc123',
        }
    )
    env = m['spec']['template']['spec']['containers'][0]['env']
    env_by_name = {e['name']: e for e in env}
    assert env_by_name['LEARTECH_REPO_ROOT'] == {'name': 'LEARTECH_REPO_ROOT', 'value': '/workspace'}
    assert env_by_name['INITIATIVE_RUN_ID'] == {'name': 'INITIATIVE_RUN_ID', 'value': 'run-abc123'}


def test_manifest_secret_refs_use_value_from_form() -> None:
    """secretKeyRef-sourced env vars must carry `valueFrom`, never a
    `value` key. The chart template uses the same shape — see
    `_job-template.tpl` `range $name, $ref := .secretRefs`."""
    m = _baseline_manifest(
        env={'NORMAL_VAR': 'plain'},
        secret_refs={
            'CLAUDE_API_KEY': {'secret': 'ai-review-api-keys', 'key': 'CLAUDE_API_KEY'},
            'GH_TOKEN': {'secret': 'tekton-git', 'key': 'password'},
        },
    )
    env = m['spec']['template']['spec']['containers'][0]['env']
    env_by_name = {e['name']: e for e in env}

    claude = env_by_name['CLAUDE_API_KEY']
    assert 'value' not in claude
    assert claude['valueFrom'] == {'secretKeyRef': {'name': 'ai-review-api-keys', 'key': 'CLAUDE_API_KEY'}}

    gh = env_by_name['GH_TOKEN']
    assert 'value' not in gh
    assert gh['valueFrom'] == {'secretKeyRef': {'name': 'tekton-git', 'key': 'password'}}

    # Plain env still carries `value`.
    assert env_by_name['NORMAL_VAR'] == {'name': 'NORMAL_VAR', 'value': 'plain'}


def test_manifest_resource_limits_flow_through() -> None:
    """A custom resources dict must land verbatim on the container."""
    m = _baseline_manifest(
        resources={
            'requests': {'cpu': '1', 'memory': '2Gi'},
            'limits': {'cpu': '8', 'memory': '16Gi'},
        }
    )
    container = m['spec']['template']['spec']['containers'][0]
    assert container['resources'] == {
        'requests': {'cpu': '1', 'memory': '2Gi'},
        'limits': {'cpu': '8', 'memory': '16Gi'},
    }


def test_default_resources_match_chart_values() -> None:
    """`_default_resources()` is the fallback used when caller doesn't
    pass an explicit policy. Must match `values.yaml::jobs.resources`
    defaults so chart + Python agree on the unspecified case."""
    r = _default_resources()
    assert r == {
        'requests': {'cpu': '500m', 'memory': '1Gi'},
        'limits': {'cpu': '4', 'memory': '8Gi'},
    }


def test_manifest_pod_security_context_is_non_root() -> None:
    """Non-root + drop ALL caps is a leartech-wide cluster-policy
    requirement. The chart template enforces it; the Python builder
    must too, or the API server will reject the Job spec at admission."""
    m = _baseline_manifest()
    pod_sec = m['spec']['template']['spec']['securityContext']
    assert pod_sec == {'runAsNonRoot': True, 'runAsUser': 1000, 'fsGroup': 1000}

    container = m['spec']['template']['spec']['containers'][0]
    csec = container['securityContext']
    assert csec['runAsNonRoot'] is True
    assert csec['runAsUser'] == 1000
    assert csec['allowPrivilegeEscalation'] is False
    assert csec['capabilities']['drop'] == ['ALL']


def test_manifest_command_invokes_initiative_module() -> None:
    """The pod entrypoint is `python -m gate.agent.initiative` with
    `--initiative <name> --run-id <id>` — that's the documented entry
    point for the agent (see `gate.agent.initiative.main`)."""
    m = _baseline_manifest(initiative='demo-feature', run_id='run-abc123')
    container = m['spec']['template']['spec']['containers'][0]
    assert container['command'] == ['python', '-m', 'gate.agent.initiative']
    assert container['args'] == ['--initiative', 'demo-feature', '--run-id', 'run-abc123']


def test_manifest_uses_service_account_and_restart_never() -> None:
    m = _baseline_manifest(service_account='custom-runner-sa')
    pod_spec = m['spec']['template']['spec']
    assert pod_spec['serviceAccountName'] == 'custom-runner-sa'
    # Never restart — backoffLimit=0 + restartPolicy=Never means failures
    # surface immediately rather than retrying mid-run.
    assert pod_spec['restartPolicy'] == 'Never'
    assert m['spec']['backoffLimit'] == 0


def test_manifest_ttl_seconds_after_finished_default() -> None:
    """24-hour TTL gives humans a comfortable window to read failure
    logs before K8s garbage-collects the Job + Pod."""
    m = _baseline_manifest()
    assert m['spec']['ttlSecondsAfterFinished'] == DEFAULT_TTL_SECONDS_AFTER_FINISHED == 86400


def test_manifest_ttl_can_be_overridden() -> None:
    m = _build_job_manifest(
        run_id='r',
        initiative='i',
        image='img',
        namespace='ns',
        service_account='sa',
        env={},
        secret_refs={},
        resources=_default_resources(),
        ttl_seconds_after_finished=3600,
    )
    assert m['spec']['ttlSecondsAfterFinished'] == 3600


def test_manifest_image_pull_policy_is_always() -> None:
    """`Always` pulls match the chart template — the released image tag
    can be reused across runs, and we want the latest digest at every
    spawn rather than a stale cached image."""
    m = _baseline_manifest()
    container = m['spec']['template']['spec']['containers'][0]
    assert container['imagePullPolicy'] == 'Always'


def test_manifest_top_level_kind_and_api_version() -> None:
    m = _baseline_manifest()
    assert m['apiVersion'] == 'batch/v1'
    assert m['kind'] == 'Job'


# ---------------------------------------------------------------------------
# spawn_initiative_job — async, mocked-K8s tests.
# ---------------------------------------------------------------------------


def _mock_create_response(job_name: str, namespace: str) -> MagicMock:
    """Mimic the V1Job response shape that BatchV1Api.create_namespaced_job
    returns — only the `.metadata.name` and `.metadata.namespace` fields
    are consumed by the unit under test."""
    resp = MagicMock()
    resp.metadata.name = job_name
    resp.metadata.namespace = namespace
    return resp


@pytest.mark.asyncio
async def test_spawn_calls_load_incluster_config() -> None:
    """In-cluster auth is the only auth path supported. Skipping the
    load means the SDK falls back to no auth and the create call 401s
    against the API server — surface it loud, not silent."""
    with (
        patch('gate.agent.job_runner.config') as mock_config,
        patch('gate.agent.job_runner.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_runner.client') as mock_client_mod,
    ):
        mock_config.load_incluster_config = AsyncMock()
        # ApiClient is used as `async with ApiClient() as api: ...` —
        # mock the async context manager.
        api_client_instance = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=api_client_instance)
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        batch.create_namespaced_job = AsyncMock(return_value=_mock_create_response('run-1', 'jx-staging'))
        mock_client_mod.BatchV1Api.return_value = batch

        await spawn_initiative_job(
            initiative_name='demo',
            run_id='run-1',
            image='ghcr.io/foo:1',
            namespace='jx-staging',
            env={},
        )

    mock_config.load_incluster_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_submits_manifest_to_batch_v1() -> None:
    """The unit must call `create_namespaced_job` on BatchV1Api with the
    target namespace and a body that matches `_build_job_manifest`'s
    output. Asserting via the captured body ensures env + labels +
    image flow through end-to-end."""
    with (
        patch('gate.agent.job_runner.config') as mock_config,
        patch('gate.agent.job_runner.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_runner.client') as mock_client_mod,
    ):
        mock_config.load_incluster_config = AsyncMock()
        api_client_instance = MagicMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=api_client_instance)
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        batch.create_namespaced_job = AsyncMock(return_value=_mock_create_response('run-zz', 'jx-prod'))
        mock_client_mod.BatchV1Api.return_value = batch

        name, ns = await spawn_initiative_job(
            initiative_name='demo-feature',
            run_id='run-zz',
            image='ghcr.io/foo:9',
            namespace='jx-prod',
            env={'FOO': 'bar'},
            secret_refs={'CLAUDE_API_KEY': {'secret': 'ai-review-api-keys', 'key': 'CLAUDE_API_KEY'}},
        )

    # Return contract: (job_name, namespace) reflects the API server response.
    assert name == 'run-zz'
    assert ns == 'jx-prod'

    # BatchV1Api is constructed with the live api client.
    mock_client_mod.BatchV1Api.assert_called_once_with(api_client_instance)
    batch.create_namespaced_job.assert_awaited_once()
    call_kwargs = batch.create_namespaced_job.await_args.kwargs
    assert call_kwargs['namespace'] == 'jx-prod'

    # The body is the manifest dict; spot-check the bits most prone to
    # accidental drift (labels, image, env carriage).
    body = call_kwargs['body']
    assert body['metadata']['name'] == 'run-zz'
    assert body['metadata']['labels']['leartech.io/initiative'] == 'demo-feature'
    assert body['spec']['template']['spec']['containers'][0]['image'] == 'ghcr.io/foo:9'
    env = body['spec']['template']['spec']['containers'][0]['env']
    env_names = {e['name'] for e in env}
    assert env_names == {'FOO', 'CLAUDE_API_KEY'}


@pytest.mark.asyncio
async def test_spawn_uses_default_service_account_when_unspecified() -> None:
    """Default SA is the chart-provisioned `*-job-runner`. Overriding
    must work too, but the default is what 95% of callers use."""
    with (
        patch('gate.agent.job_runner.config') as mock_config,
        patch('gate.agent.job_runner.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_runner.client') as mock_client_mod,
    ):
        mock_config.load_incluster_config = AsyncMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        batch.create_namespaced_job = AsyncMock(return_value=_mock_create_response('r', 'ns'))
        mock_client_mod.BatchV1Api.return_value = batch

        await spawn_initiative_job(
            initiative_name='demo',
            run_id='r',
            image='img',
            namespace='ns',
            env={},
        )

    body = batch.create_namespaced_job.await_args.kwargs['body']
    sa = body['spec']['template']['spec']['serviceAccountName']
    assert sa == DEFAULT_SERVICE_ACCOUNT == 'leartech-automated-agent-job-runner'


@pytest.mark.asyncio
async def test_spawn_uses_default_resources_when_unspecified() -> None:
    with (
        patch('gate.agent.job_runner.config') as mock_config,
        patch('gate.agent.job_runner.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_runner.client') as mock_client_mod,
    ):
        mock_config.load_incluster_config = AsyncMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        batch.create_namespaced_job = AsyncMock(return_value=_mock_create_response('r', 'ns'))
        mock_client_mod.BatchV1Api.return_value = batch

        await spawn_initiative_job(
            initiative_name='demo',
            run_id='r',
            image='img',
            namespace='ns',
            env={},
        )

    body = batch.create_namespaced_job.await_args.kwargs['body']
    res = body['spec']['template']['spec']['containers'][0]['resources']
    assert res == _default_resources()


@pytest.mark.asyncio
async def test_spawn_propagates_409_conflict_unchanged() -> None:
    """If the API server returns 409 (Job with this name already exists),
    the helper must re-raise the original ApiException so callers can
    classify it as a duplicate-run rather than a transient retryable
    error. Swallowing 409 would mask real idempotency bugs upstream."""
    with (
        patch('gate.agent.job_runner.config') as mock_config,
        patch('gate.agent.job_runner.ApiClient') as mock_api_client_cls,
        patch('gate.agent.job_runner.client') as mock_client_mod,
    ):
        mock_config.load_incluster_config = AsyncMock()
        mock_api_client_cls.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_api_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
        batch = MagicMock()
        batch.create_namespaced_job = AsyncMock(side_effect=ApiException(status=409, reason='Conflict'))
        mock_client_mod.BatchV1Api.return_value = batch

        with pytest.raises(ApiException) as exc_info:
            await spawn_initiative_job(
                initiative_name='demo',
                run_id='run-dup',
                image='img',
                namespace='ns',
                env={},
            )

    assert exc_info.value.status == 409
