"""Tests for gate.agent.infra_agent — the infra-agent entrypoint wiring (local CoS).

Cluster CoS (deferred): the infra agent runs the repo-factory Plan end-to-end and a
hello-world service reaches /health=200 on both clusters. Here we prove the entrypoint is
wired: write-mode + MCP tools granted, the deterministic-scaffold rule is in the prompt,
the infra_agent role exists in the catalog and references only real MCPs.
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


def test_allowed_tools_grant_repo_factory_and_open_pr() -> None:
    tools = infra_agent.INFRA_ALLOWED_TOOLS
    assert {'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'} <= set(tools)
    assert 'mcp__leartech-pr-context__open_pr' in tools
    # deterministic repo ops go through the server-side repo-factory MCP
    for t in ('create_repo', 'register_source_config', 'scaffold'):
        assert f'mcp__leartech-repo-factory__{t}' in tools
    # the JX3 release check goes through the jx-release MCP
    for t in ('release_status', 'promote_status', 'retest_promote'):
        assert f'mcp__leartech-jx-release__{t}' in tools


def test_system_prompt_routes_to_repo_factory_mcp_and_two_clusters() -> None:
    prompt = infra_agent._build_system_prompt()
    assert 'mcp__leartech-repo-factory__' in prompt  # deterministic ops via the MCP, not Bash
    assert 'do not patch by hand' in prompt
    assert 'jx-build-cluster-gsm' in prompt and 'jx-build-cluster-akv' in prompt
    assert 'release-health-check' in prompt  # the merged!=healthy verification action
    # the release check composes the jx-release MCP (promote across clusters) + escalates gate-fails
    assert 'mcp__leartech-jx-release__promote_status' in prompt
    assert 'needs-cross-plan-Infra-agent' in prompt
    # scaffold MUST pass run_id/namespace so it publishes targetPR -> step reaches
    # AwaitingReview (else a repo-backed scaffold step fails as "opened no PR").
    assert 'run_id=$LEARTECH_RUN_ID' in prompt and 'namespace=$AGENT_RUN_NAMESPACE' in prompt


def test_task_prompt_embeds_action_and_inputs() -> None:
    out = infra_agent._task_prompt('create-repo', {'newRepo': 'mikelear/hello-go'})
    assert 'create-repo' in out
    assert 'mikelear/hello-go' in out


def test_run_infra_task_returns_2_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_last_health_verdict_parses_and_takes_last() -> None:
    assert infra_agent._last_health_verdict('all good\nRELEASE_HEALTH: PASS') == 'PASS'
    assert infra_agent._last_health_verdict('RELEASE_HEALTH: FAIL: no deployment') == 'FAIL'
    assert infra_agent._last_health_verdict('nothing to see here') is None
    # narration before the verdict, and the LAST verdict wins
    multi = 'RELEASE_HEALTH: FAIL: rollout incomplete\n...retried...\nRELEASE_HEALTH: PASS'
    assert infra_agent._last_health_verdict(multi) == 'PASS'


def test_resolve_exit_code_health_check_fails_closed() -> None:
    # release-health-check: only an explicit PASS survives; FAIL/MISSING force 1 even when the
    # SDK loop reported success (is_error=False -> sdk_exit_code=0). Closes the false-success.
    assert infra_agent._resolve_exit_code('release-health-check', 0, 'PASS') == 0
    assert infra_agent._resolve_exit_code('release-health-check', 0, 'FAIL') == 1
    assert infra_agent._resolve_exit_code('release-health-check', 0, None) == 1
    # other actions keep the SDK-derived code untouched
    assert infra_agent._resolve_exit_code('create-repo', 0, None) == 0
    assert infra_agent._resolve_exit_code('create-repo', 1, 'PASS') == 1


def test_release_health_check_prompt_demands_verdict_and_fails_closed() -> None:
    prompt = infra_agent.INFRA_SYSTEM_PROMPT
    assert 'RELEASE_HEALTH: PASS' in prompt
    assert 'RELEASE_HEALTH: FAIL' in prompt
    assert 'never a PASS' in prompt  # "not deployed yet" is a FAIL


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
    assert captured['params'] == {'newRepo': 'mikelear/hello-go'}  # action key stripped from params


def test_main_errors_when_no_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_INITIATIVE_YAML', raising=False)
    with pytest.raises(click.BadParameter, match='no inputs'):
        infra_agent.main.callback(action=None, inputs_opt=None, model='m', max_turns=1)
