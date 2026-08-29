"""Async PR-review loop using the Claude Agent SDK. Read-only: gathers context via
the Go MCP servers and produces a written verdict."""

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
from gate.agent.system_prompt import REVIEW_SYSTEM_PROMPT
from gate.agent.tool_logging import log_advertised_tools
from gate.mcp_servers import build_remote_mcp_servers

DEFAULT_MODEL = os.environ.get('LEARTECH_AGENT_MODEL', 'claude-opus-4-7')
DEFAULT_MAX_TURNS = 20

MCP_ALLOWED_TOOLS = [
    'mcp__leartech-jx3-flow__list_pr_checks',
    'mcp__leartech-jx3-flow__wait_for_terminal',
    'mcp__leartech-jx3-flow__wait_for_first_failure_or_all_pass',
    'mcp__leartech-pr-context__open_pr',
]


def _build_system_prompt() -> str:
    """JX3 platform calibration, then the review prompt."""
    blocks: list[str] = [load_jx3_calibration()]
    blocks.append(REVIEW_SYSTEM_PROMPT)
    return '\n\n---\n\n'.join(blocks)


def _build_options(model: str, max_turns: int) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=_build_system_prompt(),
        mcp_servers={**build_remote_mcp_servers()},
        allowed_tools=MCP_ALLOWED_TOOLS,
        permission_mode='bypassPermissions',
        max_turns=max_turns,
        model=model,
    )


async def review_pr(
    repo: str, pr_number: int, *, model: str = DEFAULT_MODEL, max_turns: int = DEFAULT_MAX_TURNS
) -> int:
    """Drive Claude through a PR review using the Go MCP servers. Returns exit code."""
    if not os.environ.get('ANTHROPIC_API_KEY'):
        click.echo(
            'ANTHROPIC_API_KEY not set. Run `leartech-claude-key` to fetch from the cluster.',
            err=True,
        )
        return 2

    options = _build_options(model, max_turns)
    log_advertised_tools(options.mcp_servers or {}, options.allowed_tools or [], logger='agent.review')
    user_prompt = (
        f'Review {repo}#{pr_number}. Use the MCP tools to gather context, '
        f'run the gate, and produce a concise review report following the structure in your system prompt.'
    )

    exit_code = 0
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
