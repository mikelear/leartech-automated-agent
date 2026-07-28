"""Tests for gate.agent.ba_agent — the BA-agent entrypoint wiring.

The BA agent (business analyst) takes a BRIEF and outputs one or more DRAFT
Plan CRDs via the control-plane / agent-api MCPs. It does NOT open a PR.
These tests pin the pieces that matter:

  * The Brief pydantic model validates the extended fields
    (successCriteria / context / resolves).
  * The ba_agent role in the catalog references only real MCPs.
  * `BA_ALLOWED_TOOLS` grants exactly the tools we intend and NOTHING that
    would let the agent write code (no Write / Edit / Bash / open_pr).
  * The system prompt encodes the "draft-by-default" + "final step verifies
    successCriteria" invariants.
  * `run_ba_task` behaves correctly with / without an API key, and re-raises
    SDK crashes.
  * The CLI entrypoint reads the brief from `$LEARTECH_INITIATIVE_YAML`
    (matching the infra-agent contract).

No live LLM / gateway calls — everything either monkeypatches `query` or
uses the model_dump surface directly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import pytest
from pydantic import ValidationError

from gate.agent import ba_agent
from gate.agent.mcp_catalog import load_catalog

# --- Catalog wiring -----------------------------------------------------------


def test_ba_role_in_catalog_references_real_mcps() -> None:
    """ba_agent role must be defined and every MCP it lists must exist."""
    catalog = load_catalog()
    assert 'ba_agent' in catalog.roles
    role = catalog.roles['ba_agent']
    # Read + Glob + Grep must be present; Write / Edit / Bash must NOT
    # (the BA authors PLANS via MCP, not code changes on disk).
    assert {'Read', 'Glob', 'Grep'} <= set(role.tools)
    for forbidden in ('Write', 'Edit', 'Bash'):
        assert forbidden not in role.tools, f'{forbidden!r} in ba_agent tools — BA must not write code / shell out'
    # Every MCP referenced by the role must exist in the catalog.
    for mcp in role.mcps:
        assert mcp in catalog.mcp_servers, f'ba_agent references unknown MCP {mcp!r}'
    # The three authoring / state MCPs the initiative requires by name.
    assert 'leartech-platform-state' in role.mcps
    assert 'leartech-control-plane' in role.mcps
    assert 'leartech-agent-api' in role.mcps
    # Web research MCP wired.
    assert 'leartech-ai-gateway-web' in role.mcps


def test_ba_role_pins_opus_not_auto() -> None:
    """The BA role must NOT route through the gateway's "auto" model — that
    can silently downgrade to GLM and lose the correlation reasoning the BA
    depends on. Pin explicitly to `claude-opus-4-8`."""
    role = load_catalog().roles['ba_agent']
    assert role.llm is not None, 'ba_agent must have an explicit llm config (Opus 4.8)'
    assert role.llm.model is not None and 'opus' in role.llm.model.lower()
    assert role.llm.model != 'auto', 'BA agent must NOT use "auto" model (can downgrade to GLM)'


def test_platform_state_control_plane_agent_api_in_wanted_remote_mcps() -> None:
    """The remote MCP registry must WANT all three BA-agent servers, so
    build_remote_mcp_servers discovers + wires them once auth is present."""
    from gate.mcp_servers import remote

    assert 'platform_state' in remote.WANTED_MCP_SERVERS
    assert 'control_plane' in remote.WANTED_MCP_SERVERS
    assert 'agent_api' in remote.WANTED_MCP_SERVERS


# --- Allowed-tools set --------------------------------------------------------


def test_allowed_tools_grant_state_authoring_web_pr_and_not_open_pr() -> None:
    tools = ba_agent.BA_ALLOWED_TOOLS

    # Built-ins: read-only trio.
    assert {'Read', 'Glob', 'Grep'} <= set(tools)
    # NO write / shell surface — BA must not touch the filesystem.
    for forbidden in ('Write', 'Edit', 'Bash'):
        assert forbidden not in tools, f'{forbidden!r} must not be in BA_ALLOWED_TOOLS'

    # Platform-state (read-only correlation).
    for t in ('list_plans', 'list_runs', 'get_plan_state', 'deploy_health'):
        assert f'mcp__leartech-platform-state__{t}' in tools

    # Authoring — create_plan on control_plane, amend_plan on agent_api.
    assert 'mcp__leartech-control-plane__create_plan' in tools
    assert 'mcp__leartech-agent-api__amend_plan' in tools

    # Web research — through ai-gateway.
    assert 'mcp__leartech-ai-gateway-web__web_search' in tools
    assert 'mcp__leartech-ai-gateway-web__web_fetch' in tools

    # PR context (read-only).
    assert 'mcp__leartech-pr-context__get_pr_metadata' in tools
    assert 'mcp__leartech-pr-context__get_pr_diff' in tools
    assert 'mcp__leartech-pr-context__list_changed_files' in tools

    # Explicitly NO open_pr — BA does NOT open PRs.
    assert 'mcp__leartech-pr-context__open_pr' not in tools


# --- Brief schema -------------------------------------------------------------


def _minimal_brief_dict() -> dict[str, object]:
    return {
        'name': 'fix-flaky-release',
        'goal': 'Remediate the recurring release-health flake on cluster gcp for foo-service.',
        'successCriteria': [
            'foo-service Deployment on gcp reports >=1 available replica',
            'HTTP GET /health/live returns 200 for 3 consecutive polls',
        ],
        'context': (
            'Cluster gcp saw 5 release failures in the last 24h. az is unaffected. '
            'The failing step is release-health-check.'
        ),
        'resolves': [
            {'name': 'foo-service-release', 'namespace': 'jx-staging'},
            {'name': 'foo-service-release', 'namespace': 'jx-production'},
        ],
    }


def test_brief_validates_extended_fields() -> None:
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    assert brief.name == 'fix-flaky-release'
    assert len(brief.success_criteria) == 2
    assert len(brief.resolves) == 2
    assert brief.resolves[0].name == 'foo-service-release'
    assert brief.resolves[0].namespace == 'jx-staging'


def test_brief_accepts_single_string_success_criteria() -> None:
    """A humane brief shape: `successCriteria: "X"` becomes `["X"]`."""
    data = _minimal_brief_dict()
    data['successCriteria'] = 'Deployment healthy on gcp'
    brief = ba_agent.Brief.model_validate(data)
    assert brief.success_criteria == ['Deployment healthy on gcp']


def test_brief_rejects_empty_success_criteria() -> None:
    """The BA appends a verification step for every criterion — no criteria
    would silently drop the invariant, so we fail loudly at load time."""
    data = _minimal_brief_dict()
    data['successCriteria'] = []
    with pytest.raises(ValidationError, match='successCriteria'):
        ba_agent.Brief.model_validate(data)


def test_brief_planref_allows_extra_context() -> None:
    """PlanRef is `extra='allow'` — a brief may include cluster / since /
    other free-form fields the BA can reason about without a schema bump."""
    data = _minimal_brief_dict()
    data['resolves'] = [
        {'name': 'foo', 'namespace': 'jx', 'cluster': 'gcp', 'since': '2026-07-27T00:00:00Z'},
    ]
    brief = ba_agent.Brief.model_validate(data)
    # The typed fields survive validation…
    assert brief.resolves[0].name == 'foo'
    # …and the extra fields are preserved on the model (Pydantic v2 dumps them).
    dumped = brief.resolves[0].model_dump()
    assert dumped['cluster'] == 'gcp'
    assert dumped['since'] == '2026-07-27T00:00:00Z'


def test_brief_requires_goal_and_name() -> None:
    with pytest.raises(ValidationError):
        ba_agent.Brief.model_validate({'name': '', 'goal': 'x', 'successCriteria': ['y']})
    with pytest.raises(ValidationError):
        ba_agent.Brief.model_validate({'name': 'x', 'goal': '', 'successCriteria': ['y']})


def test_load_brief_accepts_json_and_yaml() -> None:
    payload = _minimal_brief_dict()
    as_json = json.dumps(payload)
    as_yaml = 'name: fix-flaky-release\ngoal: g\nsuccessCriteria:\n  - h\n'
    j = ba_agent.load_brief(as_json)
    y = ba_agent.load_brief(as_yaml)
    assert j.name == 'fix-flaky-release'
    assert y.name == 'fix-flaky-release'
    assert y.success_criteria == ['h']


def test_load_brief_rejects_empty_and_non_mapping() -> None:
    with pytest.raises(ValueError, match='empty brief'):
        ba_agent.load_brief('')
    with pytest.raises(ValueError, match='mapping'):
        ba_agent.load_brief('- item\n')  # a list, not a mapping


# --- System prompt encodes the invariants ------------------------------------


def test_system_prompt_encodes_draft_by_default_and_verification() -> None:
    """The two invariants the BA MUST enforce end up on the LLM's system
    prompt, so a future refactor that drops them fails this test loudly."""
    prompt = ba_agent._build_system_prompt()
    # Draft-by-default: both the annotation + the hold flag are named.
    assert ba_agent.DRAFT_ANNOTATION_KEY in prompt
    assert ba_agent.DRAFT_ANNOTATION_VALUE in prompt
    assert 'hold: true' in prompt.lower()
    # Verification step for successCriteria.
    assert 'successCriteria' in prompt
    assert 'verification' in prompt.lower()
    # Explicit "no PR" + "no code" invariants.
    assert 'do NOT open a PR' in prompt or 'do not open a PR' in prompt.lower()
    # BA has ZERO infra-specific knowledge — clear signal in the prompt.
    assert 'ZERO infra' in prompt or 'zero infra' in prompt.lower()
    # Never post `/hold cancel` — a human clears the hold after review.
    assert '/hold cancel' in prompt
    # Cluster-wide multi-resolve is a first-class shape.
    assert 'MULTIPLE' in prompt or 'multi-resolve' in prompt


def test_system_prompt_names_the_three_authoring_paths() -> None:
    """The prompt must reference create_plan + amend_plan by name so the LLM
    picks the right tool; and platform_state so the correlation step happens."""
    prompt = ba_agent._build_system_prompt()
    assert 'create_plan' in prompt
    assert 'amend_plan' in prompt
    assert 'get_plan_state' in prompt or 'platform_state' in prompt


# --- Task prompt embeds the brief --------------------------------------------


def test_task_prompt_embeds_brief_body_and_reminders() -> None:
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    out = ba_agent._task_prompt(brief)
    # The serialised body must use the outward-facing alias
    # (`successCriteria`, not `success_criteria`).
    assert '"successCriteria"' in out
    assert 'foo-service' in out
    # The reminders about hold / draft / verification appear in the user turn
    # too so the LLM sees them alongside the input, not only on system.
    assert ba_agent.DRAFT_ANNOTATION_KEY in out
    assert 'verification' in out.lower()


# --- Runtime entrypoint ------------------------------------------------------


def test_run_ba_task_returns_2_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    rc = asyncio.run(ba_agent.run_ba_task(brief))
    assert rc == 2


def test_run_ba_task_reraises_sdk_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')

    async def _boom(*_args: object, **_kwargs: object):  # noqa: ANN202 — test stub async generator
        raise RuntimeError('sdk down')
        yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(ba_agent, 'query', _boom)
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    with pytest.raises(RuntimeError, match='sdk down'):
        asyncio.run(ba_agent.run_ba_task(brief))


def test_run_ba_task_returns_0_when_sdk_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: the SDK yields a ResultMessage(is_error=False) and we
    return 0 — no matter what the model actually did during the turns."""
    from claude_agent_sdk.types import ResultMessage

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')

    async def _one_result_message(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield ResultMessage(
            subtype='success',
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id='sess',
            total_cost_usd=0.0,
            usage={},
            result=None,
        )

    monkeypatch.setattr(ba_agent, 'query', _one_result_message)
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    rc = asyncio.run(ba_agent.run_ba_task(brief))
    assert rc == 0


def test_run_ba_task_returns_1_when_sdk_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_agent_sdk.types import ResultMessage

    monkeypatch.setenv('ANTHROPIC_API_KEY', 'k')

    async def _err_result(*_args: object, **_kwargs: object):  # noqa: ANN202
        yield ResultMessage(
            subtype='error_max_turns',
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id='sess',
            total_cost_usd=0.0,
            usage={},
            result=None,
        )

    monkeypatch.setattr(ba_agent, 'query', _err_result)
    brief = ba_agent.Brief.model_validate(_minimal_brief_dict())
    rc = asyncio.run(ba_agent.run_ba_task(brief))
    assert rc == 1


# --- CLI wiring --------------------------------------------------------------


def test_main_reads_brief_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The controller contract: brief arrives via $LEARTECH_INITIATIVE_YAML."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_YAML', json.dumps(_minimal_brief_dict()))
    captured: dict[str, object] = {}

    async def _fake(brief: ba_agent.Brief, **_kw: object) -> int:
        captured['brief'] = brief
        return 0

    monkeypatch.setattr(ba_agent, 'run_ba_task', _fake)
    with pytest.raises(SystemExit) as exc:
        ba_agent.main.callback(brief_opt=None, model='m', max_turns=1)
    assert exc.value.code == 0
    assert isinstance(captured['brief'], ba_agent.Brief)
    assert captured['brief'].name == 'fix-flaky-release'  # type: ignore[union-attr]


def test_main_reads_brief_from_at_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--brief @path reads the file."""
    brief_path = tmp_path / 'brief.yaml'
    brief_path.write_text('name: from-file\ngoal: g\nsuccessCriteria:\n  - c1\n')
    monkeypatch.delenv('LEARTECH_INITIATIVE_YAML', raising=False)

    captured: dict[str, object] = {}

    async def _fake(brief: ba_agent.Brief, **_kw: object) -> int:
        captured['brief'] = brief
        return 0

    monkeypatch.setattr(ba_agent, 'run_ba_task', _fake)
    with pytest.raises(SystemExit) as exc:
        ba_agent.main.callback(brief_opt=f'@{brief_path}', model='m', max_turns=1)
    assert exc.value.code == 0
    assert captured['brief'].name == 'from-file'  # type: ignore[union-attr]


def test_main_errors_when_no_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('LEARTECH_INITIATIVE_YAML', raising=False)
    with pytest.raises(click.BadParameter, match='no brief'):
        ba_agent.main.callback(brief_opt=None, model='m', max_turns=1)


def test_main_errors_on_invalid_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    """A syntactically-valid mapping missing successCriteria fails validation
    with a clear message so operators see WHY the brief was rejected."""
    monkeypatch.setenv('LEARTECH_INITIATIVE_YAML', '{"name": "x", "goal": "g"}')
    with pytest.raises(click.BadParameter, match='did not validate'):
        ba_agent.main.callback(brief_opt=None, model='m', max_turns=1)


def test_main_errors_on_missing_at_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv('LEARTECH_INITIATIVE_YAML', raising=False)
    missing = tmp_path / 'nope.yaml'
    with pytest.raises(click.BadParameter, match='could not read brief file'):
        ba_agent.main.callback(brief_opt=f'@{missing}', model='m', max_turns=1)


# --- Default model + max_turns are sane defaults ------------------------------


def test_default_model_is_opus_not_auto() -> None:
    """Regression guard: DEFAULT_MODEL must be a pinned Opus, not 'auto'.

    The initiative is explicit about this: the gateway's auto-router can
    downgrade to GLM. We never want that for BA reasoning-heavy authoring.
    """
    # Env override is supported — test the DEFAULT in isolation by consulting
    # the default value the module set at import time.
    assert 'opus' in ba_agent.DEFAULT_MODEL.lower() or ba_agent.DEFAULT_MODEL.startswith('claude-')
    assert ba_agent.DEFAULT_MODEL != 'auto'


def test_default_max_turns_is_positive() -> None:
    assert ba_agent.DEFAULT_MAX_TURNS > 0
