"""BA agent — brief-to-Plan authoring entrypoint.

Mirrors :mod:`gate.agent.infra_agent` in shape (an entrypoint on the agent image
that reads its inputs from ``$LEARTECH_INITIATIVE_YAML``) but with a very
different responsibility:

  * INPUT: a BRIEF — the Initiative contract EXTENDED with ``successCriteria``,
    ``context``, and ``resolves: [PlanRef]``.
  * OUTPUT: one or more Plan CRDs authored via the control-plane / agent-api
    MCPs. **The BA agent does NOT open a PR.** Its output is a *plan*, not a
    code change.

The BA has ZERO infra-specific knowledge. It consumes the brief only, correlates
it against live platform state (``platform_state`` MCP — list_plans /
list_runs / get_plan_state / deploy_health), does light web research through
the ai-gateway (``leartech-ai-gateway-web``), and then authors remediation /
target Plans via ``create_plan`` (with ``amend_plan`` available for the case
where mutating an in-flight plan is safer than a fresh create).

## Draft-by-default

Every authored plan is DRAFT-BY-DEFAULT — the BA sets ``hold: true`` on it AND
stamps a ``leartech.io/draft: "true"`` annotation. That way heavy downstream
work (Job spawn, PR opens, etc.) never triggers automatically off a BA-authored
plan; a human reviews the plan, clears the hold, and only then does execution
begin. This is the whole reason the BA can operate autonomously — its output
never runs unless someone approves.

## Final-step invariant

Every authored plan's FINAL step MUST verify the brief's ``successCriteria``.
Concretely: BA appends a verification step — the deterministic ``deploy-health``
check (version-aware; a ``kind: check`` step on ``leartech-agent-infra``)
against the deploy the brief targets, but the exact tool depends on what the
successCriteria expresses. Without that step, the plan can "succeed" without
actually satisfying the brief — the design memo calls this out as the whole
point of the successCriteria contract.

## Multi-plan authoring

The BA may author or amend MULTIPLE plans in a single session (cluster-wide
multi-resolve). Example: a brief with ``resolves: [PlanRef(cluster=gcp),
PlanRef(cluster=az)]`` typically produces two draft plans, one per cluster.

## Reasoning + web research

Reasoning routes through the Claude Agent SDK (which picks up
``ANTHROPIC_BASE_URL`` → ai-gateway automatically). Web research routes
through the ``leartech-ai-gateway-web`` in-process MCP (which POSTs to
``<gateway>/v1/search`` and ``<gateway>/v1/fetch``).

Model is pinned to ``claude-opus-4-8`` — NOT "auto", which can silently
downgrade to a cheaper backend (GLM) at the gateway router. Downgrades lose the
ability to correlate live state against the brief in a single reasoning pass.

## Provider portability

This module deliberately avoids any ``anthropic`` import — the LLM seam lives
in :mod:`gate.llm` (one-shot completions) and :mod:`claude_agent_sdk` (the
Claude Agent SDK loop the entrypoint uses). Tools are all standard MCP (either
remote via ``leartech-mcp-servers`` or in-process ``create_sdk_mcp_server``),
so a future non-Anthropic runtime keeps the tool surface intact. See
``AI-GATEWAY-AND-PORTABILITY.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import click
import yaml
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gate import obslog
from gate.agent.calibrations import load_jx3_calibration
from gate.agent.lessons import render_for
from gate.agent.test_mode import parse_test_mode, run_test_mode
from gate.mcp_servers import build_ai_gateway_web_server, build_remote_mcp_servers

# BA agent runs on Opus 4.8 pinned — NOT "auto". The gateway's router can
# downgrade "auto" to GLM (cheaper, less capable), which loses the multi-source
# correlation reasoning the BA depends on. Env-configurable for cluster-side
# per-role overrides, but the in-code default MUST NOT drift back to "auto".
DEFAULT_MODEL = os.environ.get('LEARTECH_BA_AGENT_MODEL', 'claude-opus-4-8')

# The BA agent may make many tool calls per plan (list_plans, list_runs,
# get_plan_state, deploy_health, web_search, web_fetch, create_plan, ...).
# 200 turns keeps a comfortable ceiling without funding an infinite loop.
DEFAULT_MAX_TURNS = 200

# Read-only aspects (list / correlate) live on ``leartech-platform-state``;
# they never mutate state, so listing them explicitly makes it easy to audit
# what the BA can observe.
BA_PLATFORM_STATE_TOOLS = [
    'mcp__leartech-platform-state__list_plans',
    'mcp__leartech-platform-state__list_runs',
    'mcp__leartech-platform-state__get_plan_state',
    'mcp__leartech-platform-state__deploy_health',
]

# Authoring surfaces — separate MCPs so future finer-grained authz can gate
# create vs. amend independently.
BA_AUTHORING_TOOLS = [
    'mcp__leartech-control-plane__create_plan',
    'mcp__leartech-agent-api__amend_plan',
]

# Web research via ai-gateway /v1/search + /v1/fetch — in-process MCP wrapping
# an httpx client that reads ANTHROPIC_BASE_URL + AI_GATEWAY_API_KEY. Wired as
# tools so the LLM decides *when* to search rather than us pre-fetching.
BA_WEB_TOOLS = [
    'mcp__leartech-ai-gateway-web__web_search',
    'mcp__leartech-ai-gateway-web__web_fetch',
]

# PR context (read-only) — a brief may reference PRs the BA needs to inspect
# before authoring a remediation plan against them (metadata, diff, changed
# files). No open_pr — BA does NOT open PRs.
BA_PR_CONTEXT_TOOLS = [
    'mcp__leartech-pr-context__get_pr_metadata',
    'mcp__leartech-pr-context__get_pr_diff',
    'mcp__leartech-pr-context__list_changed_files',
]

BA_ALLOWED_TOOLS: list[str] = [
    # Built-ins — Read/Glob/Grep only. NO Write/Edit/Bash: the BA authors
    # PLANS (via MCP tool calls), not code. Restricting this keeps a stray
    # `git commit` from the agent's system prompt from ever producing a
    # filesystem side effect.
    'Read',
    'Glob',
    'Grep',
    *BA_PLATFORM_STATE_TOOLS,
    *BA_AUTHORING_TOOLS,
    *BA_WEB_TOOLS,
    *BA_PR_CONTEXT_TOOLS,
]

# Sentinel annotation the BA stamps on every plan it authors. Downstream (the
# controller / dashboard) can filter on this to distinguish BA drafts from
# operator-authored plans. Format matches the leartech.io/* annotation
# convention on the AgentRun CRD.
DRAFT_ANNOTATION_KEY = 'leartech.io/draft'
DRAFT_ANNOTATION_VALUE = 'true'


# --- Brief schema -------------------------------------------------------------


class PlanRef(BaseModel):
    """A reference to a live plan the brief expects the BA to remediate.

    Minimal shape — just enough to fetch the plan via
    ``get_plan_state(namespace, name)`` and correlate what's actually running
    against what the brief says needs fixing. Additional fields (``cluster``,
    ``since``) are accepted as free-form context so the BA can reason about
    "which cluster is misbehaving" without a schema migration each time.
    """

    model_config = ConfigDict(extra='allow')

    name: str = Field(min_length=1, description='The Plan CR name to inspect / remediate.')
    namespace: str = Field(min_length=1, description='The Plan CR namespace.')


class Brief(BaseModel):
    """The BA agent's input — Initiative-shaped, plus successCriteria / context / resolves.

    Kept intentionally *separate* from :class:`gate.initiatives.loader.Initiative`
    because that model is ``extra='forbid'`` and adding successCriteria / resolves
    to every legacy YAML consumer would be a much bigger change than this
    initiative asks for. The Brief is the BA's own contract; downstream tools
    that only need the initiative-shaped subset can serialise it back into the
    Initiative model.
    """

    model_config = ConfigDict(extra='allow')

    name: str = Field(min_length=1, description='Short kebab-case identifier for the brief.')
    goal: str = Field(min_length=1, description='What the BA must accomplish. Free text.')

    # The three EXTENDED fields — the whole reason the Brief type exists.
    success_criteria: list[str] = Field(
        default_factory=list,
        alias='successCriteria',
        description=(
            "Bulleted list of criteria the plan's FINAL step MUST verify. E.g. "
            "'deployment X on cluster Y is healthy'. The BA must append a "
            'verification step to every authored plan that checks these.'
        ),
    )
    context: str = Field(
        default='',
        description='Free-form context the BA can lean on: symptoms, timeline, related PRs.',
    )
    resolves: list[PlanRef] = Field(
        default_factory=list,
        description=(
            'The live plan(s) the brief expects to be remediated. The BA copies '
            "this into each authored plan's `remediates: [...]` so downstream "
            'can trace back from remediation → target.'
        ),
    )

    @field_validator('success_criteria', mode='before')
    @classmethod
    def _accept_string_success_criteria(cls, value: object) -> object:
        """Accept a single-string ``successCriteria`` as a one-element list.

        Briefs authored by humans sometimes write ``successCriteria: "X"``
        instead of ``["X"]``; be permissive at the boundary and normalise.
        """
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @model_validator(mode='after')
    def _require_something_to_verify(self) -> Brief:
        """A brief with no successCriteria has no verification step to append.

        We treat that as an authoring bug — the whole point of the BA is that
        every plan it produces terminates in a successCriteria-verification
        step. An empty list would silently drop that invariant.
        """
        if not self.success_criteria:
            raise ValueError(
                'Brief must declare at least one successCriteria — the BA '
                "appends a verification step to every plan, and there's "
                'nothing to verify without at least one criterion.'
            )
        return self


def load_brief(raw: str) -> Brief:
    """Parse a brief from a YAML or JSON string.

    The entrypoint reads ``$LEARTECH_INITIATIVE_YAML``, which is either raw JSON
    (controller-inlined step ``inputs``) or YAML (a human running the entrypoint
    with ``--brief`` from a file). ``yaml.safe_load`` handles both — JSON is a
    strict subset of YAML.
    """
    text = raw.strip()
    if not text:
        raise ValueError('empty brief — set $LEARTECH_INITIATIVE_YAML or pass --brief')
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f'brief must be a mapping at the top level, got {type(data).__name__}')
    return Brief.model_validate(data)


# --- System prompt ------------------------------------------------------------


BA_SYSTEM_PROMPT = f"""\
You are the leartech BA (Business Analyst) AGENT. You take a BRIEF and produce
one or more DRAFT Plan CRDs — you do NOT open a PR and you do NOT write code.

You have ZERO infra-specific knowledge beyond the AUTHORABLE CAPABILITIES
catalog (above) — that catalog is the ONLY source of what a plan step can do.
You consume the brief for WHAT to remediate. Do not guess at cluster names,
deploy topologies, or repo layouts — the brief tells you what to target, and
the MCPs let you look up live state.

GROUND RULES

1. Every authored plan is DRAFT-BY-DEFAULT:
   - Set `hold: true` on the plan spec (so Tide-style auto-promotion is
     blocked).
   - Stamp the annotation `{DRAFT_ANNOTATION_KEY}={DRAFT_ANNOTATION_VALUE}`
     on the plan's metadata (so the dashboard / controller can filter BA
     drafts distinctly from operator-authored plans).
   You NEVER post `/hold cancel` and you NEVER unset the draft annotation —
   a human clears both after review.

2. Every authored plan's FINAL step MUST verify the brief's
   `successCriteria`. Typical shape: an infra-agent `deploy-health` check
   step (kind: check, action: deploy-health) that verifies the deployed
   service is the NEW version AND healthy per the criterion — deterministic
   and version-aware. Without this verification step, the plan can
   "succeed" without actually satisfying the brief — do not omit it.

3. Copy the brief's `resolves: [PlanRef]` into each authored plan's
   `remediates: [...]` field so downstream can trace remediation → target.

4. You may author or amend MULTIPLE plans in one session. If the brief's
   `resolves` spans clusters, typically one draft plan per cluster.

5. Prefer `amend_plan` when a live plan is close enough to the brief that
   a diff is safer than a fresh create; prefer `create_plan` otherwise.
   In both cases the draft-annotation + hold rules apply.

WORKFLOW

Step 1 — READ THE BRIEF. It carries `goal`, `successCriteria`, `context`,
`resolves`. Extract what you're being asked to remediate.

Step 2 — CORRELATE LIVE STATE. For every `PlanRef` in `resolves`, call
`get_plan_state(name, namespace)` and, when relevant, `deploy_health` /
`list_runs`. Do NOT re-author a plan that is already healthy or already in
flight with the right shape — that's cluster-wide multi-resolve, not
duplication.

Step 3 — RESEARCH (optional). If the brief references upstream fixes, error
messages, or third-party behaviour, use `web_search` and `web_fetch` to
verify. Skip this step when the brief is self-contained.

Step 4 — AUTHOR. Every step MUST come from AUTHORABLE CAPABILITIES:
   - Pick `agentType` per that catalog's `routing` — an in-repo change (code,
     chart/values, tests) is a DEV agent PR (leartech-agent-<lang>) with
     {{name, goal, repo, branch}}; a cluster-side/privileged op is
     leartech-agent-infra with `inputs.action` set to one of its `available`
     actions (+ that action's params). NEVER author an infra step without a
     valid `action`, and NEVER invent an agentType/action not in the catalog.
   - If the fix needs a power that is NOT an `available` action (e.g. provisioning
     a backend secret today), do NOT author a phantom step. Do BOTH: (1) author
     the executable fix that unblocks it NOW if one exists — usually a DEV PR
     that removes the need (e.g. set database.enabled=false) — AND (2) report the
     matching `capability_gaps` id (needs-infra:<action> / needs-human) in your
     summary so the infra surface grows to cover it properly. The gap is a
     REPORTED line, never a plan step, so it can't compete with the executable
     fix. If no executable fix exists at all, author just the verify step and
     report the gap alone.
   Then call `create_plan` (or `amend_plan`) with:
   - The executable remediation steps.
   - A final step that VERIFIES the brief's `successCriteria` — a
     `kind: check` step on leartech-agent-infra with `action: deploy-health`
     (service + clusters inputs; deterministic + version-aware).
   - `hold: true` (the tool maps this to the CRD's spec.paused gate) and the
     `{DRAFT_ANNOTATION_KEY}={DRAFT_ANNOTATION_VALUE}` annotation.
   - `remediates: [...]` populated from the brief's `resolves`.

Step 5 — REPORT. End your final message with a short summary — one line
per plan authored (or amended) with its name, namespace, and the
verification step at the end. If you decided NOT to author a plan (e.g.
because the state is already healthy), state that explicitly with the
evidence.

If ANY step fails (MCP unreachable, LLM tool_use error, brief invalid),
STOP and report the failure. Do NOT retry indefinitely. Do NOT fall back
to any non-MCP path — the MCPs are the only authoring surface.
"""


# --- Runtime wiring -----------------------------------------------------------


# The authorable capability surface (AgentTypes + infra actions + gaps). Kept as
# a data file (not hardcoded here) so infra can grow the surface without a
# ba_agent code change — add an action there, the BA can author it next deploy.
CAPABILITIES_PATH = Path(__file__).parent / 'authoring_capabilities.yaml'


def _render_authoring_capabilities() -> str:
    """Render authoring_capabilities.yaml into the system prompt so every step the
    BA authors is EXECUTABLE. Re-emitted as canonical (comment-free, alias-expanded)
    YAML so it always tracks the file. Degrades to empty on read/parse error — the
    BA still runs, just without the capability guard (logged)."""
    try:
        data = yaml.safe_load(CAPABILITIES_PATH.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:  # pragma: no cover - defensive
        obslog.error('capabilities_load_failed', f'could not load {CAPABILITIES_PATH.name}: {exc}', logger='ba')
        return ''
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    return (
        'AUTHORABLE CAPABILITIES\n\n'
        'Author plan steps ONLY within this surface. Choose agent_type per `routing`; '
        "every step's inputs MUST match the chosen type's (or infra action's) contract. "
        'If a remediation needs a power that is not an `available` action, do NOT author '
        'a phantom step — author only the executable steps and report the matching '
        '`capability_gaps` entry (needs-infra / needs-human). Infra is the restricted '
        'capability-holder; its `actions` list grows over time.\n\n' + body
    )


def _build_system_prompt() -> str:
    """JX3 calibration + any encoded ba_agent lessons + the authorable-capability
    catalog + the BA system prompt."""
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('ba_agent')
    if lessons:
        blocks.append(lessons)
    capabilities = _render_authoring_capabilities()
    if capabilities:
        blocks.append(capabilities)
    blocks.append(BA_SYSTEM_PROMPT)
    return '\n\n---\n\n'.join(blocks)


def _build_options(model: str, max_turns: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        mcp_servers={
            # In-process — the ai-gateway web layer (search + fetch).
            'leartech-ai-gateway-web': build_ai_gateway_web_server(),
            # Remote — platform_state, control_plane, agent_api, pr_context.
            # `build_remote_mcp_servers` returns `{}` when the auth env is
            # unset (laptop / test / not-yet-wired); the agent then only has
            # the in-process MCP + built-in tools, which is the graceful
            # degradation path.
            **build_remote_mcp_servers(),
        },
        allowed_tools=BA_ALLOWED_TOOLS,
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
    )


def _task_prompt(brief: Brief) -> str:
    """Serialise the brief into the user turn — JSON so the LLM parses cleanly.

    We include the model_dump with `by_alias=True` so `successCriteria` reads
    exactly the way brief authors write it, not the pythonic snake_case.
    """
    payload = brief.model_dump(by_alias=True, mode='json')
    return (
        'Author draft remediation Plan(s) for this BRIEF:\n\n'
        f'{json.dumps(payload, indent=2)}\n\n'
        'Follow the WORKFLOW in your system prompt. Correlate live state '
        'via `platform_state` MCP tools before authoring. Every plan you '
        'author MUST be draft-by-default (hold:true + '
        f'{DRAFT_ANNOTATION_KEY}={DRAFT_ANNOTATION_VALUE} annotation) and '
        "MUST end with a verification step for the brief's successCriteria."
    )


async def run_ba_task(
    brief: Brief,
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> int:
    """Drive the BA agent through one brief. Returns the process exit code.

    The success shape is deliberately LOOSER than infra-agent's — the BA has
    no single machine-readable verdict, it authors N plans and reports them.
    Exit code tracks whether the SDK loop ran to completion without an error,
    not whether any specific plan was authored.
    """
    # ── TEST-MODE short-circuit ────────────────────────────────────────────
    # A plan step may set ``brief.testMode`` (as an extra dict, since Brief
    # uses ``extra='allow'``) to skip the LLM/SDK loop entirely. ONLY
    # honored when LEARTECH_AGENT_TEST_MODE_ALLOWED=true is set — otherwise
    # the directive is IGNORED. The BA agent NEVER opens a PR, so we pass
    # ``open_pr_args=None`` — a ``prOutcome`` other than 'none' just logs a
    # skip line (BA is not a PR-backed step).
    test_mode_inputs = brief.model_dump(by_alias=True, mode='json')
    test_mode_spec = parse_test_mode(test_mode_inputs)
    if test_mode_spec is not None:
        obslog.info(
            'run_start',
            f'ba agent brief={brief.name} (test-mode)',
            logger='ba',
            brief=brief.name,
            test_mode=True,
        )
        exit_code = await run_test_mode(test_mode_spec, open_pr_args=None)
        obslog.info(
            'run_end',
            f'ba agent brief={brief.name} done (test-mode)',
            logger='ba',
            brief=brief.name,
            exit_code=exit_code,
            test_mode=True,
        )
        return exit_code

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return 2

    obslog.info('run_start', f'ba agent brief={brief.name}', logger='ba', brief=brief.name, model=model)
    options = _build_options(model, max_turns)
    prompt = _task_prompt(brief)

    exit_code = 0
    try:
        # Drain the async iterator fully — returning from inside `async for`
        # leaves the SDK's generator half-shut and raises on cleanup
        # (mirrors gate/agent/infra_agent.py + gate/agent/main.py).
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                    elif isinstance(block, ThinkingBlock | ToolResultBlock):
                        # Thinking = internal reasoning; tool results are seen
                        # by the agent — surface its synthesis instead.
                        pass
            elif isinstance(message, ResultMessage):
                exit_code = 1 if message.is_error else 0
    except Exception as exc:
        obslog.error('run_end', f'ba agent crashed: {exc}', logger='ba', brief=brief.name, exit_code=1)
        raise

    obslog.info('run_end', f'ba agent brief={brief.name} done', logger='ba', brief=brief.name, exit_code=exit_code)
    return exit_code


# The controller inlines the Plan step's `inputs` JSON into this env var
# (jobspawn.go). An entrypoint-override AgentType gets NO CLI args, so inputs
# arrive here, not via flags. Same convention as infra_agent.
INPUTS_ENV = 'LEARTECH_INITIATIVE_YAML'


def _load_brief_from_cli(brief_opt: str | None) -> Brief:
    """Resolve the brief text: --brief (arg or @file) → $LEARTECH_INITIATIVE_YAML.

    ``--brief`` accepts either the raw brief body or ``@path/to/brief.yaml``
    (the ``@`` prefix is the conventional "read from file" shape — cheap to
    parse and doesn't require a separate flag).
    """
    if brief_opt is not None:
        text = brief_opt
        if text.startswith('@'):
            path = text[1:]
            try:
                text = open(path, encoding='utf-8').read()  # noqa: SIM115 — read-once, no ctx-mgr needed
            except OSError as exc:
                raise click.BadParameter(f'could not read brief file {path!r}: {exc}') from exc
    else:
        text = os.environ.get(INPUTS_ENV, '')
    if not text.strip():
        raise click.BadParameter(f'no brief: set ${INPUTS_ENV}, or pass --brief (or --brief @path/to/file.yaml)')
    try:
        return load_brief(text)
    except Exception as exc:
        raise click.BadParameter(f'brief did not validate: {exc}') from exc


def _dry_run_summary(brief: Brief) -> str:
    """Human-readable one-brief summary for ``--dry-run`` mode.

    The dry-run path is the fast, LLM-free sanity check operators can run
    while drafting a brief — it validates the schema (via ``load_brief``)
    and prints WHAT the BA will be asked to do, without spending any
    tokens or contacting the gateway. See ``docs/BA-TEST-HARNESS.md`` for
    the full workflow.
    """
    lines = [
        '# BA dry-run — brief validated, no plans authored.',
        f'name: {brief.name}',
        f'goal: {brief.goal.strip().splitlines()[0][:120]}',
        f'successCriteria: {len(brief.success_criteria)} criterion(s)',
    ]
    for c in brief.success_criteria:
        lines.append(f'  - {c}')
    if brief.resolves:
        lines.append(f'resolves: {len(brief.resolves)} PlanRef(s) — the BA is expected to author >=1 draft plan')
        for ref in brief.resolves:
            lines.append(f'  - {ref.name} in {ref.namespace}')
    else:
        lines.append('resolves: [] — nothing to remediate; the BA will author a target plan (empty remediates)')
    lines.append('')
    lines.append(
        'To exercise this brief with the real BA on-cluster, wrap it in an AgentRun '
        'of AgentType leartech-agent-ba (see docs/BA-TEST-HARNESS.md).'
    )
    return '\n'.join(lines)


@click.command()
@click.option('--brief', 'brief_opt', default=None, help=f'Brief body (YAML/JSON) or @file; defaults to ${INPUTS_ENV}.')
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option('--max-turns', default=DEFAULT_MAX_TURNS, type=int, show_default=True, help='Max agent turns.')
@click.option(
    '--dry-run',
    'dry_run',
    is_flag=True,
    default=False,
    help=(
        'Validate the brief and print a summary WITHOUT calling the LLM or the '
        'gateway. Zero-cost sanity check while drafting briefs. Exits 0 on '
        'validation success, 2 on invalid brief.'
    ),
)
def main(brief_opt: str | None, model: str, max_turns: int, dry_run: bool) -> None:
    """Run the BA agent for one brief (the entrypoint a BA AgentType spawns).

    Inputs default to ``$LEARTECH_INITIATIVE_YAML`` (the controller's contract);
    ``--brief`` overrides for local use. ``--dry-run`` skips the LLM call
    entirely and just validates + summarises the brief — useful while
    drafting a brief and to prove BA plumbing without firing repo-factory-
    scale work.
    """
    brief = _load_brief_from_cli(brief_opt)
    if dry_run:
        click.echo(_dry_run_summary(brief))
        sys.exit(0)
    sys.exit(asyncio.run(run_ba_task(brief, model=model, max_turns=max_turns)))


# Re-export for tests / type checkers.
__all__ = [
    'BA_ALLOWED_TOOLS',
    'BA_AUTHORING_TOOLS',
    'BA_PLATFORM_STATE_TOOLS',
    'BA_PR_CONTEXT_TOOLS',
    'BA_SYSTEM_PROMPT',
    'BA_WEB_TOOLS',
    'Brief',
    'DEFAULT_MAX_TURNS',
    'DEFAULT_MODEL',
    'DRAFT_ANNOTATION_KEY',
    'DRAFT_ANNOTATION_VALUE',
    'INPUTS_ENV',
    'PlanRef',
    'load_brief',
    'main',
    'run_ba_task',
]


if __name__ == '__main__':
    main()
