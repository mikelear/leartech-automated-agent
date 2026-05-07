---
id: schedulewakeup-works-in-sdk-context
title: ScheduleWakeup is honoured by query() — earlier assumption was wrong
captured_at: 2026-05-04T11:30:00Z
source:
  type: agent_run
  reference: pr_37_iteration_2
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: architecture
applies_to: []
status: encoded
encoded_in: []
---

Empirical finding — `ScheduleWakeup` works inside `claude_agent_sdk.query()`.

I had assumed it was a Claude Code interactive-harness-only feature and would be a
no-op in the SDK. The first initiative run (PR #37 iteration 2) called
`ScheduleWakeup` 8+ times across 35 minutes; every wakeup fired on schedule, the
agent resumed, polled, scheduled again, and so on. The query stream kept its session
alive across each wakeup.

**Implication**: the orchestrator-side wait/retry loop I had pencilled in for v1.5
(Python-side polling + re-invoking the agent) is **NOT NEEDED**. The agent handles
its own waits via `ScheduleWakeup`.

**Generic lesson**: don't assume SDK feature gaps; verify against real runs before
designing around them. We avoided shipping an entire wait-loop infrastructure that
would have been redundant.
