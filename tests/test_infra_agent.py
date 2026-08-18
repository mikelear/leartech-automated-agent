"""Tests for gate.agent.infra_agent — the infra-agent entrypoint wiring (local CoS).

Cluster CoS (deferred): the infra agent runs the repo-factory Plan end-to-end and a
hello-world service reaches deploy_health=healthy on both clusters. Here we prove the
entrypoint is wired: write-mode + MCP tools granted (incl. k8s + jx-release), the
deterministic-scaffold rule is in the prompt, the infra_agent role exists in the
catalog and references only real MCPs.

The release-verify checks are DETERMINISTIC and no-LLM: they short-circuit via
``is_check_action`` in ``run_infra_task`` and are covered by
``tests/test_release_checks.py``. The legacy LLM-transcribed STAGE_STATUS
release-health-check machinery has been removed; here we focus on the
repo-factory prompt / tool-surface contracts and the entrypoint plumbing.
"""

from __future__ import annotations

import asyncio

import click
import pytest

from gate.agent import infra_agent
from gate.agent.mcp_catalog import load_catalog


def test_infra_role_in_catalog_references_real_mcps() -> None:
    catalog = load_catalog()
    assert 'infra_agent' in catalog.roles
    role = catalog.roles['infra_agent']
    assert {'Read', 'Write', 'Edit', 'Bash'} <= set(role.tools)
    for mcp in role.mcps:
        assert mcp in catalog.mcp_servers, f'infra_agent references unknown MCP {mcp!r}'
    assert 'leartech-k8s' in role.mcps
    assert 'leartech-jx-release' in role.mcps
    assert 'leartech-k8s' in catalog.mcp_servers
    assert 'leartech-jx-release' in catalog.mcp_servers


def test_allowed_tools_grant_repo_factory_open_pr_jx_release_and_k8s() -> None:
    tools = infra_agent.INFRA_ALLOWED_TOOLS
    assert {'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'} <= set(tools)
    assert 'mcp__leartech-pr-context__open_pr' in tools
    for t in ('create_repo', 'register_source_config', 'scaffold', 'smoke_pr'):
        assert f'mcp__leartech-repo-factory__{t}' in tools
    for t in ('release_status', 'promote_status', 'retest_promote'):
        assert f'mcp__leartech-jx-release__{t}' in tools
    for t in ('deploy_health', 'get_job_state', 'list_jobs_by_label'):
        assert f'mcp__leartech-k8s__{t}' in tools


def test_system_prompt_routes_to_repo_factory() -> None:
    prompt = infra_agent._build_system_prompt()
    assert 'mcp__leartech-repo-factory__' in prompt
    assert 'do not patch by hand' in prompt
    assert 'jx-build-cluster-gsm' in prompt and 'jx-build-cluster-akv' in prompt
    assert 'run_id=$LEARTECH_RUN_ID' in prompt and 'namespace=$AGENT_RUN_NAMESPACE' in prompt


def test_system_prompt_documents_repo_factory_actions() -> None:
    """The repo-factory ACTIONS the LLM path still owns must be documented."""
    prompt = infra_agent.INFRA_SYSTEM_PROMPT
    for action in ('create-repo', 'register-source-config', 'scaffold-pr', 'smoke-pr'):
        assert action in prompt, f'{action} missing from INFRA_SYSTEM_PROMPT'
    assert 'release-verify checks' in prompt or 'release_checks.py' in prompt
    assert 'STAGE_STATUS' not in prompt
    assert 'release-health-check' not in prompt


def test_task_prompt_embeds_action_and_inputs() -> None:
    out = infra_agent._task_prompt('create-repo', {'newRepo': 'mikelear/hello-go'})
    assert 'create-repo' in out
    assert 'mikelear/hello-go' in out


def test_run_infra_task_returns_2_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-check action (create-repo) reaches the api-key gate and returns 2
    when ANTHROPIC_API_KEY is unset (the deterministic check actions would
    short-circuit BEFORE the gate)."""
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    rc = asyncio.run(infra_agent.run_infra_task('create-repo', {'newRepo': 'x'}))
    assert rc == 2


def test_run_infra_task_reraises_sdk_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')

    async def _boom(*_args: object, **_kwargs: object):  # noqa: ANN202 — test stub async generator
        raise RuntimeError('sdk down')
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(infra_agent, 'query', _boom)
    with pytest.raises(RuntimeError, match='sdk down'):
        asyncio.run(infra_agent.run_infra_task('create-repo', {'newRepo': 'x'}))


def test_main_rejects_invalid_json_inputs() -> None:
    with pytest.raises(click.BadParameter, match='valid JSON'):
        infra_agent.main.callback(action='create-repo', inputs_opt='{not json', model='m', max_turns=1)

    with pytest.raises(click.BadParameter, match='JSON object'):
        infra_agent.main.callback(action='create-repo', inputs_opt='[]', model='m', max_turns=1)


def test_main_reads_action_and_params_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The controller contract: inputs (incl. action) arrive via $LEARTECH_INITIATIVE_YAML."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_YAML', '{"action": "create-repo", "newRepo": "mikelear/hello-go"}')
    captured: dict[str, object] = {}

    async def _fake(action: str, params: dict[str, object], **_kw: object) -> int:
        captured['action'] = action
        captured['params'] = params
        return 0

    monkeypatch.setattr(infra_agent, 'run_infra_task', _fake)
    with pytest.raises(SystemExit) as exc:
        infra_agent.main.callback(action=None, inputs_opt=None, model='m', max_turns=1)
    assert exc.value.code == 0
    assert captured['action'] == 'create-repo'
    assert captured['params'] == {'newRepo': 'mikelear/hello-go'}


def test_main_errors_when_no_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_INITIATIVE_YAML', raising=False)
    with pytest.raises(click.BadParameter, match='no inputs'):
        infra_agent.main.callback(action=None, inputs_opt=None, model='m', max_turns=1)


def test_authoring_capabilities_advertises_deterministic_deploy_health() -> None:
    """The BA-facing authoring capabilities YAML MUST advertise the deterministic
    ``deploy-health`` check as `available` (the canonical verify step) and MUST
    NOT still advertise the removed legacy STAGE_STATUS actions. Guard against
    silent drift between the infra surface + what the BA reads."""
    from pathlib import Path

    import yaml

    caps_path = Path(__file__).resolve().parent.parent / 'gate' / 'agent' / 'authoring_capabilities.yaml'
    data = yaml.safe_load(caps_path.read_text(encoding='utf-8'))
    actions = data['agent_types']['leartech-agent-infra']['actions']
    assert actions['deploy-health']['status'] == 'available'
    for removed in ('release-health-check', 'release-status', 'verify-gate', 'boot-status'):
        assert removed not in actions, f'{removed} should have been removed from authoring_capabilities.yaml'
