"""Async initiative-driver loop using the Claude Agent SDK in write mode.

Reads a YAML initiative, drives Claude through the full make-change → commit → push →
open-PR → watch-gate → iterate-on-failure cycle. Uses `query()` (single fire-and-forget
session) with a high `max_turns` budget — the agent's internal calling of
`run_criteria_set` is itself the iteration loop. If you need cross-session resumption
or interactive interrupts, swap to `ClaudeSDKClient` (v1.5).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

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

from gate.agent.initiative_prompt import INITIATIVE_SYSTEM_PROMPT
from gate.agent.lessons import render_for
from gate.agent.main import DEFAULT_MODEL, MCP_ALLOWED_TOOLS
from gate.initiatives import load_initiative
from gate.mcp_servers import (
    build_artifacts_server,
    build_criteria_server,
    build_pipeline_server,
    build_pr_context_server,
)


@dataclass(frozen=True)
class RunSummary:
    """Outcome of a single initiative run — surfaced to API callers via app.state."""

    exit_code: int
    turns: int | None = None
    cost_usd: float | None = None
    pr_number: int | None = None


def _resolve_pr_number(qualified_repo: str, branch: str) -> int | None:
    """Best-effort: ask GitHub for the open PR on `branch`. Returns None on miss/error.

    Runs synchronously; called once at end-of-run so the few-hundred-ms cost is fine.
    """
    try:
        result = subprocess.run(
            [
                'gh',
                'pr',
                'list',
                '--repo',
                qualified_repo,
                '--head',
                branch,
                '--state',
                'open',
                '--json',
                'number',
                '--limit',
                '1',
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        rows = json.loads(result.stdout or '[]')
        if not rows:
            return None
        number = rows[0].get('number')
        return int(number) if number is not None else None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return None


def _build_crash_sticky_body(
    *,
    reason: str,
    turn_count: int,
    max_turns: int,
    cost: float | None,
    hint: str,
) -> str:
    """Render the crash-sticky markdown. The marker lets future tooling find it."""
    cost_str = f'${cost:.4f}' if cost is not None else 'unknown'
    return (
        '<!-- leartech-agent-run -->\n'
        f'## ⚠ Agent run did not complete\n\n'
        f'**Reason**: {reason}\n\n'
        f'**Turns**: {turn_count}/{max_turns}  •  **Cost so far**: {cost_str}\n\n'
        f'{hint}\n'
    )


def _post_crash_sticky(*, qualified_repo: str, pr_number: int | None, body: str) -> None:
    """Best-effort: post a crash sticky to the PR.

    Called only from the harness's exception branches when the agent never reached
    its own step-11 sticky. Tolerates every failure mode (no PR, no network, gh
    auth issue) — we're already in an error path, so any secondary failure here is
    logged to stderr and swallowed.
    """
    if pr_number is None:
        click.echo('  (crash sticky skipped: no open PR resolved for this branch)', err=True)
        return
    try:
        result = subprocess.run(
            [
                'gh',
                'pr',
                'comment',
                str(pr_number),
                '-R',
                qualified_repo,
                '--body',
                body,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if result.returncode != 0:
            click.echo(
                f'  (crash sticky post failed: gh exit {result.returncode}: {result.stderr.strip()})',
                err=True,
            )
        else:
            click.echo(f'  → crash sticky posted to PR #{pr_number}', err=True)
    except (subprocess.TimeoutExpired, OSError) as exc:
        click.echo(f'  (crash sticky post errored: {exc})', err=True)


# 60 → 150 → 1000 → 200. Re-baselined 2026-05-26 after the Phase 1 cascade hit
# the Anthropic org-level rate limit (20M prompt bytes/hour on Sonnet 4.6).
# Observed actual usage: agent-base + py + ng finished in 30, 30-something,
# and 30 turns respectively (~$0.50-$1 each). Even substantial initiatives
# (catalog-fire-fallback at 134 turns, OAuth challenge at 107) stayed well
# under 200. The 1000 cap was a "safety net against rabbit-holes" — at our
# new Haiku-default + tighter quotas, 200 is a more honest safety net that
# also caps any runaway burn quickly. Override per-run via `--max-turns N`
# for genuinely larger initiatives.
DEFAULT_INITIATIVE_MAX_TURNS = 200

# Standard write-mode toolkit. Bash gives `git`, `gh`, `npm`, etc.; the rest are file ops.
WRITE_MODE_TOOLS = ['Read', 'Write', 'Edit', 'Glob', 'Grep', 'Bash']


def _default_repo_root(repo_name: str) -> Path:
    """Resolve the consumer-repo checkout path. Honours these in order:

    1. `LEARTECH_REPO_ROOT` env var → treat as a parent directory; append `<repo-name>` to it.
       (Useful for cluster pods where checkouts live at `/workspace/<repo>`.)
    2. Fallback: `~/leartech/<repo-name>` — the laptop convention.
    """
    repo_short = repo_name.split('/')[-1]
    parent_override = os.environ.get('LEARTECH_REPO_ROOT')
    if parent_override:
        return Path(parent_override).expanduser() / repo_short
    return Path('~/leartech').expanduser() / repo_short


def _clone_repo(*, qualified_repo: str, cwd: Path) -> int:
    """Clone `qualified_repo` (e.g. `mikelear/leartech-automated-agent`) into `cwd`.

    Uses direct `git clone` over HTTPS with the GH_TOKEN as a basic-auth user
    (`x-access-token:<token>@github.com/...`). This is GitHub's documented
    git-over-HTTPS auth format and — critically — hits NO GitHub API; just the
    git wire protocol. That makes the clone immune to the 5000pts/h GraphQL
    rate-limit bucket that `gh repo clone` consumed.

    Returns 0 on success, non-zero on failure. The token is never logged: we
    redact it from any echoed stderr before surfacing.
    """
    gh_token = os.environ.get('GH_TOKEN')
    if not gh_token:
        click.echo(
            f'Repo checkout not found at {cwd} and GH_TOKEN is not set — cannot clone. '
            f'Either clone manually (`git clone https://github.com/{qualified_repo}.git {cwd}`) '
            f'or set GH_TOKEN.',
            err=True,
        )
        return 2
    click.echo(
        click.style(f'→ cloning {qualified_repo} → {cwd}', fg='cyan'),
        err=True,
    )
    cwd.parent.mkdir(parents=True, exist_ok=True)
    url = f'https://x-access-token:{gh_token}@github.com/{qualified_repo}.git'
    result = subprocess.run(
        ['git', 'clone', '--depth', '1', url, str(cwd)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        # Defensive: redact the token from any error output before echoing.
        redacted_stderr = result.stderr.replace(gh_token, '***REDACTED***')
        click.echo(
            f'Clone failed (exit {result.returncode}):\n{redacted_stderr}',
            err=True,
        )
        return 2
    return 0


async def run_initiative(
    initiative_path: Path,
    *,
    repo_root: Path | None = None,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_INITIATIVE_MAX_TURNS,
) -> RunSummary:
    """Drive a single initiative end-to-end. Returns a summary of the run."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return RunSummary(exit_code=2)

    initiative = load_initiative(initiative_path)

    # Multi-repo execution (coordinated changes across multiple PRs in a single
    # agent session) is a follow-up slice. Schema accepts the shape today; the
    # agent loop only handles len(repos) == 1 until that slice lands.
    if initiative.is_multi_repo:
        click.echo(
            click.style(
                f'Initiative `{initiative.name}` declares {len(initiative.repos)} repos but '
                'multi-repo execution is not yet implemented.\n'
                'For now, split into one initiative per repo (each can still cite the same '
                'parent brief via `description:`). Multi-repo execution lands as a follow-up '
                'slice — see `project_next_phase_alignment.md`.',
                fg='red',
            ),
            err=True,
        )
        return RunSummary(exit_code=2)

    primary = initiative.primary
    cwd = repo_root or _default_repo_root(primary.qualified_repo)
    if not cwd.exists():
        # Cluster mode: the consumer repo isn't pre-mounted, so clone it from GitHub
        # on demand. We use direct `git clone` over HTTPS (with GH_TOKEN injected
        # into the URL) rather than `gh repo clone`, because `gh` resolves the
        # clone URL via the GitHub GraphQL API — which shares a 5000pts/h bucket
        # with operator-side `gh` usage. Direct git protocol hits no API.
        # Laptop mode normally has the repo at ~/leartech/<repo>/ already, so this
        # branch only fires on a fresh dev machine or the deployed pod.
        clone_exit = _clone_repo(qualified_repo=primary.qualified_repo, cwd=cwd)
        if clone_exit != 0:
            return RunSummary(exit_code=clone_exit)

    calibrations = render_for('initiative_agent')
    system_prompt = f'{calibrations}\n\n---\n\n{INITIATIVE_SYSTEM_PROMPT}' if calibrations else INITIATIVE_SYSTEM_PROMPT

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={
            'leartech-pipeline': build_pipeline_server(),
            'leartech-pr-context': build_pr_context_server(),
            'leartech-test-artifacts': build_artifacts_server(),
            'leartech-criteria': build_criteria_server(),
        },
        allowed_tools=[*WRITE_MODE_TOOLS, *MCP_ALLOWED_TOOLS],
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
        cwd=str(cwd),
        add_dirs=[str(cwd)],
    )

    user_prompt = (
        f'Run this initiative end-to-end. Your working directory is `{cwd}`.\n\n'
        f'```yaml\n{initiative_path.read_text()}\n```\n\n'
        f'Begin by checking what state the branch is in (`git status`, `git branch --show-current`), '
        f'then proceed through the loop in your system prompt.'
    )

    click.echo(click.style(f'→ initiative: {initiative.name}', fg='green', bold=True), err=True)
    click.echo(click.style(f'  repo: {primary.qualified_repo}  branch: {primary.branch}', fg='green'), err=True)
    click.echo(click.style(f'  cwd: {cwd}', fg='green'), err=True)
    click.echo('', err=True)

    exit_code = 0
    last_turn_count = 0
    last_cost: float | None = None
    crash_sticky_body: str | None = None
    try:
        async for message in query(prompt=user_prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        click.echo(block.text)
                    elif isinstance(block, ToolUseBlock):
                        click.echo(click.style(f'\n→ {block.name}', fg='cyan'), err=True)
                    elif isinstance(block, ThinkingBlock | ToolResultBlock):
                        pass
            elif isinstance(message, ResultMessage):
                last_turn_count = message.num_turns
                usage = message.usage or {}
                cost = message.total_cost_usd if message.total_cost_usd is not None else 0.0
                last_cost = cost
                click.echo(
                    click.style(
                        f'\n--- turns={message.num_turns}  '
                        f'in={usage.get("input_tokens", "?")}  '
                        f'out={usage.get("output_tokens", "?")}  '
                        f'cost=${cost:.4f}',
                        fg='yellow',
                    ),
                    err=True,
                )
                exit_code = 1 if message.is_error else 0
    except Exception as exc:  # noqa: BLE001 — SDK raises bare Exception; we narrow via turn-count heuristic
        # The SDK's `receive_messages()` raises a generic Exception when the consumer-set
        # `max_turns` is reached (see issue #913) AND for genuine transport errors. We use
        # the most recent ResultMessage's `num_turns` to distinguish: if we got close to
        # the cap, it's almost certainly a cap-hit; otherwise it's a real crash.
        # In either case the agent never reached its own step-11 sticky, so the harness
        # posts a crash sticky itself once we've resolved the PR number below.
        if last_turn_count >= max_turns:
            click.echo(
                click.style(
                    f'\n‼ Initiative hit the max_turns ceiling ({max_turns}). '
                    f'The SDK terminates abruptly on cap-hit (known: issue #913, '
                    f'lesson agent-sdk-crash-during-long-initiative). Substantive work is '
                    f'likely already pushed; re-fire is idempotent (agent detects the existing '
                    f'branch + PR). To give more headroom: re-run with `--max-turns 250`.',
                    fg='red',
                    bold=True,
                ),
                err=True,
            )
            exit_code = 2
            crash_sticky_body = _build_crash_sticky_body(
                reason=f'hit the `max_turns` ceiling ({max_turns}).',
                turn_count=last_turn_count,
                max_turns=max_turns,
                cost=last_cost,
                hint=(
                    "Substantive work is likely already pushed (this PR's commits). "
                    'Re-fire is idempotent — the agent detects the existing branch + PR. '
                    'For more headroom, re-run with `--max-turns 250`.'
                ),
            )
        else:
            click.echo(
                click.style(
                    f'\n‼ Unexpected SDK exception at turn {last_turn_count}/{max_turns}: {exc}',
                    fg='red',
                    bold=True,
                ),
                err=True,
            )
            exit_code = 1
            crash_sticky_body = _build_crash_sticky_body(
                reason=f'SDK crashed unexpectedly: `{exc}`',
                turn_count=last_turn_count,
                max_turns=max_turns,
                cost=last_cost,
                hint=(
                    "Substantive work may already be pushed (this PR's commits). "
                    'Re-fire is idempotent — the agent detects the existing branch + PR.'
                ),
            )

    pr_number = _resolve_pr_number(primary.qualified_repo, primary.branch)
    if crash_sticky_body is not None:
        _post_crash_sticky(
            qualified_repo=primary.qualified_repo,
            pr_number=pr_number,
            body=crash_sticky_body,
        )

    return RunSummary(
        exit_code=exit_code,
        turns=last_turn_count or None,
        cost_usd=last_cost,
        pr_number=pr_number,
    )


@click.command()
@click.argument('initiative_path', type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    '--repo-root',
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help='Override the auto-derived `~/leartech/<repo>` checkout path.',
)
@click.option('--model', default=DEFAULT_MODEL, show_default=True, help='Claude model.')
@click.option(
    '--max-turns', default=DEFAULT_INITIATIVE_MAX_TURNS, type=int, show_default=True, help='Hard cap on agent turns.'
)
def main(initiative_path: Path, repo_root: Path | None, model: str, max_turns: int) -> None:
    """Run an initiative YAML end-to-end via the write-mode agent."""
    summary = asyncio.run(run_initiative(initiative_path, repo_root=repo_root, model=model, max_turns=max_turns))
    sys.exit(summary.exit_code)


if __name__ == '__main__':
    main()
