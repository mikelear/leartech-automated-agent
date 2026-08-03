"""Tests for gate.agent.infra_agent — the infra-agent entrypoint wiring (local CoS).

Cluster CoS (deferred): the infra agent runs the repo-factory Plan end-to-end and a
hello-world service reaches deploy_health=healthy on both clusters. Here we prove the
entrypoint is wired: write-mode + MCP tools granted (incl. k8s + jx-release), the
deterministic-scaffold rule is in the prompt, the infra_agent role exists in the
catalog and references only real MCPs.

The release-health-check verdict is pinned deterministic:
    * given all stages PASS via STAGE_STATUS lines → verdict PASS with NO httpx probe;
    * given a STAGE_STATUS FAIL → verdict FAIL naming the failing (stage, cluster);
    * given an early-exit RELEASE_HEALTH: FAIL → verdict FAIL with the reason;
    * a stray "RELEASE_HEALTH: PASS" from the LLM is IGNORED (determinism guardrail);
    * inputs.clusters pins the required set (single-cluster plan steps supported).
See ``tests/test_release_health.py`` for the aggregator-level pins; here we focus on
the infra-agent-wired verdict function and the prompt / tool-surface contracts.
"""

from __future__ import annotations

import asyncio

import click
import pytest

from gate.agent import infra_agent
from gate.agent.mcp_catalog import load_catalog
from gate.agent.release_health import ProbeResult, StageVerdict


def test_infra_role_in_catalog_references_real_mcps() -> None:
    catalog = load_catalog()
    assert 'infra_agent' in catalog.roles
    role = catalog.roles['infra_agent']
    assert {'Read', 'Write', 'Edit', 'Bash'} <= set(role.tools)
    for mcp in role.mcps:
        assert mcp in catalog.mcp_servers, f'infra_agent references unknown MCP {mcp!r}'
    # k8s + jx-release are REQUIRED for the deterministic release-health-check.
    assert 'leartech-k8s' in role.mcps
    assert 'leartech-jx-release' in role.mcps
    # And they must be declared as real MCP servers (not just referenced).
    assert 'leartech-k8s' in catalog.mcp_servers
    assert 'leartech-jx-release' in catalog.mcp_servers


def test_allowed_tools_grant_repo_factory_open_pr_jx_release_and_k8s() -> None:
    tools = infra_agent.INFRA_ALLOWED_TOOLS
    assert {'Read', 'Write', 'Edit', 'Bash', 'Glob', 'Grep'} <= set(tools)
    assert 'mcp__leartech-pr-context__open_pr' in tools
    # deterministic repo ops go through the server-side repo-factory MCP
    for t in ('create_repo', 'register_source_config', 'scaffold', 'smoke_pr'):
        assert f'mcp__leartech-repo-factory__{t}' in tools
    # the JX3 release check goes through the jx-release MCP
    for t in ('release_status', 'promote_status', 'retest_promote'):
        assert f'mcp__leartech-jx-release__{t}' in tools
    # stages 3 + 4 go through the k8s MCP (in-cluster reads; no kubectl on
    # the agent side, no unreachable-from-sandbox HTTP probe).
    for t in ('deploy_health', 'get_job_state', 'list_jobs_by_label'):
        assert f'mcp__leartech-k8s__{t}' in tools


def test_system_prompt_routes_to_repo_factory_and_composes_release_check_via_mcps() -> None:
    prompt = infra_agent._build_system_prompt()
    assert 'mcp__leartech-repo-factory__' in prompt  # deterministic ops via the MCP, not Bash
    assert 'do not patch by hand' in prompt
    assert 'jx-build-cluster-gsm' in prompt and 'jx-build-cluster-akv' in prompt
    assert 'release-health-check' in prompt  # the merged!=healthy verification action
    # the release check composes jx-release + tekton + k8s — every stage is an MCP call
    assert 'mcp__leartech-jx-release__promote_status' in prompt
    assert 'mcp__leartech-jx-release__release_status' in prompt
    assert 'mcp__leartech-tekton__list_pipelineruns_for_pr' in prompt
    assert 'mcp__leartech-k8s__deploy_health' in prompt
    assert 'mcp__leartech-k8s__list_jobs_by_label' in prompt
    # qa-gate escalation still routes to the cross-plan Infra-agent handoff
    assert 'needs-cross-plan-Infra-agent' in prompt
    # scaffold MUST pass run_id/namespace so it publishes targetPR -> step reaches
    # AwaitingReview (else a repo-backed scaffold step fails as "opened no PR").
    assert 'run_id=$LEARTECH_RUN_ID' in prompt and 'namespace=$AGENT_RUN_NAMESPACE' in prompt


def test_release_health_prompt_forbids_http_probe_and_pins_stage_status_grammar() -> None:
    """The prompt MUST forbid HTTP /health probes from the sandbox (the specific
    bug this refactor closes) and MUST pin the STAGE_STATUS grammar."""
    prompt = infra_agent.INFRA_SYSTEM_PROMPT
    # No httpx / ingress /health probe attempted from this sandbox.
    lowered = prompt.lower()
    assert 'never attempt an http get' in lowered
    assert 'cannot reach the ingress' in lowered
    # DETERMINISM CONTRACT explicit
    assert 'DETERMINISM CONTRACT' in prompt or 'DETERMINISTIC' in prompt or 'deterministic' in prompt
    # The exact STAGE_STATUS grammar is pinned in the prompt so the LLM emits
    # what the aggregator can parse.
    assert 'STAGE_STATUS:' in prompt
    assert 'stage=' in prompt and 'cluster=' in prompt and 'verdict=' in prompt
    # Stages 1-4 are all called out.
    for n in ('stage=1', 'stage=2', 'stage=3', 'stage=4'):
        assert n in prompt
    # No PASS-by-silence — missing STAGE_STATUS is a FAIL.
    assert 'PASS-by-silence' in prompt or 'no PASS-by-silence' in prompt.lower() or 'fail-closed' in lowered


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
# The whole point of moving the verdict off the LLM's free-form narration is
# that the outcome must be a function of parsed STAGE_STATUS lines (+ any
# early-exit RELEASE_HEALTH: FAIL), never of what the model decided to say.
# These pins guard that.


def _happy_transcript() -> str:
    return '\n'.join(
        [
            'STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=release v1.2.3 Succeeded',
            'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 merged',
            'STAGE_STATUS: stage=2 cluster=az verdict=PASS reason=promote PR #102 merged',
            'STAGE_STATUS: stage=3 cluster=gcp verdict=PASS reason=jx-boot Job succeeded',
            'STAGE_STATUS: stage=3 cluster=az verdict=PASS reason=jx-boot Job succeeded',
            'STAGE_STATUS: stage=4 cluster=gcp verdict=PASS reason=healthy=true available_replicas=2',
            'STAGE_STATUS: stage=4 cluster=az verdict=PASS reason=healthy=true available_replicas=2',
        ]
    )


def test_health_check_verdict_all_stages_pass() -> None:
    """All required (stage, cluster) pairs PASS → verdict PASS. This is the
    fix's headline: verdict determined by structured emissions, not narration."""
    result = infra_agent._health_check_verdict({'service': 'hello-go'}, _happy_transcript())
    assert result.verdict == 'PASS'
    assert result.reason is None
    assert result.failing_stage is None


def test_health_check_verdict_stage_fail_makes_verdict_fail() -> None:
    """A STAGE_STATUS FAIL flips the verdict; the failing stage + cluster
    surface on the ProbeResult for structured logs."""
    transcript = '\n'.join(
        [
            'STAGE_STATUS: stage=1 cluster=- verdict=PASS',
            'STAGE_STATUS: stage=4 cluster=az verdict=FAIL reason=healthy=false available_replicas=0',
        ]
    )
    result = infra_agent._health_check_verdict({'service': 'hello-go'}, transcript)
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 4
    assert result.failing_cluster == 'az'
    assert 'healthy=false' in (result.reason or '')


def test_health_check_verdict_early_exit_fail_shortcircuits() -> None:
    """An early-exit ``RELEASE_HEALTH: FAIL`` propagates verbatim as verdict
    reason — used when stages 1-3 hit a signal they can't proceed past
    (release didn't fire, gate-fail)."""
    transcript = 'RELEASE_HEALTH: FAIL: release did not fire within 60min'
    result = infra_agent._health_check_verdict({'service': 'hello-go'}, transcript)
    assert result.verdict == 'FAIL'
    assert 'release did not fire' in (result.reason or '')


def test_health_check_verdict_missing_stages_fail() -> None:
    """No STAGE_STATUS lines → FAIL with the first missing pair named.

    Prevents PASS-by-silence when the LLM narrates but forgets to emit the
    machine-readable lines."""
    result = infra_agent._health_check_verdict(
        {'service': 'hello-go'},
        'the release seems fine, deploys look good, /health is 200',  # narration only
    )
    assert result.verdict == 'FAIL'
    assert result.failing_stage == 1  # first missing coverage


def test_health_check_verdict_ignores_llm_pass_claim() -> None:
    """A stray ``RELEASE_HEALTH: PASS`` from the LLM must NOT influence the
    verdict — determinism guardrail against the historical "curled once and
    claimed PASS" improvisation."""
    transcript = 'RELEASE_HEALTH: PASS: I looked at kubectl and it seems fine'
    result = infra_agent._health_check_verdict({'service': 'hello-go'}, transcript)
    # No STAGE_STATUS coverage → FAIL, not PASS.
    assert result.verdict == 'FAIL'


def test_health_check_verdict_uses_aggregator_seam_for_isolation() -> None:
    """The verdict function accepts a fake ``aggregator`` so it can be unit-
    tested in isolation from the parser (belt-and-braces for future refactors
    of the aggregator's internals)."""
    calls: list[tuple[str, tuple[str, ...]]] = []

    def _fake(transcript: str, *, required_clusters: tuple[str, ...]) -> ProbeResult:
        calls.append((transcript, required_clusters))
        return ProbeResult(verdict='PASS', reason=None, stages=(), failing_stage=None)

    result = infra_agent._health_check_verdict(
        {'clusters': ['gcp', 'az']},
        'x',
        aggregator=_fake,
    )
    assert result.verdict == 'PASS'
    assert calls == [('x', ('gcp', 'az'))]


def test_resolve_required_clusters_defaults_to_both() -> None:
    assert infra_agent._resolve_required_clusters({}) == ('gcp', 'az')


def test_resolve_required_clusters_reads_list() -> None:
    assert infra_agent._resolve_required_clusters({'clusters': ['gcp']}) == ('gcp',)
    assert infra_agent._resolve_required_clusters({'clusters': ['az', 'gcp']}) == ('az', 'gcp')


def test_resolve_required_clusters_reads_single_string() -> None:
    assert infra_agent._resolve_required_clusters({'cluster': 'gcp'}) == ('gcp',)


def test_resolve_required_clusters_falls_back_on_malformed() -> None:
    """Bad inputs → default (both), not a crash. The aggregator's fail-closed
    semantics catch any real coverage gap regardless."""
    assert infra_agent._resolve_required_clusters({'clusters': []}) == ('gcp', 'az')
    assert infra_agent._resolve_required_clusters({'clusters': None}) == ('gcp', 'az')
    assert infra_agent._resolve_required_clusters({'cluster': ''}) == ('gcp', 'az')


def test_stage_verdict_dataclass_shape() -> None:
    """Sanity for the structured-log payload shape."""
    sv = StageVerdict(stage=4, cluster='gcp', verdict='PASS', reason='healthy=true')
    assert sv.stage == 4
    assert sv.cluster == 'gcp'
    assert sv.verdict == 'PASS'
    assert sv.reason == 'healthy=true'


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


# ── Individual single-stage release-check actions — dispatch + verdict wiring ─
#
# The five decomposed actions (release-status, promote-status, verify-gate,
# boot-status, deploy-health) all share the same DETERMINISM CONTRACT as the
# composed release-health-check but score verdicts against ONE stage of
# STAGE_STATUS lines and, on FAIL, emit a structured BA failure context the
# escalation carries to the spawned BA Agent. These tests pin the
# entrypoint-level wiring — the exit code + obslog + BA context flow through
# ``run_infra_task`` / ``_stage_action_verdict``.


def test_system_prompt_documents_individual_stage_actions() -> None:
    """The infra-agent prompt MUST document each of the five individual
    single-stage actions so the LLM knows which MCP to call and which
    STAGE_STATUS line to emit for a per-step Plan."""
    prompt = infra_agent.INFRA_SYSTEM_PROMPT
    # Section marker announcing the decomposed actions block.
    assert 'INDIVIDUAL SINGLE-STAGE' in prompt
    # Every action name appears.
    for name in ('release-status', 'promote-status', 'verify-gate', 'boot-status', 'deploy-health'):
        assert name in prompt, f'{name} missing from INFRA_SYSTEM_PROMPT'
    # Each stage-specific MCP is documented so the LLM knows which primitive
    # to call for the action (release-status = release_status + tekton,
    # promote-status / verify-gate = promote_status, boot-status = k8s Job MCPs,
    # deploy-health = k8s deploy_health).
    assert 'mcp__leartech-jx-release__release_status' in prompt
    assert 'mcp__leartech-jx-release__promote_status' in prompt
    assert 'mcp__leartech-k8s__list_jobs_by_label' in prompt
    assert 'mcp__leartech-k8s__deploy_health' in prompt
    # The prompt calls out the deterministic-verdict + BA-failure-context contract.
    assert 'ba_failure_context' in prompt.lower() or 'BA failure context' in prompt or 'BA_STAGE_GUIDANCE' in prompt


def test_stage_action_verdict_release_status_pass() -> None:
    """release-status PASS → exit-code-relevant PASS verdict (stage 1 only)."""
    transcript = 'STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=release Succeeded'
    result = infra_agent._stage_action_verdict(
        'release-status',
        {'service': 'hello-go'},
        transcript,
    )
    assert result.verdict == 'PASS'
    assert result.stage == 1
    assert result.ba_failure_context is None


def test_stage_action_verdict_release_status_fail_ba_context() -> None:
    """release-status FAIL surfaces the structured BA context: release_status
    + tekton MCPs + "release Tekton pipeline" remediation guidance."""
    transcript = (
        'STAGE_STATUS: stage=1 cluster=- verdict=FAIL '
        'reason=release PipelineRun hello-go-release-abc failed at step kaniko'
    )
    result = infra_agent._stage_action_verdict(
        'release-status',
        {'service': 'hello-go'},
        transcript,
    )
    assert result.verdict == 'FAIL'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['action'] == 'release-status'
    assert ctx['stage'] == 1
    assert 'mcp__leartech-jx-release__release_status' in ctx['mcp']
    assert 'release Tekton pipeline' in ctx['remediation_hint']


def test_stage_action_verdict_promote_status_requires_both_clusters_by_default() -> None:
    """promote-status defaults to requiring gcp AND az. Missing az → FAIL."""
    transcript = 'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 opened'
    result = infra_agent._stage_action_verdict(
        'promote-status',
        {'service': 'hello-go'},
        transcript,
    )
    assert result.verdict == 'FAIL'
    assert result.failing_cluster == 'az'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['action'] == 'promote-status'


def test_stage_action_verdict_promote_status_respects_inputs_clusters() -> None:
    """A single-cluster promote-status only requires PASS on THAT cluster."""
    transcript = 'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 opened'
    result = infra_agent._stage_action_verdict(
        'promote-status',
        {'service': 'hello-go', 'clusters': ['gcp']},
        transcript,
    )
    assert result.verdict == 'PASS'


def test_stage_action_verdict_verify_gate_fail_names_qa_gate() -> None:
    """verify-gate FAIL on qa-gate red → BA context includes retest_promote guidance."""
    transcript = '\n'.join(
        [
            'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 merged',
            'STAGE_STATUS: stage=2 cluster=az verdict=FAIL reason=qa-gate failed on promote PR #102',
        ]
    )
    result = infra_agent._stage_action_verdict(
        'verify-gate',
        {'service': 'hello-go'},
        transcript,
    )
    assert result.verdict == 'FAIL'
    assert result.failing_cluster == 'az'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert 'retest_promote' in ctx['mcp']


def test_stage_action_verdict_boot_status_fail_names_job() -> None:
    """boot-status FAIL surfaces the Job name in mcp_returned."""
    transcript = (
        'STAGE_STATUS: stage=3 cluster=gcp verdict=FAIL reason=jx-boot Job hello-go-boot-101 failed at helmfile-apply'
    )
    result = infra_agent._stage_action_verdict(
        'boot-status',
        {'service': 'hello-go', 'clusters': ['gcp']},
        transcript,
    )
    assert result.verdict == 'FAIL'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['action'] == 'boot-status'
    assert 'jx-boot Job hello-go-boot-101' in (ctx['mcp_returned'] or '')
    assert 'helmfile-apply' in (ctx['mcp_returned'] or '')


def test_stage_action_verdict_deploy_health_pass() -> None:
    """deploy-health PASS on both clusters → verdict PASS."""
    transcript = '\n'.join(
        [
            'STAGE_STATUS: stage=4 cluster=gcp verdict=PASS reason=healthy=true available_replicas=2',
            'STAGE_STATUS: stage=4 cluster=az verdict=PASS reason=healthy=true available_replicas=2',
        ]
    )
    result = infra_agent._stage_action_verdict(
        'deploy-health',
        {'service': 'hello-go'},
        transcript,
    )
    assert result.verdict == 'PASS'


def test_stage_action_verdict_deploy_health_fail_ba_context_forbids_httpx() -> None:
    """deploy-health FAIL BA context tells the BA NEVER to reintroduce the
    httpx probe (the specific bug this refactor closed)."""
    transcript = (
        'STAGE_STATUS: stage=4 cluster=az verdict=FAIL '
        'reason=healthy=false available_replicas=0 desired_replicas=1 deployment not ready'
    )
    result = infra_agent._stage_action_verdict(
        'deploy-health',
        {'clusters': ['az']},
        transcript,
    )
    assert result.verdict == 'FAIL'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['action'] == 'deploy-health'
    # The remediation hint MUST call out that httpx from this sandbox is
    # the historical bug — no future BA-driven remediation should reintroduce it.
    assert 'httpx' in ctx['remediation_hint']


def test_stage_action_verdict_unknown_action_fails_closed() -> None:
    """An unknown action name (typo / drift) fails closed with a specific
    BA context rather than silently PASSing."""
    result = infra_agent._stage_action_verdict('bogus-action', {}, '')
    assert result.verdict == 'FAIL'
    assert result.reason is not None and 'unknown' in result.reason
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['action'] == 'bogus-action'
    assert 'unregistered' in ctx['remediation_hint'] or 'Add the action' in ctx['remediation_hint']


def test_stage_action_verdict_missing_stage_status_fails_closed() -> None:
    """No STAGE_STATUS lines for the action's stage → FAIL (no PASS-by-silence)."""
    result = infra_agent._stage_action_verdict(
        'deploy-health',
        {'clusters': ['gcp']},
        'the deploy looks fine, no complaints',  # narration only
    )
    assert result.verdict == 'FAIL'
    ctx = result.ba_failure_context
    assert ctx is not None
    assert ctx['mcp_returned'] is None  # no explicit MCP response


def test_composed_release_health_check_still_works() -> None:
    """The decomposition is ADDITIVE — the composed release-health-check
    verdict function still exists and produces its historical result. This
    is a REGRESSION-GUARD for the composed action's unchanged contract."""
    transcript = '\n'.join(
        [
            'STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=release v1 Succeeded',
            'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 merged',
            'STAGE_STATUS: stage=2 cluster=az verdict=PASS reason=promote PR #102 merged',
            'STAGE_STATUS: stage=3 cluster=gcp verdict=PASS reason=jx-boot Job succeeded',
            'STAGE_STATUS: stage=3 cluster=az verdict=PASS reason=jx-boot Job succeeded',
            'STAGE_STATUS: stage=4 cluster=gcp verdict=PASS reason=healthy=true available_replicas=2',
            'STAGE_STATUS: stage=4 cluster=az verdict=PASS reason=healthy=true available_replicas=2',
        ]
    )
    result = infra_agent._health_check_verdict({'service': 'hello-go'}, transcript)
    assert result.verdict == 'PASS'


def test_stage_action_verdict_uses_aggregator_seam_for_isolation() -> None:
    """The verdict function accepts a fake ``aggregator`` so it can be unit-
    tested in isolation from the parser — same seam as _health_check_verdict."""
    from gate.agent.release_health import StageActionResult

    calls: list[tuple[str, tuple[str, ...] | None]] = []

    def _fake(transcript: str, *, required_clusters: tuple[str, ...] = ('gcp', 'az')) -> StageActionResult:
        calls.append((transcript, required_clusters))
        return StageActionResult(verdict='PASS', reason=None, stage=4, stages=(), failing_stage=None)

    result = infra_agent._stage_action_verdict(
        'deploy-health',
        {'clusters': ['gcp', 'az']},
        'x',
        aggregator=_fake,
    )
    assert result.verdict == 'PASS'
    assert calls == [('x', ('gcp', 'az'))]


def test_authoring_capabilities_lists_all_five_actions() -> None:
    """The BA-facing authoring capabilities YAML MUST advertise the five
    individual actions as `available` so a BA can author a decomposed
    release-shepherd Plan. Guard against silent drift between the
    infra_agent code + the authoring surface the BA reads."""
    from pathlib import Path

    import yaml

    caps_path = Path(__file__).resolve().parent.parent / 'gate' / 'agent' / 'authoring_capabilities.yaml'
    data = yaml.safe_load(caps_path.read_text(encoding='utf-8'))
    actions = data['agent_types']['leartech-agent-infra']['actions']
    for name in ('release-status', 'promote-status', 'verify-gate', 'boot-status', 'deploy-health'):
        assert name in actions, f'{name} missing from authoring_capabilities.yaml'
        assert actions[name]['status'] == 'available'
    # And the composed action stays around for single-step use.
    assert actions['release-health-check']['status'] == 'available'


def test_release_shepherd_template_exists_and_chains_the_five_actions() -> None:
    """The examples/templates/release-shepherd.yaml template MUST exist and
    chain the five individual actions in dependsOn order. Guard against
    silent drift + missing template files."""
    from pathlib import Path

    import yaml

    template = Path(__file__).resolve().parent.parent / 'examples' / 'templates' / 'release-shepherd.yaml'
    assert template.exists(), 'examples/templates/release-shepherd.yaml is required'
    data = yaml.safe_load(template.read_text(encoding='utf-8'))
    step_actions = [step['inputs']['action'] for step in data['spec']['steps']]
    assert step_actions == [
        'release-status',
        'promote-status',
        'verify-gate',
        'boot-status',
        'deploy-health',
    ]
    # dependsOn chains each step to the previous — the whole point of
    # decomposing to a chain: stop the moment an upstream stage fails.
    steps = data['spec']['steps']
    for i in range(1, len(steps)):
        depends_on = steps[i].get('dependsOn') or []
        assert steps[i - 1]['name'] in depends_on, (
            f'step[{i}].dependsOn missing step[{i - 1}] — the chain would not stop-on-failure'
        )
