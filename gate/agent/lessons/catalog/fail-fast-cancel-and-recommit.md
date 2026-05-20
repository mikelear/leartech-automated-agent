---
id: fail-fast-cancel-and-recommit
title: Use `wait_for_first_failure_or_all_pass` between push and decision — don't wait for the slowest check
captured_at: 2026-05-20T13:00:00Z
source:
  type: agent_run
  reference: mortgages_pr1_walk_away_b4274fc623b1
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/fail-fast-cancel-and-recommit.md
encoded_at: 2026-05-20T13:00:00Z
---

`wait_for_terminal` waits for **every** required check to terminate — slowest
check wins. End2end on a Go service takes 8–10 minutes; lint on the same PR
finishes in 30s. If lint fails, the agent already knows it has to commit a
fix and start a fresh PR cycle. Waiting another 9 minutes for end2end to
finish before the agent reacts is pure latency.

`wait_for_first_failure_or_all_pass` is the fail-fast counterpart. Polls
`list_pr_checks` at short intervals and returns as soon as **either**:

- ANY check fails (`status: "first_failure"`, with the failing check's
  cluster/name/state/pipelinerun-name in `first_failure`), OR
- ALL checks succeed (`status: "all_passed"`).

Surfaces lint failures in ~15s while end2end is still running, so the agent
iterates on a fresh commit immediately.

## When to use which

| Tool | Use for |
|---|---|
| `wait_for_first_failure_or_all_pass` | Between push and the next decision point — "should I iterate or is this done?" |
| `wait_for_terminal` | Final-state confirmation before the "ready for client review" sticky — you want to be certain every check is settled |

Use the fail-fast one inside the iteration loop. Use the full-terminal one
before the final sticky.

## Loop shape

```python
while iterations < max_iterations:
    # ... edit, push ...
    result = wait_for_first_failure_or_all_pass(repo, pr, timeout_seconds=1800)
    if result.status == 'all_passed':
        break  # move to final sticky
    if result.status == 'first_failure':
        # classify: code-fixable / transient / pre-existing
        # iterate on code-fixable, /test retest on transient, classify on pre-existing
        continue
    if result.status == 'timeout':
        # 30 min with neither all-pass nor any-fail — likely a real stall
        # /retest may unblock, or escalate
        ...
```

## Why we don't auto-cancel in-flight checks yet

When lint fails fast and the agent iterates, the OTHER checks (end2end,
security-scan, dynamic-scan) keep running on the stale SHA — wasted cluster
time. Ideal: cancel them, free resources, push the new commit, new run starts.

That requires cross-cluster `kubectl patch pipelinerun ... status=Cancelled`
with RBAC the agent's ServiceAccount doesn't have today. It's deferred to a
follow-up. For now: accept the wasted in-flight time, optimise via fail-fast
on the **next** cycle instead.

When the cancellation primitive ships, the loop body becomes:
1. Receive `first_failure`
2. Classify as code-fixable
3. Call `cancel_pending_checks(repo, pr)` — kills the wasted in-flight pipelineruns
4. Edit/commit/push — fresh pipelines start on the new SHA

## Pairs with

- `retest-transient-failures-not-walk-away` — when first_failure is
  transient, this lesson says retest instead of iterating code.
- `chatops-recovery-on-stalled-tekton-checks` — when the wait times out,
  `/retest` to unblock.
- `prefer-blocking-watch-over-polling` — same principle (block in the
  subprocess, not in the agent loop); this lesson adds the fail-fast
  variant.

## Why this matters

Build + test + scan + deploy + e2e takes 8-15 minutes on a fresh PR. If the
agent iterates 3 times, that's ~30-45 minutes wall-clock just from "waiting
for slowest check" overhead. Fail-fast collapses that to ~30-45s per
iteration on lint failures, which is most of them. Compounds across every
initiative.

Mike's framing 2026-05-20: "the agent's job is to get all Tekton pipelines
green" — but the path to "all green" includes many cycles of "fix one
thing", and each cycle should be as short as the fastest failure-signal.
