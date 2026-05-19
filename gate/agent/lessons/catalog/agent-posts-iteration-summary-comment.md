---
id: agent-posts-iteration-summary-comment
title: When stopping iteration, the agent must post a structured summary comment to the PR
captured_at: 2026-05-17T10:35:00Z
source:
  type: agent_run
  reference: pr_11_webcoder_ui_about_page_8193c9767378
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/agent-posts-iteration-summary-comment.md
encoded_at: 2026-05-19T11:45:00Z
---

When the agent stops iterating on an initiative — whether because the work is
complete, the iteration budget is exhausted, or the remaining failures are
diagnosed as out-of-scope (infra issues, dependent repo bugs, cross-cluster
asymmetry) — it **must post a single structured summary comment to the PR
before exiting**.

The PR description captures the *initial plan*. Tekton bot comments capture
the *check-by-check verdicts*. Neither captures the agent's own *iteration
narrative* — what it tried, what it concluded, why it stopped. Without that
narrative on the PR itself, a human reviewer landing cold has to read the
pod log (which may be gone by the time they look) to understand the state.

This lesson is a stop-gap until the BA/forensic agents are wired and a more
structured feedback loop exists. Even after that lands, a self-contained
end-of-run comment on the PR remains valuable: it is durable, it is visible
without cluster access, and it documents the agent's *judgment* (not just its
*outputs*).

## How this surfaced

PR #11 on `mikelear/webcoder-ui` — the second dogfood demo, fired from the
deployed agent at run `8193c9767378` (2026-05-17 09:56–10:34Z, 37min).

The agent ran 4 iterations and stopped with 4 failing checks remaining
(`gcp/pr`, `gcp/end2end`, `gcp/end2end-ui`, `az/end2end-ui`). Its diagnosis
in the pod log was sharp:

- It identified that `gcp/pr` was failing on the same
  `leartech-angular-service-template@0.0.22` cross-service noise that hit the
  agent's *own* promo PRs 1h earlier (PR #403 on `jx-build-cluster-gsm`).
- It identified that GCP Lighthouse wasn't processing chatops retest commands.
- It concluded that further iteration would burn budget on infra problems it
  can't fix from inside the consumer-repo sandbox.

All three observations are correct and useful. **None of them landed on the
PR.** A reviewer reading PR #11 sees only "/hold" + a list of failing checks.
Mike's question that surfaced this lesson:

> "Has it commented its assumptions and findings to the PR?"

The honest answer was *partly* — PR description had file inventory + the
component-pattern assumption, but no end-of-run summary.

## Procedure

After the final iteration (whether successful, budget-exhausted, or
abandoned) and **before exiting the SDK loop**, the agent must:

1. **Compute the rollup**:
   - Iterations used / max (e.g. `4/7`)
   - Resolved check names (what flipped from fail → pass during the run)
   - Unresolved check names (still failing or pending)
   - Stopping reason: one of `complete`, `budget-exhausted`,
     `infra-diagnosis-out-of-scope`, `criteria-gap`, `dependency-on-other-repo`

2. **Classify unresolved failures**:
   For each unresolved check, briefly state which category — infra, code,
   external dependency, criteria misconfiguration. This is the most valuable
   part of the summary; reviewers shouldn't have to re-derive it.

3. **Post one comment** with this structure:

       ## Run summary

       **Iterations:** 4/7
       **Status:** stopped — remaining failures diagnosed as infrastructure

       **Resolved during this run:**
       - `az/ai-review` — addressed feedback from advisory reviewers
       - `az/lint`, `az/test` — passed after retest

       **Unresolved (not code issues):**
       - `gcp/pr` — same `angular-service-template@0.0.22` cross-service
         noise hitting other promo PRs today; not caused by this PR
       - `gcp/end2end` / `gcp/end2end-ui` — GCP Lighthouse not processing
         `/test` chatops commands; infra-level retest path broken
       - `az/end2end-ui` — likely related to the AZ Lighthouse keeper
         fork-exhaustion observed at 09:09Z today

       **Stopping rationale:** Further SDK iterations would burn budget on
       infra problems outside the sandbox's reach. Recommend human triage of
       the cluster-side issues, then `/test all` once the keeper is healthy.

       **Held pending review** (`/hold` previously posted, do-not-merge/hold
       label present).

4. Do not post the summary if the run exits via SDK crash (handled separately
   by the parent service). Do not post it more than once per run.

## Why "even if there are no failures"

A successful run also benefits from a summary comment — it shrinks reviewer
load. The summary need not be long when everything passed; in that case:

       ## Run summary

       **Iterations:** 2/7 — all checks green on first full pass after a single
       lint fix. Held pending review.

The cost of the comment is ~1 extra SDK turn; the benefit is durable.

## What this is NOT

- **Not a replacement** for posting `/hold` (still required per the
  `agent-prs-must-be-held-pending-review` lesson).
- **Not a replacement** for the PR description (which captures plan +
  assumptions made at start of run).
- **Not a replacement** for per-iteration commit messages.
- **Not a fix** for the in-memory `pr_number`/`turns`/`cost_usd` service-side
  bug — that needs a separate code fix in `app/routers/initiatives.py`.

## Calibration vs structural fix

This is a calibration lesson — applied via prompt injection at session start.
The structural fix (a "summary comment" tool baked into the runner that the
agent calls explicitly, or auto-posted by the service on terminal status) is
a follow-up worth doing once the BA/forensic agents are clearer. Until then,
this lesson carries the contract.
