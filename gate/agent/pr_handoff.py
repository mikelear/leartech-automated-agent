"""Deterministic per-turn handoff writes to the PR.

The agent is not in control of when it stops: a controller deadline kill or a
node move can end an iteration mid-thought. The successor then starts with a
branch and a PR but no record of what its predecessor concluded, which is how a
fresh iteration came to rubber-stamp an unmerged PR.

Turn count and spend live nowhere durable in the AgentRun runtime. That Job gets
no DB DSN, so ``run_driver.update_run_progress`` falls back to a process-local
dict that dies with the pod, and the agent writes only ``status.targetPR`` to the
CR. This module puts them on the PR instead, via the ``post_pr_handoff`` MCP tool.

Deterministic, not prompt-driven: the LLM is not asked to remember to check in.
Cadence is decided here; the STATE classification (ok / approaching_limit /
exhausted) is derived by the MCP server from ``turns_used`` + ``turns_max`` so
there is exactly one implementation of that rule.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Post at least this often so a kill never loses more than this many turns of
# context. The MCP tool rewrites the comment only when the state digest changes,
# so a call that says nothing new costs one round-trip and no PR edit.
HANDOFF_EVERY_TURNS = 10

# Absolute floor for "near the ceiling": on a small max_turns a 10% margin is a
# turn or two, which is no warning at all. Mirrors the server's BudgetWarnTurns
# for CADENCE only — the server still classifies the state.
NEAR_LIMIT_TURNS = 15
NEAR_LIMIT_FRACTION = 0.1

HANDOFF_SERVER = 'jx3_flow'
HANDOFF_TOOL = 'post_pr_handoff'


class ToolCaller(Protocol):
    def __call__(
        self, base_url: str, server: str, tool: str, args: dict[str, Any]
    ) -> Awaitable[tuple[dict[str, Any], str | None]]: ...


def turns_remaining(turns: int, max_turns: int) -> int | None:
    """Turns left before the ceiling, or None when there is no usable ceiling."""
    if max_turns <= 0 or turns < 0:
        return None
    return max(0, max_turns - turns)


def is_near_limit(turns: int, max_turns: int) -> bool:
    """Whether the run is close enough to the ceiling to check in every turn."""
    remaining = turns_remaining(turns, max_turns)
    if remaining is None:
        return False
    return remaining <= NEAR_LIMIT_TURNS or remaining <= int(max_turns * NEAR_LIMIT_FRACTION)


def should_post(*, turns: int, max_turns: int, last_posted_turn: int) -> bool:
    """Cadence decision, pure so it is testable without a network or an SDK.

    Every turn once near the ceiling — that is the window where an interruption
    is most likely and the successor's need is greatest. Otherwise every
    HANDOFF_EVERY_TURNS turns. Never twice for the same turn.
    """
    if turns <= 0 or turns == last_posted_turn:
        return False
    if is_near_limit(turns, max_turns):
        return True
    return turns - last_posted_turn >= HANDOFF_EVERY_TURNS


def build_summary(*, turns: int, max_turns: int, last_tool_call: str | None, iteration: int) -> str:
    """What a successor needs from a machine-written checkpoint: where the run got
    to, not a narrative. The LLM's own reasoning arrives via the tool's other
    fields when it calls post_pr_handoff itself."""
    parts = [f'Automatic checkpoint at turn {turns}']
    if max_turns > 0:
        parts[0] += f'/{max_turns}'
    if iteration > 0:
        parts.append(f'iteration {iteration}')
    if last_tool_call:
        parts.append(f'last tool call `{last_tool_call}`')
    remaining = turns_remaining(turns, max_turns)
    if remaining is not None and is_near_limit(turns, max_turns):
        parts.append(f'{remaining} turn(s) of budget left — a successor should expect to take over')
    return '. '.join(parts) + '.'


async def post_handoff(
    *,
    base_url: str,
    repo: str,
    pr_number: int,
    run_id: str | None,
    iteration: int,
    turns: int,
    max_turns: int,
    cost_usd: float | None,
    last_tool_call: str | None,
    model: str | None = None,
    tool_caller: ToolCaller | Callable[..., Awaitable[tuple[dict[str, Any], str | None]]],
) -> tuple[bool, str | None]:
    """Write the checkpoint. Returns (posted, error); never raises.

    A failed handoff must never affect the run: it is a durability aid, not the
    mission. The MCP side already retries transient GitHub failures.
    """
    if not repo or pr_number <= 0:
        return False, 'no PR yet'
    args: dict[str, Any] = {
        'repo': repo,
        'pr_number': pr_number,
        'summary': build_summary(turns=turns, max_turns=max_turns, last_tool_call=last_tool_call, iteration=iteration),
        'turns_used': turns,
    }
    if run_id:
        args['run_id'] = run_id
    if iteration > 0:
        args['iteration'] = iteration
    if max_turns > 0:
        args['turns_max'] = max_turns
    if cost_usd:
        args['cost_usd'] = cost_usd
    if model:
        args['model'] = model

    try:
        result, err = await tool_caller(base_url, HANDOFF_SERVER, HANDOFF_TOOL, args)
    except Exception as exc:  # noqa: BLE001 — a checkpoint must not break the run
        logger.warning('handoff checkpoint raised for turn %s: %s', turns, exc)
        return False, f'{type(exc).__name__}: {exc}'
    if err:
        logger.warning('handoff checkpoint failed at turn %s: %s', turns, err)
        return False, err
    logger.info('handoff checkpoint written at turn %s (action=%s)', turns, (result or {}).get('action', '?'))
    return True, None
