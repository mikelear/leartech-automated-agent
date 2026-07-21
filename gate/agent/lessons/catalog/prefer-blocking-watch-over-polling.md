---
id: prefer-blocking-watch-over-polling
title: Prefer blocking `gh pr checks --watch` over MCP polling loops when waiting on terminal events
captured_at: 2026-05-05T00:55:00Z
source:
  type: agent_run
  reference: pr_40_close_out_demo
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/prefer-blocking-watch-over-polling.md
encoded_at: 2026-05-05T00:55:00Z
---

When waiting for Tekton checks to reach a terminal state, **always use a single
blocking Bash call**:

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

**Do NOT loop-poll** `mcp__leartech-jx3-flow__list_pr_checks` (or any other MCP
read tool) waiting for state to change. Each MCP call burns:

- 1 agent turn
- ~$0.02–0.03 in tokens
- ~5–15 seconds of wall-clock per call

A polling loop costs $0.20–0.50 per minute of waiting. The blocking `--watch` call
costs **zero** — `gh` sleeps inside a subprocess that the agent isn't billing for.

## How this surfaced

Observed live during PR #40's close-out demo: the agent called
`mcp__leartech-jx3-flow__list_pr_checks` 7+ times in a row across ~7 turns ($0.18)
while the actual checks had already reached terminal state. The agent was
"watching in the background" but its mental model was MCP-poll, not Bash-block.

## Procedure

**Best (preferred): use the `wait_for_terminal` MCP tool**

    mcp__leartech-jx3-flow__wait_for_terminal(repo, pr_number, timeout_seconds=900)

This wraps `gh pr checks --watch` inside the MCP server's subprocess — zero
agent-turn cost during the wait. Returns a structured result with
`status: "all_passed" | "some_failed" | "timeout"` plus the final checks state.
This is the v1 stop-gap until pub/sub (v2.0) replaces it entirely.

**Fallback (when running outside the agent loop): blocking Bash**

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

Same primitive, exposed directly. Use when you're at a shell, not driving
through the MCP layer.

**On timeout** (`status: "timeout"` from MCP, exit 124 from Bash) — the pipeline
is wedged. Apply chatops recovery (`chatops-recovery-on-stalled-tekton-checks`
lesson): confirm via one `list_pr_checks` call, post `/test <check>`, then
`wait_for_terminal` again.

**Only after the wait returns terminal** should the agent run
`mcp__leartech-criteria__run_criteria_set` for the gate verdict. The MCP tools
are for *acting on terminal state*, not polling toward it.

## Why this lesson is a stop-gap, not a permanent rule

The whole class of "wait for an external event" problems is the wrong shape for
agents. The right architecture is **pub/sub** — the agent subscribes to
`pr.checks.terminal{repo, pr}` on a message bus and is woken by a notification
when the state actually changes. No polling, no timeouts, zero waiting cost.

See `project_v2_pubsub_direction.md` for the design memo. When v2.0 ships
(NATS / Redis pubsub on cluster, single topic for `pr.checks.terminal`), the
`mcp__leartech-bus__wait_for_pipeline_terminal` MCP tool replaces both this
polling pattern and the `gh pr checks --watch` workaround.

**This calibration lesson becomes obsolete when v2.0 lands.** Until then,
`--watch` is the portable correct answer.
