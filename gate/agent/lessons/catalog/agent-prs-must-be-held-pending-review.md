---
id: agent-prs-must-be-held-pending-review
title: Agent-authored PRs must post `/hold` to prevent auto-merge before human review
captured_at: 2026-05-05T00:25:00Z
source:
  type: agent_run
  reference: pr_39_full_run
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/initiative_prompt.py
  - gate/agent/lessons/catalog/agent-prs-must-be-held-pending-review.md
encoded_at: 2026-05-05T00:25:00Z
---

When an initiative-agent opens a PR, it must immediately post `/hold` as a PR
comment to block auto-merge:

    gh pr comment <pr> -R <repo> --body "/hold"

## Why this matters

PR #39 (the AI-coverage-scanner demo) auto-merged into `main` once all gate checks
went green — **without any human reviewer ever seeing the change**. The catalog's
auto-merge logic doesn't distinguish "agent-authored" from "renovate-authored" from
"human-authored"; once green, all are merge-eligible.

For agent runs that's a real governance gap. The agent's "Ready for client review"
sticky is the *agent's verdict*, not approval. Auto-merging on the agent's own
verdict creates a closed loop with no human in it.

## How `/hold` works

Lighthouse Keeper (the JX3 merge controller) honours chatops commands:

- `/hold` — sets the `do-not-merge/hold` label, blocks auto-merge regardless of checks
- `/hold cancel` — clears the hold, lets auto-merge proceed

The hold stays in place until cleared. Human reviewers cancel it after review.

## Hard rules for the agent

1. **Always post `/hold`** as one of the first comments after `gh pr create`.
2. **Never post `/hold cancel`** — only humans cancel the merge hold.
3. **Don't apologise for the hold in the PR description** — it's the safe default,
   not an exception. State plainly: "Held pending human review (`/hold` posted)."
4. The "Ready for client review" sticky is still posted when gate is green; the
   sticky's job is to summarise *what to review*, not to clear the merge gate.

## Mitigation if the hold was missed (e.g. PR #39)

PR #39 already merged. Going forward:

- For *future* agent PRs, the system prompt now mandates `/hold` (encoded in
  `INITIATIVE_SYSTEM_PROMPT` step 5).
- Catalog-side: a longer-term fix is to add a `requires-human-review` label that
  Keeper rejects for auto-merge by default — only removed by human action. That's
  out-of-scope for this lesson; raise as a `leartech-pipeline-catalog` issue.
- Org-policy: consider adding GitHub branch-protection rules requiring at least
  one human reviewer (different login from the PR author) on `main`. That'd
  belt-and-braces the agent's hold convention.
