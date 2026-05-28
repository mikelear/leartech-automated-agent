---
id: self-retrospect-honesty
title: Be honest in self-retrospective; false positives are worse than empty findings
captured_at: 2026-05-28T12:00:00Z
source:
  type: agent_run
  reference: pr_46_and_pr_1_azure_openai_env_gap_2026_05_28
  observer: mike@leartech
  latency_to_capture: hours
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/self_retrospect.py
  - app/routers/initiatives.py
  - gate/agent/lessons/catalog/self-retrospect-honesty.md
---

After the agent posts "Ready for client review" on a successful run, a
one-shot LLM call is fired asking: "given this PR diff + AI review
verdict + final gate state, what could I have caught locally before
pushing?" The output is filed as a GitHub Issue with the
`self-retrospective` label on the originating repo.

The retrospect prompt asks for structured JSON; findings become triage
input for either a calibration lesson, a pytest criterion, a tekton
step, or a pre-push check. The Issue is the hand-off; Mike decides
which become real initiatives.

## When prompted to identify things you should have caught locally

- **Do** identify real cross-file consistency gaps you missed. PR #27
  (Azure OpenAI 4th reviewer) is the canonical example: the agent
  added a new env-var consumer but didn't propagate to the producer
  Tekton task. That's a real, generalisable gap worth a criterion.
- **Do** name concrete enhancements (lesson / criterion / tekton-step
  / pre-push-check). Each finding should map to ONE form.
- **Do** prioritise honestly: `high` for issues that caused real
  reviewer feedback or post-merge fixes; `medium` for issues that
  *could* have caused them; `low` is filtered out by the dispatcher
  anyway so don't pad with low items.

## What NOT to do

- **DON'T** invent findings to fill the JSON schema if you genuinely
  have none. Empty `findings: []` is the honest answer when work was
  clean — and the dispatcher knows to file no Issue in that case, so
  there's zero downstream cost to honesty.
- **DON'T** propose enhancements that already exist. The lessons
  catalog at `gate/agent/lessons/catalog/` and the criteria registry
  at `gate/criteria/` are the prior art — verify against them before
  suggesting a "new" lesson or criterion.
- **DON'T** raise generic process critiques ("I should be more
  careful"). Every finding must be a SPECIFIC, REPRODUCIBLE thing —
  with a clear root cause and a clear local check that would have
  flagged it.
- **DON'T** retrospect on failures that the gate would obviously have
  caught (lint errors, test failures). The retrospect is for issues
  that *slipped through* the gate. If lint or test caught it before
  human review, the gate did its job — no retrospective needed.

## How the dispatcher uses the output

- 0 findings → no Issue filed. Honest answer is free.
- ≥1 finding (after dropping `low`) → one Issue per run with all
  findings as sections. Labels: `self-retrospective` +
  `candidate/<form>` for each finding form represented.

## Cost

~$0.25 per retrospective call on Opus (2000 output tokens). Skipped
entirely for PRs under 10 changed lines. Can be disabled per-cluster
via the `LEARTECH_AGENT_SELF_RETROSPECT=false` env var if rollout
reveals issues.

## Why this lesson exists

The retrospect prompt explicitly tells the model "do not invent
findings — false positives are worse than empty". That guidance is
*also* a calibration lesson because future versions of the prompt
(or future agents introspecting the same way) should preserve the
honesty invariant.

Pairs with `cite-failing-criteria-when-explaining-fixes` — the same
principle of "say what you mean, no padding" applied to a different
surface.
