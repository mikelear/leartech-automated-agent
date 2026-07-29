"""Async PR-review loop using the Claude Agent SDK.

v1 is read-only — the agent gathers context via MCP tools and produces a written verdict.
The Stop-hook-gated iteration loop with write tools (Read/Write/Edit/Bash + initiative YAML
driver) lands in the worked-initiative slice.
"""

from __future__ import annotations

import asyncio
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

from gate.agent.calibrations import load_jx3_calibration
from gate.agent.lessons import render_for
from gate.agent.system_prompt import REVIEW_SYSTEM_PROMPT
from gate.agent.test_mode import parse_test_mode, run_test_mode
from gate.mcp_servers import (
    build_artifacts_server,
    build_criteria_server,
    build_remote_mcp_servers,
)

DEFAULT_MODEL = os.environ.get('LEARTECH_AGENT_MODEL', 'claude-opus-4-7')
DEFAULT_MAX_TURNS = 20

# DEFAULT_MODEL is env-var-configurable to enable cluster-side model overrides
# without code changes. The in-code default is Opus 4.7 — suitable for complex
# strategic initiatives (agent self-modification, architecture work, deep refactors).
#
# Rationale for Opus as default: PR #33 switched to Haiku 4.5 as a Tier-1 rate-limit
# tune but encountered reasoning-accuracy regressions (false negatives on test counts).
# For upcoming Path C initiatives (control-plane refactor, language routing, job-per-run
# redesign), the cost of Opus is justified by reasoning quality.
#
# Override via env var for cost-sensitive periods:
#   LEARTECH_AGENT_MODEL=claude-haiku-4-5  (cheap, ~10x reduction)
#   LEARTECH_AGENT_MODEL=claude-sonnet-4-6 (balanced cost/quality)
#
# Clusters set this via their GitOps configs/leartech-automated-agent.yaml (see
# this initiative and PR #33 for context). Per-run overrides via click --model flag
# also available. See project_job_per_run_roadmap.md Phase 4 for per-initiative
# routing design.

# MCP tool names follow the convention `mcp__<server-name>__<tool-name>`.
MCP_ALLOWED_TOOLS = [
    # PR-check status surface — served remotely by the Go
    # leartech-mcp-servers `jx3_flow` deployment at
    # ${LEARTECH_MCP_URL}/mcp/jx3_flow. The former in-process shim
    # (`gate.mcp_servers.pipeline_server` under the `leartech-pipeline`
    # MCP name) has been removed; the remote is wired via `REMOTE_MCPS`
    # in `gate.mcp_servers.remote`.
    'mcp__leartech-jx3-flow__list_pr_checks',
    'mcp__leartech-jx3-flow__wait_for_terminal',
    'mcp__leartech-test-artifacts__list_playwright_runs',
    'mcp__leartech-test-artifacts__head_artifact',
    'mcp__leartech-criteria__list_criteria',
    'mcp__leartech-criteria__run_criteria_set',
    # open_pr: the agent opens its PR by calling this MCP tool (structured
    # GitHub create → publishes AgentRun.status{targetPR,headBranch}) instead
    # of running `gh pr create` + scraping the number. See initiative_prompt.
    'mcp__leartech-pr-context__open_pr',
]


def _build_system_prompt() -> str:
    """Prepend the JX3 platform calibration + any encoded lessons for review_agent.

    Composition order (top → bottom of rendered prompt):
      1. JX3 full-flow calibration — static, shipped in the wheel.
      2. Encoded calibration lessons from the catalog (filtered to review_agent).
      3. The review system prompt itself.
    """
    blocks: list[str] = [load_jx3_calibration()]
    lessons = render_for('review_agent')
    if lessons:
        blocks.append(lessons)
    blocks.append(REVIEW_SYSTEM_PROMPT)
    return '\n\n---\n\n'.join(blocks)


def _build_options(model: str, max_turns: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        mcp_servers={
            'leartech-test-artifacts': build_artifacts_server(),
            'leartech-criteria': build_criteria_server(),
            # Authed remote MCPs — currently leartech-pr-context,
            # leartech-tekton, and leartech-jx3-flow (PR-check surface).
            # Empty dict when unconfigured; see gate/mcp_servers/remote.py.
            **build_remote_mcp_servers(),
        },
        allowed_tools=MCP_ALLOWED_TOOLS,
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
    )


def _read_test_mode_inputs_from_env() -> dict[str, object] | None:
    """Parse an optional test-mode inputs JSON from ``LEARTECH_INITIATIVE_YAML``.

    The review agent has no dedicated inputs surface (CLI args only), but the
    shared shape used by the infra + BA agents inlines the plan step's inputs
    JSON into ``$LEARTECH_INITIATIVE_YAML``. When present AND that JSON has
    a ``testMode`` key, we honor it here too so the review agent shares the
    same short-circuit as its siblings. Returns None when the env var is
    unset / not JSON / not a dict.
    """
    raw = os.environ.get('LEARTECH_INITIATIVE_YAML', '').strip()
    if not raw:
        return None
    try:
        # Deliberate: json.loads instead of yaml.safe_load — a JSON body is
        # what the controller inlines. The review agent doesn't take a YAML
        # initiative, so we only need the strict-JSON subset.
        import json

        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


async def review_pr(
    repo: str, pr_number: int, *, model: str = DEFAULT_MODEL, max_turns: int = DEFAULT_MAX_TURNS
) -> int:
    """Drive Claude through a PR review using the gate's MCP servers. Returns exit code."""
    # ── TEST-MODE short-circuit ────────────────────────────────────────────
    # The review agent has no PR-open path (it's read-only) so open_pr is
    # NEVER called from here even in test-mode. ONLY honored when
    # LEARTECH_AGENT_TEST_MODE_ALLOWED=true; a stray directive in the env is
    # ignored otherwise. Placed BEFORE the API-key check so a test-mode run
    # with no key still succeeds.
    test_mode_inputs = _read_test_mode_inputs_from_env()
    test_mode_spec = parse_test_mode(test_mode_inputs)
    if test_mode_spec is not None:
        return await run_test_mode(test_mode_spec, open_pr_args=None)

    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return 2

    options = _build_options(model, max_turns)
    user_prompt = (
        f'Review {repo}#{pr_number}. Use the MCP tools to gather context, '
        f'run the gate, and produce a concise review report following the structure in your system prompt.'
    )

    # Drain the iterator fully and return after — `return`ing from inside the `async for`
    # leaves the SDK's internal generator mid-shutdown and triggers
    # "RuntimeError: aclose(): asynchronous generator is already running" on cleanup.
    exit_code = 0
    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    click.echo(block.text)
                elif isinstance(block, ToolUseBlock):
                    click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                elif isinstance(block, ThinkingBlock):
                    pass  # Suppress thinking blocks — agent's internal reasoning, not user-facing.
                elif isinstance(block, ToolResultBlock):
                    pass  # Tool results are seen by the agent; we surface its synthesis instead.
        elif isinstance(message, ResultMessage):
            exit_code = 1 if message.is_error else 0

    return exit_code


@click.command()
@click.option('--repo', required=True, help='Repo name (mikelear/X or just X).')
@click.option('--pr', required=True, type=int, help='PR number.')
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option('--max-turns', default=DEFAULT_MAX_TURNS, type=int, show_default=True, help='Max agent turns.')
def main(repo: str, pr: int, model: str, max_turns: int) -> None:
    """Run the read-only PR-review agent against a live PR."""
    sys.exit(asyncio.run(review_pr(repo, pr, model=model, max_turns=max_turns)))


if __name__ == '__main__':
    main()
