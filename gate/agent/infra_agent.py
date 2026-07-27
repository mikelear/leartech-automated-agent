"""Infra agent — cluster-side write-mode loop (repo-factory + release verification).

Mirrors ``gate/agent/main.py``'s query-loop shape but WRITE-mode. It owns the repo/cluster
wiring the dev agent doesn't:

  - ``create-repo``            : create the GitHub repo (README so ``main`` exists to PR against)
  - ``register-source-config``: register the repo in a cluster's source-config (one PR per cluster)
  - ``deploy-config``         : land the deploy/helmfile config for a cluster
  - ``scaffold-pr``           : deterministically scaffold from a language template and open the PR
                                whose preview exercises the Tekton steps
  - ``release-health-check``  : after a dev PR merges, verify the release is HEALTHY (the
                                "PR merged != release healthy" gap)

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
import re
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
# release-health-check action composes these to shepherd a release through jx-promote.
JX_RELEASE_TOOLS = [
    'mcp__leartech-jx-release__release_status',
    'mcp__leartech-jx-release__promote_status',
    'mcp__leartech-jx-release__retest_promote',
]

# Write-mode built-ins + the shared MCP surface + step-aware Tekton tools + the repo-factory
# and jx-release MCPs. Deterministic repo ops go through repo-factory (server-side); the
# release check goes through jx-release; Bash is for the optional /health tail (kubectl/curl).
INFRA_ALLOWED_TOOLS = [
    *WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS, *INITIATIVE_TEKTON_TOOLS, *REPO_FACTORY_TOOLS, *JX_RELEASE_TOOLS,
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
- The JX3 release check is DETERMINISTIC too — go through the jx-release MCP, never hand-scrape
  Tekton or GitHub:
    * mcp__leartech-jx-release__release_status — did the release fire on the repo?
    * mcp__leartech-jx-release__promote_status — promote PRs across both clusters + verify/gate
      state (all_green / gate_failed / merged / all_merged).
    * mcp__leartech-jx-release__retest_promote — chatops /retest to clear ONE flake.
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
- release-health-check: shepherd the service THROUGH the JX3 release pipeline to a landed,
  healthy release — the automation of the manual release watch. You are triggered when the dev
  PR OPENS (AwaitingReview), so nothing has released yet; you WAIT and drive it, using the
  jx-release MCP (do NOT hand-scrape Tekton). Bounded by `budgetMinutes` from your inputs
  (default 60 if unset) — a real cold-repo multi-cluster release+promote+deploy can take
  40-50 min, so do NOT give up early. Poll ~60s between checks (`sleep 60`) — never one giant
  sleep. Stages (stop + FAIL closed only once the budget elapses):
    1. RELEASE FIRED — poll mcp__leartech-jx-release__release_status(repo=mikelear/<service>)
       until released=true (the dev PR merged and the release Tekton produced a release).
    2. PROMOTE PRs — poll mcp__leartech-jx-release__promote_status(service) (both clusters):
       * found=false on a cluster → keep polling (jx-promote hasn't opened it yet).
       * a cluster check is non-green but NOT gate_failed (a flake) → call
         mcp__leartech-jx-release__retest_promote(cluster, pr_number) ONCE for that PR, then
         keep polling. Do NOT retest-loop.
       * any_gate_failed=true → STOP. This is a real qa-gate failure that may need other plans:
         FAIL with reason "needs-cross-plan-Infra-agent: <cluster> qa-gate failed on promote PR
         #<n>". Do NOT try to fix it yourself.
    3. MERGED — keep polling until all_merged=true (Tide auto-merges the promote PRs on green).
    4. HEALTH (optional tail) — once merged, best-effort confirm the Deployment `service` in
       `namespace` is rolling out; if you can quickly curl https://<host><healthPath> for 200
       via kubectl-discovered Ingress, note it. Do not fail solely on this tail if all_merged.
  PASS only when all_merged=true (stage 3). Params: service, namespace, healthPath.
  You MUST end your final message with EXACTLY ONE verdict line, on its own line:
      RELEASE_HEALTH: PASS
  or
      RELEASE_HEALTH: FAIL: <one-line reason>
  Output PASS ONLY if you OBSERVED all of: the Deployment exists with >=1 available replica
  AND an HTTP 200 from the health endpoint. If the Deployment/Ingress is missing, the rollout
  is incomplete, the curl is non-200, or you could NOT confirm for ANY reason, output FAIL.
  A merged PR, a queued release, or "not deployed yet" is a FAIL, never a PASS — the step's
  success is decided by this verdict, not by whether you finished exploring.

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


# Machine-readable verdict the release-health-check action must emit; the LAST match wins
# (the agent may narrate before its final verdict line). Absent => treated as FAIL.
_HEALTH_VERDICT_RE = re.compile(r'^\s*RELEASE_HEALTH:\s*(PASS|FAIL)\b', re.MULTILINE)


def _last_health_verdict(text: str) -> str | None:
    """Return the last RELEASE_HEALTH verdict (PASS/FAIL) in the agent transcript, or None."""
    matches = _HEALTH_VERDICT_RE.findall(text)
    return matches[-1].upper() if matches else None


def _resolve_exit_code(action: str, sdk_exit_code: int, health_verdict: str | None) -> int:
    """Fold the outcome-verdict into the exit code for judgment actions.

    For release-health-check, ONLY an explicit PASS keeps success; FAIL or a MISSING verdict
    forces exit 1 (a merged PR / undeployed release must never read as healthy). Other actions
    keep the SDK-derived code (is_error).
    """
    if action == 'release-health-check' and health_verdict != 'PASS':
        return 1
    return sdk_exit_code


async def run_infra_task(
    action: str,
    inputs: dict[str, object],
    *,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
) -> int:
    """Drive the infra agent through one action. Returns the process exit code."""
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
    transcript: list[str] = []
    try:
        # Drain the iterator fully (return inside `async for` breaks the SDK's generator
        # shutdown — see gate/agent/main.py).
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
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

    # Judgment actions must drive the exit code from the OUTCOME, not just SDK errors.
    # release-health-check emits a machine-readable verdict; anything but an explicit PASS
    # (incl. a MISSING verdict) FAILS the step — a merged PR / undeployed release must never
    # read as healthy (closes the false-success where exit_code tracked only is_error).
    if action == 'release-health-check':
        verdict = _last_health_verdict('\n'.join(transcript))
        exit_code = _resolve_exit_code(action, exit_code, verdict)
        obslog.info(
            'health_verdict',
            f'release-health-check verdict={verdict or "MISSING"}',
            logger='infra', action=action, verdict=verdict or 'MISSING', exit_code=exit_code,
        )

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
