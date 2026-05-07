---
id: agent-sdk-crash-during-long-initiative
title: claude_agent_sdk raises generic Exception when consumer-set max_turns is hit, instead of clean termination
captured_at: 2026-05-04T23:58:00Z
source:
  type: agent_run
  reference: pr_39_full_run
  observer: mike@leartech
  latency_to_capture: minutes
category: tool_bug
applies_to:
  - initiative_agent
status: open
slipped_past_criteria: []
proposed_criterion: null
---

## Root cause (revised 2026-05-05)

Initial framing was "SDK crashes mysteriously after long sessions" — **wrong**.
N=3 reproductions all reported exactly 61 turns; checking
`gate/agent/initiative.py` showed `DEFAULT_INITIATIVE_MAX_TURNS = 60`. The "61
turns" is just our consumer-set cap + 1 — i.e., the SDK reaching the cap and
trying to terminate.

**Actual bug**: when the consumer-set `max_turns` is reached, the SDK's
termination path raises a generic `Exception: Command failed with exit code 1`
instead of yielding a clean end-of-stream message (or a typed
`MaxTurnsReachedError`). Fully deterministic — fires every time the agent
uses up its turn budget, regardless of write phase / wait phase / MCP polling.

Stack trace (always identical):

    File "claude_agent_sdk/_internal/query.py", line 803, in receive_messages
        raise Exception(message.get("error", "Unknown error"))
    Exception: Command failed with exit code 1
    Error output: Check stderr output for details

## Reproductions: N=3 (all on max_turns=60, all reported turn 61)

| # | Date | Initiative | PR | Turns | Cost | Activity on final turn |
|---|---|---|---|---|---|---|
| 1 | 2026-05-04 | auth-ui-add-profile-page | #39 | 61 | $1.62 | wait-for-pipeline (MCP polling) |
| 2 | 2026-05-05 | webcoder-service-restore-canonical-makefile | #13 | 61 | $1.63 | wait-for-pipeline (23× MCP polling) |
| 3 | 2026-05-05 | webcoder-ui-add-initiatives-tab | #? | 61 | $2.71 | mid-fix Bash call, iteration-heavy |

The strikingly identical 61 was the diagnostic that uncovered the root cause —
worth remembering as a debugging move: *if the same number shows up across
unrelated reproductions, check your own configuration first*.

## Two mitigations now in place

### 1. Bumped `DEFAULT_INITIATIVE_MAX_TURNS` from 60 to 150

In `gate/agent/initiative.py`. Iteration-heavy runs (auth-latency e2e
debugging, multi-fix loops) demonstrably hit ~60 in real work. 150 gives
comfortable headroom while we wait for the SDK fix.

### 2. Client-side detection of cap-hit in our message loop

`gate/agent/initiative.py` now wraps `async for message in query(...)` in
try/except. When the SDK's bare `Exception` fires, we check
`last_turn_count >= max_turns`:

- If yes → cap-hit, exit code 2 with a clear message pointing at this lesson + issue #913
- If no → real transport error, exit code 1 with the original exception

This means consumers see *"Initiative hit the max_turns ceiling (150). Re-fire
is idempotent."* instead of an opaque traceback. The crash is no longer
mysterious-looking when it happens.

### What was NOT lost in any of the three crashes

Substantive work was already on the remote (commits pushed, PR open) every
time. The "push early, push often" discipline + idempotent re-fire means we
have a survivable bug, not a blocker.

## Upstream

Filed as [anthropics/claude-agent-sdk-python#913](https://github.com/anthropics/claude-agent-sdk-python/issues/913)
on 2026-05-05. First posted with the wrong framing (mysterious crash);
[follow-up comment](https://github.com/anthropics/claude-agent-sdk-python/issues/913#issuecomment-4380896580)
corrects to the actual root cause + includes a suggested client-side workaround
for other consumers.

Asks of the SDK team:
- Make `max_turns` reached a clean termination — either emit `{"type": "end", "stop_reason": "max_turns"}` or raise a typed `MaxTurnsReachedError`
- Surface subprocess stderr generally (still useful but secondary to the typed-exception fix)

## Defensive design — deferred to webCoder convergence

Even with max_turns bumped + client-side detection, the structural fix for
long-running initiatives is to **factor the wait-for-pipeline phase out of the
agent loop**:

- Agent's job: write code, push, open PR, post `/hold`, exit cleanly.
- Wait-for-pipeline: separate non-agent process. Posts the sticky comment when
  checks settle.

This sidesteps the bug class entirely (agent never reaches max_turns because
it doesn't burn turns waiting). **Do NOT design or build this primitive here.**
The webCoder session is designing an async-watcher / eventing layer for
completely different reasons — `~/.claude/projects/-Users-mikelear-leartech-webCoder/memory/project_agent_dashboard_eventing.md`.

**Trigger to revisit**: webCoder's eventing/async-watcher pattern lands. At
that point, the wait-phase factor-out becomes "consume webCoder's primitive",
not "design a new one."

## Cross-lesson principle worth surfacing

When two suspicious-looking signals appear at the same numeric value across
multiple reproductions (here: 61 turns in 3 unrelated runs), **check your own
configuration before blaming the dependency**. The N=3 identical signal made
the wrong framing look very plausible until we grepped our own constants.
