---
id: chatops-recovery-on-stalled-tekton-checks
title: Block on `gh pr checks --watch`, then chatops-retrigger if a check stalls > 15 min
captured_at: 2026-05-04T11:35:00Z
source:
  type: agent_run
  reference: pr_37_iteration_2
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/chatops-recovery-on-stalled-tekton-checks.md
encoded_at: 2026-05-04T18:00:00Z
---

When you've pushed and need to wait for the Tekton pipeline to settle:

**Don't use ScheduleWakeup or sleep loops.** Use `gh pr checks` in watch mode — it's a
single Bash call that blocks until every required check reaches a terminal state, with
no reliance on harness-specific scheduling features:

    timeout 900 gh pr checks <pr> -R <repo> --watch --required --interval 30

`--watch` polls until terminal; `--required` ignores skipped/optional checks; `--interval 30`
is gentle on the API. Wrap with `timeout 900` so a stuck check doesn't hang the agent
indefinitely (15 min ceiling — long enough to catch a real run, short enough to act on
a stall).

If `timeout` fires (exit 124), the pipeline is wedged. Recovery flow:

1. Confirm the stall is real with `mcp__leartech-jx3-flow__list_pr_checks`. Pending-with-pod-RUNNING
   is normal; pending-no-pod for >15 min usually means the queue is wedged.
2. Post `/test <check-name>` (or `/retest`) as a PR comment via `gh pr comment` to retrigger:

       gh pr comment <pr> -R <repo> --body "/test test"

3. Resume the watch with another `timeout 900 gh pr checks ... --watch`.

This pattern was discovered when `az/test` stalled ~35 min on PR #37; chatops retrigger
unblocked it within a few minutes. The original implementation used `ScheduleWakeup`
which works only because the Agent SDK's underlying CLI harness happens to honour it —
not a stable contract. `gh pr checks --watch` is correct in any context (laptop CLI,
cluster pod, future pub/sub world).

**v2 direction** (see `project_v2_pubsub_direction.md`): the right primitive is
`subscribe pr.checks.terminal{repo, pr}` over a message bus — no polling, no timeouts.
For now, watch+retrigger is the portable stop-gap.
