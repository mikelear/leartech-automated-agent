"""Infra agent — cluster-side write-mode loop (repo-factory + release verification).

Mirrors ``gate/agent/main.py``'s query-loop shape but WRITE-mode. It owns the repo/cluster
wiring the dev agent doesn't:

  - ``create-repo``            : create the GitHub repo (README so ``main`` exists to PR against)
  - ``register-source-config``: register the repo in a cluster's source-config (one PR per cluster)
  - ``deploy-config``         : land the deploy/helmfile config for a cluster
  - ``scaffold-pr``           : deterministically scaffold from a language template and open the PR
                                whose preview exercises the Tekton steps
  - the ``kind: check`` release-verify actions (release-pipeline-status / promote-status /
                                deploy-health / bootjob-for-commit) : thin DETERMINISTIC Go-MCP-tool
                                relays (no LLM turn-loop) — see ``gate.agent.release_checks``. These
                                cover the "PR merged != release healthy" gap version-awarely.

Scaffolding is DETERMINISTIC via ``gate.tools.repo_factory`` (literal rename, never LLM
grep/replace); the agent orchestrates + handles per-cluster variation but does NOT hand-edit
template files. Its persona (MCPs/tools/model) is the ``infra_agent`` role in
``mcp_catalog.yaml``; it runs on its own gateway virtual key (see
memory project_per_agent_model_routing), so its model can be swapped cheaply without a
redeploy. See memory project_repo_factory_init for the Plan that drives these actions.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import click
from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from gate import obslog
from gate.agent.calibrations import load_jx3_calibration
from gate.agent.initiative import INITIATIVE_TEKTON_TOOLS, WRITE_MODE_TOOLS
from gate.agent.lessons import render_for
from gate.agent.main import DEFAULT_MODEL, MCP_ALLOWED_TOOLS
from gate.agent.release_checks import is_check_action, run_check_action
from gate.agent.test_mode import parse_test_mode, run_test_mode
from gate.mcp_servers import build_remote_mcp_servers

DEFAULT_MAX_TURNS = 200

# The repo-factory MCP tools (server-side, on the platform-mcps host). create/register/
# scaffold run with the owner PAT server-side — the agent just calls them.
REPO_FACTORY_TOOLS = [
    'mcp__leartech-repo-factory__create_repo',
    'mcp__leartech-repo-factory__register_source_config',
    'mcp__leartech-repo-factory__scaffold',
    'mcp__leartech-repo-factory__smoke_pr',
]

# jx_release MCP — the JX3 release-check primitives (GitHub-API-first, both clusters). The
# deterministic release-verify checks (gate.agent.release_checks) call these Go MCP tools
# directly via the bridge to shepherd a release through jx-promote.
JX_RELEASE_TOOLS = [
    'mcp__leartech-jx-release__release_status',
    'mcp__leartech-jx-release__promote_status',
    'mcp__leartech-jx-release__retest_promote',
]

# k8s MCP — in-cluster read surface (no kubectl needed on the agent side). The
# deterministic release-verify checks call these Go MCP tools directly:
#   * list_jobs_by_label / get_job_state — did the jx-boot Job for this release
#     run and succeed on each cluster? (bootjob-for-commit)
#   * deploy_health — is the Deployment healthy (>=1 available replica) + running
#     the NEW version on each cluster? (deploy-health — replaces the historical
#     unreachable HTTP /health probe)
K8S_TOOLS = [
    'mcp__leartech-k8s__deploy_health',
    'mcp__leartech-k8s__get_job_state',
    'mcp__leartech-k8s__list_jobs_by_label',
]

# Write-mode built-ins + the shared MCP surface + step-aware Tekton tools + the repo-factory,
# jx-release, and k8s MCPs. Deterministic repo ops go through repo-factory (server-side); the
# deterministic release-verify checks call jx-release + tekton + k8s Go MCP tools directly (no
# httpx probe, no kubectl on the agent side — the k8s MCP host runs in-cluster with a
# read-scoped ServiceAccount).
INFRA_ALLOWED_TOOLS = [
    *WRITE_MODE_TOOLS,
    *MCP_ALLOWED_TOOLS,
    *INITIATIVE_TEKTON_TOOLS,
    *REPO_FACTORY_TOOLS,
    *JX_RELEASE_TOOLS,
    *K8S_TOOLS,
]

INFRA_SYSTEM_PROMPT = """\
You are the leartech INFRA AGENT. You own repo/cluster wiring and release verification —
the cluster-side work the dev agent does not do. You are precise, deterministic, and you
prefer proven tools over improvisation.

GROUND RULES
- Repo creation, source-config registration, and scaffolding are DETERMINISTIC and run
  SERVER-SIDE via the repo-factory MCP. CALL the tools — never Bash/gh/git or hand-edit YAML:
    * mcp__leartech-repo-factory__create_repo — creates the repo under the OWNER account
      (rejects bot tokens) and invites the 6 machine bots as collaborators.
    * mcp__leartech-repo-factory__register_source_config — idempotent source-config PR on a
      cluster (skips if already registered). cluster is 'gcp' or 'az'.
    * mcp__leartech-repo-factory__scaffold — renders a template into the target repo (literal
      rename, no grep) via the Git Data API. to_main=true pushes it straight to main (bootstrap
      a new repo: triggers land on main + the release fires); else it opens a scaffold PR.
    * mcp__leartech-repo-factory__smoke_pr — opens a trivial gated PR to verify the bootstrapped
      repo's PR pipelines fire (main now has .lighthouse/ triggers).
  The high-privilege owner credential lives in the MCP host, NOT here. If a tool errors or a
  rename looks wrong, report it as a TOOL bug — do not patch by hand.
- The release-verify checks (release-pipeline-status / promote-status / deploy-health /
  bootjob-for-commit) are DETERMINISTIC and run WITHOUT the LLM — each is a thin relay that
  calls ONE Go MCP tool (jx-release / tekton / k8s) and decides PASS/FAIL on the tool's typed
  verdict (see gate/agent/release_checks.py). They never reach this prompt; you are never asked
  to hand-scrape Tekton/GitHub or attempt a kubectl or /health HTTP probe. deploy-health is
  version-aware and reads Deployment status in-cluster — the authoritative "the new release
  landed healthy" signal.
- The platform runs on TWO clusters (GCP gitops `jx-build-cluster-gsm`, Azure
  `jx-build-cluster-akv`). Registration is ONE PR PER CLUSTER — do the cluster in your inputs;
  a Plan runs one register step per cluster.
- Config lives in repos, platform logic in the pipeline-catalog — the template already
  references the catalog; never copy pipeline logic into a new repo.

ACTIONS (your inputs include `action` + its params):
- create-repo: call mcp__leartech-repo-factory__create_repo with name=<short repo name>. It
  creates under the owner + invites the bots server-side. Params: newRepo (pass its short name).
- register-source-config: call mcp__leartech-repo-factory__register_source_config with
  service + cluster AND run_id=$LEARTECH_RUN_ID, namespace=$AGENT_RUN_NAMESPACE. It edits the
  cluster's source-config, opens the PR, AUTO-APPROVES it (owner /approve so Tide merges), and
  records the PR onto THIS AgentRun so the register step is MERGE-GATED (AwaitingReview until
  merged, then Succeeds) — so a downstream scaffold waits until the repo is really registered
  (Lighthouse/webhook live). Idempotent (no PR if already registered → Succeeds immediately).
  Params: service, cluster ('gcp'|'az'), run_id, namespace.
- scaffold-pr: BOOTSTRAP a brand-new repo — call mcp__leartech-repo-factory__scaffold with
  template, target_repo, name, to_main=true. It renders the template and pushes it (incl.
  .lighthouse/ triggers) STRAIGHT TO main (no PR), which both lets later PRs gate AND fires the
  release off the main push. Do NOT pass run_id here (no PR to record); the step is repo:"" and
  Succeeds on push. Params: template, name (target_repo = mikelear/<name>), to_main=true.
- smoke-pr: after scaffold-pr, OPEN the trivial smoke PR (deterministic plumbing) — call
  mcp__leartech-repo-factory__smoke_pr with target_repo, marker=<name> (the SERVICE name from
  your inputs, so the branch is the deterministic `smoke-<name>`). Do NOT pass run_id/namespace:
  this step just OPENS the PR (fire-and-forget, repo:"") — a downstream Dev-agent step ADOPTS
  branch `smoke-<name>` (via idempotent open_pr) and OWNS it (watch gates, fix failures, merge).
  Infra opens + verifies the plumbing; the Dev agent drives the PR. Params: target_repo =
  mikelear/<name>, name.

Report concisely what you did, which PRs you opened (numbers), and the pass/fail outcome.
"""


def _build_system_prompt() -> str:
    """JX3 calibration + any encoded infra_agent lessons + the infra system prompt."""
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('infra_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(INFRA_SYSTEM_PROMPT)
    return '\n\n---\n\n'.join(blocks)


def _build_options(model: str, max_turns: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        mcp_servers={**build_remote_mcp_servers()},
        allowed_tools=INFRA_ALLOWED_TOOLS,
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
    )


def _task_prompt(action: str, inputs: dict[str, object]) -> str:
    return (
        f'Perform infra action `{action}` with these inputs:\n\n'
        f'{json.dumps(inputs, indent=2)}\n\n'
        f'Follow the procedure for this action in your system prompt. Use the repo-factory '
        f'tool for any scaffolding. Report the PRs you opened and the outcome.'
    )


async def run_infra_task(
    action: str,
    inputs: dict[str, object],
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> int:
    """Drive the infra agent through one action. Returns the process exit code."""
    # ── TEST-MODE short-circuit ────────────────────────────────────────────
    # A plan step may set ``inputs.testMode`` to skip the LLM/SDK loop
    # entirely. ONLY honored when LEARTECH_AGENT_TEST_MODE_ALLOWED=true is
    # set — otherwise the directive is IGNORED. Placed BEFORE the API-key
    # check because test-mode's whole point is to skip the LLM. The infra
    # agent's PR-backed actions (register-source-config, smoke-pr) don't
    # call open_pr directly — the repo-factory MCP handles that — so we
    # don't build a manual open_pr_args here; the MCP's own test-mode
    # coverage exercises those flows via a different plan step.
    test_mode_spec = parse_test_mode(inputs)
    if test_mode_spec is not None:
        obslog.info(
            'run_start',
            f'infra agent action={action} (test-mode)',
            logger='infra',
            action=action,
            test_mode=True,
        )
        exit_code = await run_test_mode(test_mode_spec, open_pr_args=None)
        obslog.info(
            'run_end',
            f'infra agent action={action} done (test-mode)',
            logger='infra',
            action=action,
            exit_code=exit_code,
            test_mode=True,
        )
        return exit_code

    # ── THIN DETERMINISTIC CHECK short-circuit ─────────────────────────────
    # The release-verify check actions (release-pipeline-status / promote-status /
    # deploy-health / bootjob-for-commit) are pure Go-MCP-tool relays: call ONE
    # tool, exit on its typed verdict — NO LLM turn-loop, NO STAGE_STATUS
    # transcription (see gate/agent/release_checks.py). Placed BEFORE the
    # ANTHROPIC_API_KEY gate because these never invoke the model; they only need
    # the MCP host + auth env. This is the runtime side of the verify-release-flow
    # PlanTemplate — each kind:check step maps to one action here.
    if is_check_action(action):
        return await run_check_action(action, inputs)

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return 2

    obslog.info('run_start', f'infra agent action={action}', logger='infra', action=action)
    options = _build_options(model, max_turns)
    prompt = _task_prompt(action, inputs)

    exit_code = 0
    try:
        # Drain the iterator fully (return inside `async for` breaks the SDK's generator
        # shutdown — see gate/agent/main.py).
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                    elif isinstance(block, ThinkingBlock | ToolResultBlock):
                        pass  # internal reasoning / tool results — surface the synthesis instead
            elif isinstance(message, ResultMessage):
                exit_code = 1 if message.is_error else 0
    except Exception as exc:
        obslog.error('run_end', f'infra agent crashed: {exc}', logger='infra', action=action, exit_code=1)
        raise

    # NOTE: the release-verify checks (release-pipeline-status / promote-status / deploy-health /
    # bootjob-for-commit) are DETERMINISTIC and short-circuit ABOVE via is_check_action — they
    # never reach this LLM loop. The remaining actions here are the repo-factory ops, whose exit
    # code tracks SDK success (their real side effect — PRs — is recorded server-side by the
    # repo-factory MCP). The legacy LLM-transcribed STAGE_STATUS release-health-check verdict
    # machinery has been removed (superseded by gate/agent/release_checks.py).
    obslog.info('run_end', f'infra agent action={action} done', logger='infra', action=action, exit_code=exit_code)
    return exit_code


# The controller inlines the Plan step's `inputs` JSON into this env var (jobspawn.go);
# an entrypoint-override AgentType gets NO CLI args, so inputs arrive here, not via flags.
INPUTS_ENV = 'LEARTECH_INITIATIVE_YAML'


@click.command()
@click.option('--action', default=None, help='Infra action; defaults to inputs["action"].')
@click.option('--inputs', 'inputs_opt', default=None, help=f'JSON inputs; defaults to ${INPUTS_ENV}.')
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option('--max-turns', default=DEFAULT_MAX_TURNS, type=int, show_default=True, help='Max agent turns.')
def main(action: str | None, inputs_opt: str | None, model: str, max_turns: int) -> None:
    """Run the infra agent for one action (the entrypoint an infra AgentType spawns).

    Inputs default to ``$LEARTECH_INITIATIVE_YAML`` (the controller's contract — the Plan
    step's inputs JSON, which carries ``action`` + params); ``--inputs``/``--action``
    override for local use.
    """
    raw = inputs_opt if inputs_opt is not None else os.environ.get(INPUTS_ENV, '')
    if not raw.strip():
        raise click.BadParameter(f'no inputs: set ${INPUTS_ENV} or --inputs')
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise click.BadParameter(f'inputs must be valid JSON: {exc}') from exc
    if not isinstance(parsed, dict):
        raise click.BadParameter('inputs must be a JSON object')
    act = action or parsed.get('action')
    if not isinstance(act, str) or not act:
        raise click.BadParameter('no action: set --action or inputs["action"]')
    params = {k: v for k, v in parsed.items() if k != 'action'}
    sys.exit(asyncio.run(run_infra_task(act, params, model=model, max_turns=max_turns)))


if __name__ == '__main__':
    main()
