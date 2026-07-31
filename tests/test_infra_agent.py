"""Tests for gate.agent.infra_agent — the infra-agent entrypoint wiring (local CoS).

Cluster CoS (deferred): the infra agent runs the repo-factory Plan end-to-end and a
hello-world service reaches /health=200 on both clusters. Here we prove the entrypoint is
wired: write-mode + MCP tools granted, the deterministic-scaffold rule is in the prompt,
the infra_agent role exists in the catalog and references only real MCPs.

The release-health-check verdict is pinned deterministic:
    * given release fired + promotes merged + a stubbed HTTP endpoint returning 200 →
      verdict is PASS with NO kubectl available;
    * given the endpoint returning 502 then 200 → retries then PASSES;
    * given persistent non-200 → FAILS with a clear reason;
    * verdict does NOT depend on kubectl availability.
See ``tests/test_release_health.py`` for the probe-level pins; here we focus on the
infra-agent-wired verdict function.
"""

from __future__ import annotations

import asyncio

import click
import pytest

from gate.agent import infra_agent
from gate.agent.mcp_catalog import load_catalog
from gate.agent.release_health import ProbeResult


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
    for t in ('create_repo', 'register_source_config', 'scaffold', 'smoke_pr'):
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


# ── release-health-check: deterministic verdict path ──────────────────────────
#
# The whole point of moving the verdict off the LLM is that the outcome must be a
# function of endpoint responses (or an explicit stage-1-3 LLM FAIL), never of what
# the model decided to say. These pins guard that.


def test_last_llm_fail_reason_captures_stage_1_3_failures() -> None:
    """LLM can only DECLARE FAIL from the transcript — PASS is never accepted there."""
    assert infra_agent._last_llm_fail_reason('RELEASE_HEALTH: FAIL: release did not fire') == 'release did not fire'
    # No reason after the colon → placeholder, still recognised as FAIL.
    assert infra_agent._last_llm_fail_reason('RELEASE_HEALTH: FAIL') is not None
    # Nothing at all → the probe decides.
    assert infra_agent._last_llm_fail_reason('all good') is None
    # A stray "PASS" from the LLM is IGNORED — determinism guardrail.
    assert infra_agent._last_llm_fail_reason('RELEASE_HEALTH: PASS') is None
    # Last FAIL wins.
    multi = 'RELEASE_HEALTH: FAIL: transient\n...retried...\nRELEASE_HEALTH: FAIL: final'
    assert infra_agent._last_llm_fail_reason(multi) == 'final'


def test_health_check_verdict_uses_llm_fail_and_skips_probe() -> None:
    """When the LLM declares a stage-1-3 FAIL, the probe is NOT called — no targets to probe."""
    probes_called: list[list[str]] = []

    def _fake_probe(targets: list[str], **_: object) -> ProbeResult:
        probes_called.append(targets)
        return ProbeResult(verdict='PASS', reason=None, probes=())

    result, targets = infra_agent._health_check_verdict(
        {'service': 'hello-go'},
        'RELEASE_HEALTH: FAIL: release did not fire within 60min',
        probe=_fake_probe,
    )
    assert result.verdict == 'FAIL'
    assert 'release did not fire' in (result.reason or '')
    assert targets == []
    assert probes_called == []  # probe never invoked


def test_health_check_verdict_runs_probe_when_stages_1_3_ok() -> None:
    """No LLM FAIL declared → resolve targets, run the deterministic probe."""

    def _fake_probe(targets: list[str], **_: object) -> ProbeResult:
        assert targets == ['https://hello-go.example.com/health']
        return ProbeResult(verdict='PASS', reason=None, probes=())

    result, targets = infra_agent._health_check_verdict(
        {'host': 'hello-go.example.com', 'healthPath': '/health'},
        'HEALTH_TARGETS: https://hello-go.example.com/health',
        probe=_fake_probe,
    )
    assert result.verdict == 'PASS'
    assert targets == ['https://hello-go.example.com/health']


def test_health_check_verdict_probe_fail_makes_verdict_fail() -> None:
    """A probe FAIL becomes the verdict — the LLM has no say."""

    def _fake_probe(_targets: list[str], **_: object) -> ProbeResult:
        return ProbeResult(verdict='FAIL', reason='HTTP 500 from …/health', probes=())

    result, _ = infra_agent._health_check_verdict(
        {'host': 'hello-go.example.com'},
        'HEALTH_TARGETS: https://hello-go.example.com/health',
        probe=_fake_probe,
    )
    assert result.verdict == 'FAIL'
    assert 'HTTP 500' in (result.reason or '')


def test_health_check_verdict_no_targets_fails() -> None:
    """Missing HEALTH_TARGETS and no inputs → FAIL (no PASS by silence)."""
    result, targets = infra_agent._health_check_verdict(
        {'service': 'hello-go'},  # no healthUrl / host
        'stages completed but I forgot to emit targets',
    )
    assert result.verdict == 'FAIL'
    assert targets == []


def test_release_health_check_prompt_demands_targets_and_disowns_stage_4_verdict() -> None:
    """Prompt: LLM emits HEALTH_TARGETS on stage 4 and does NOT decide PASS itself."""
    prompt = infra_agent.INFRA_SYSTEM_PROMPT
    assert 'HEALTH_TARGETS:' in prompt
    # The historical "output PASS if you observed ..." improvisation path must be gone.
    assert 'RELEASE_HEALTH: FAIL' in prompt  # early-exit still allowed
    assert 'DETERMINISTIC' in prompt or 'deterministic' in prompt
    # kubectl-absent is explicitly not-a-fail (closes the LLM asymmetry). Case-insensitive
    # so a future re-word of "NEVER a reason to fail" -> "never a reason to fail" still passes.
    lowered = prompt.lower()
    assert 'never a reason to fail' in lowered or 'never let its absence' in lowered


def test_release_health_budget_seconds_reads_probe_budget() -> None:
    assert infra_agent._release_health_budget_seconds({}) is None
    assert infra_agent._release_health_budget_seconds({'probeBudgetSeconds': 45}) == 45.0
    # Malformed → None (probe uses its own default).
    assert infra_agent._release_health_budget_seconds({'probeBudgetSeconds': 'nope'}) is None


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
