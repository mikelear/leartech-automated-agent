---
id: agent-prs-must-be-held-pending-review
title: PR merge-hold is an OPT-IN Initiative field; when set, the agent posts `/hold` and never cancels it
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
  - gate/initiatives/loader.py
  - gate/agent/lessons/catalog/agent-prs-must-be-held-pending-review.md
encoded_at: 2026-05-05T00:25:00Z
---

Merge-hold is an OPT-IN Initiative field
(:class:`gate.initiatives.loader.Initiative.hold`, default ``False``):

* ``hold: false`` (default) — the agent does NOT post ``/hold``. Once all gate
  checks are green (incl. real ai-review) Tide auto-merges. The gate suite IS the
  review; the fail-fast loop fixes red. Plans self-complete.
* ``hold: true`` — the agent posts ``/hold`` immediately after opening the PR to
  require human ``/hold cancel`` before merge. Reserve for initiatives that
  legitimately need out-of-band human sign-off.

Regardless of the ``hold`` value, the agent NEVER posts ``/hold cancel`` — only
an approver (a human, or a future dedicated approver bot) cancels a hold.

## Historical context — why this used to be unconditional

PR #39 (the AI-coverage-scanner demo, 2026-05-05) auto-merged into ``main`` once
all gate checks went green — **without any human reviewer ever seeing the change**.
The reaction was to bolt an unconditional ``/hold`` into every agent-authored PR,
which fixed the governance gap but replicated Tide: green→Tide-would-auto-merge,
but the hold blocked it, so plans never self-completed.

The current shape treats the gate suite (incl. real ai-review) AS the review,
and makes ``/hold`` an explicit opt-in for the narrow set of initiatives that
genuinely need human sign-off before merge. The initiative YAML declares the
policy; the agent obeys.

## How `/hold` works

Lighthouse Keeper (the JX3 merge controller) honours chatops commands:

- `/hold` — sets the `do-not-merge/hold` label, blocks auto-merge regardless of checks
- `/hold cancel` — clears the hold, lets auto-merge proceed

The hold stays in place until cleared. An approver (human today; potentially a
dedicated approver bot in future) cancels it after review.

## Hard rules for the agent

1. **Post `/hold` only when the initiative's `hold` field is `true`** — as one of
   the first comments after `gh pr create`. When `hold` is false/absent, do NOT
   post `/hold`; let Tide auto-merge on green.
2. **Never post `/hold cancel`** — regardless of `hold` value, only an approver
   cancels a merge hold placed by anyone else.
3. **Don't apologise for the hold in the PR description** — when `hold: true` is
   set, it's the initiative's explicit policy. State plainly: "Held pending
   review (`/hold` posted per initiative's `hold: true`)."
4. The "Ready for client review" sticky is still posted when gate is green; the
   sticky's job is to summarise *what to review*, not to clear the merge gate.

## Mitigation if the hold was missed (e.g. PR #39)

PR #39 already merged. Going forward:

- The system prompt is rendered per-initiative via
  ``render_initiative_system_prompt(hold=initiative.hold)`` — the ``/hold``
  posting instruction is present iff the initiative opts in.
- Catalog-side: a longer-term fix is to add a `requires-human-review` label that
  Keeper rejects for auto-merge by default — only removed by human action. That's
  out-of-scope for this lesson; raise as a `leartech-pipeline-catalog` issue.
- Org-policy: consider adding GitHub branch-protection rules requiring at least
  one human reviewer (different login from the PR author) on `main`. That'd
  belt-and-braces the initiative's hold convention where used.
