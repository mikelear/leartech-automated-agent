---
id: retest-transient-failures-not-walk-away
title: When a check fails for transient reasons, retest via /test — never walk away with red checks unclassified
captured_at: 2026-05-20T12:30:00Z
source:
  type: agent_run
  reference: mortgages_api_pr1_b4274fc623b1
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/retest-transient-failures-not-walk-away.md
encoded_at: 2026-05-20T12:30:00Z
---

**The agent's job is to get every Tekton pipeline check green** — not
to declare done as soon as no code change is required. If checks are
still pending or transiently failing, the agent must wait and retest,
not walk away.

## The anti-pattern (observed 2026-05-20)

On `mortgages-api PR #1` and `mortgages-gw PR #1`, the agent opened
the PR, posted `/hold`, then stopped at iteration 0 with the sticky
saying:

> "Still running (~75 min in, first-time build): `az/end2end`, `gcp/end2end`, ...
>  Recommend human review once all checks reach terminal state."

`az/end2end` and `gcp/end2end` then **failed** because the brand-new
repo's preview deploy didn't reach 3 consecutive 200s on `/health/live`
within 10 minutes (first-ever build of a new repo — no kaniko cache,
slow first-image-pull, cold pod start). The preview was healthy 90s
later, but the agent had already concluded.

A `/test end2end` retest at that point would have passed cleanly. The
agent should have done that.

## Why this matters

Walking away with red checks unclassified violates two design rules:

1. The agent's success criterion is **all checks SUCCESS or
   classified as pre-existing infra outside the diff** — not "no code
   change needed, hand off to human".
2. Lighthouse Merge Status can never go green until checks resolve. A
   PR left with red transients sits indefinitely waiting for a human
   who doesn't know to retest.

## Procedure

After posting `/hold` + sticky, **before declaring done**:

1. **Wait for all checks to reach terminal** (SUCCESS or FAILURE),
   using `mcp__leartech-jx3-flow__wait_for_terminal`. Don't stop
   while any check is PENDING.

2. **For each FAILURE, classify**:

   | Class | Signal | Action |
   |---|---|---|
   | **Code-fixable** | failure log cites a file in your diff | iterate: edit, push, repeat |
   | **Transient timing** | first-build, cold preview, kaniko OOM on small node, network blip | retest: `gh pr comment <pr> -R <repo> --body "/test <check>"`, wait again |
   | **Pre-existing infra** | failure path outside your diff, recurrent on other PRs | classify in sticky, don't fight |

3. **Only post the "ready for review" sticky once every check is
   SUCCESS or in the pre-existing-infra bucket.** Red transients
   without retest = not done.

## Known transient patterns + retest commands

| Failing check | Common cause | Retest command |
|---|---|---|
| `*/end2end` | Preview not ready in 10 min on first build of a new repo | `gh pr comment <pr> --body "/test end2end"` |
| `*/dynamic-scan` | Preview pod CreateContainerConfigError → not reachable | `/test dynamic-scan` (after preview is healthy) |
| `*/security-scan` | Pod evicted (node memory pressure) | `/test security-scan` |
| `*/pr` (kaniko build) | Kaniko OOM on a 16 GiB build node for a heavy-image service | `/test pr` (may need infra fix — see Hub Instance 5) |
| Any check, pending > 15 min with pod gone | Tekton queue wedged | `/retest` (or `/test <check>`) |

## When to STOP retesting

Don't loop forever. After **2 retests of the same check failing the
same way**, classify it as either:
- A real infra issue → mention in sticky as "needs infra fix, not in
  diff scope", post the sticky, hand off
- A real test failure (something the gold-standard chart should
  produce but doesn't) → flag as a setup gap

Specifically: if a brand-new repo has no `/health/live` endpoint at
all because the template doesn't bootstrap one, retest won't help.
That's a chart/template gap, classify and continue.

## Pairs with

- `chatops-recovery-on-stalled-tekton-checks` — same `/test` mechanism
  but for PENDING-too-long checks. This lesson covers FAILED-but-transient.
- `cite-failing-criteria-when-explaining-fixes` — when classifying a
  failure, cite the actual check + step + log line.
- `full-gate-verification-before-sticky` — the same "don't stop too
  early" principle, applied at the gate-test level.
