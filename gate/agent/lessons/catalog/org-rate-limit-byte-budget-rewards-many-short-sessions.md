---
id: org-rate-limit-byte-budget-rewards-many-short-sessions
title: Anthropic's 20M-prompt-byte/hour org limit is a real architectural constraint — long agent sessions hit it before max_turns does
captured_at: 2026-05-06T12:30:00Z
source:
  type: agent_run
  reference: pr_9_webcoder_ui_new_dialog_video_spiral
  observer: mike@leartech
  latency_to_capture: minutes
category: architecture
applies_to:
  - initiative_agent
  - read_only_review_agent
status: open
slipped_past_criteria: []
proposed_criterion: null
---

## What we observed

PR #9 (`webcoder-ui` add-initiative-new-dialog) ran for 157 turns over ~3.5
hours, accumulating $10.28 in costs and ending with a hard `429` from
Anthropic's API:

    429 · This request would exceed your organization's rate limit of
    20,000,000 prompt bytes per hour (model: claude-sonnet-4-6)

This is **not** our `max_turns` cap (still 150), and it's not a
per-request limit. It's an **org-wide rolling-1-hour byte ceiling** that
applies to the whole organisation across every concurrent client.

## Why it bites long sessions specifically

The Agent SDK sends the **full conversation context** with each turn:

- system prompt (~10K bytes including all calibration lessons)
- entire transcript of prior turns (grows monotonically)
- tool result history (every Bash output, every Read, every MCP response)

By turn 50, a single prompt can be 50K+ tokens (~200K bytes). By turn
150, 150K+ tokens (~600K bytes). One long session is doing *N²/2* worth
of byte-traffic where N is the turn count — quadratic growth.

20M bytes/hour translates to maybe 30-40 turns of a typical mid-sized
session, OR 150 turns of a small one. **The byte ceiling is hit by long
agent loops long before any single turn hits a token limit.**

## The architectural implication

**Long iteration loops on one agent session don't scale.** They:

- Burn cost quadratically with turn count
- Hit org-wide rate limits that block other agents in your org
- Lose context coherence as the transcript fills with stale tool calls
- Maximize blast radius of any one bad call (you can't restart cleanly)

The right architecture is **many short independently-fired sessions**:

- Each session has a single, bounded job (one fix, one commit, one PR)
- Each session has fresh context, never approaches byte ceilings
- Sessions communicate via persistent state (PR status, branch, sticky
  comments, lesson catalog), not in-memory transcript
- An orchestration layer fires the next session when prerequisites hold

This is exactly the shape of:

- **`project_initiative_dependency_graph.md`** — the roadmap for declaring
  `depends_on: merged: <pr>` and firing the next initiative when
  conditions hold
- **wait-phase factor-out** (deferred, see
  `agent-sdk-crash-during-long-initiative.md`) — agent exits cleanly
  after push, separate worker watches checks and fires follow-up agent
  on terminal state
- **webCoder's eventing/dashboard work** — async-watcher pattern that
  composes with the above

## What this lesson encodes for the agent

Two procedural rules:

1. **Push early, push often** (already encoded — see `INITIATIVE_SYSTEM_PROMPT`).
   Each push is a checkpoint that lets a future session resume from
   persistent state instead of replaying transcript.

2. **Recognise iteration loops as a signal to stop**: if the agent has
   iterated on the same criterion 3+ times without progress, the
   criterion may be the problem (see
   `ai-video-review-needs-saliency-calibration`). Don't keep trying —
   exit cleanly with a verdict that flags the criterion as needing human
   judgement, and let a fresh session retry with calibration improvements
   if needed.

## What this lesson encodes for the platform

When v2 form-factor work begins (K8s Job runtime + eventing layer in
webCoder), bake byte-budget awareness into the design:

- Per-session byte budget (e.g. 2M bytes / one-tenth of org/hour) — fail
  fast with a clear "this initiative has grown too large; split into
  smaller initiatives" message
- Orchestration layer must fire sessions sequentially, not in parallel,
  unless byte budget headroom is verified
- Sessions should checkpoint state (PR + commit + sticky) every 5-10
  turns so re-fire from cold context is always possible

## Why this is `architecture`, not `tool_bug` or `calibration`

It's a property of the API + the agent's session model, not a bug in
either. The fix is at the architectural layer — many short sessions
instead of one long one. The calibration angle (rule #2 above) is
secondary to the structural shift.

## Trigger to act

Already triggered. PR #9's $10.28 single-initiative cost and the rate-limit
hit means we can't keep running multi-hour sessions. Next initiative
should:

- Set a soft target of <30 turns and revisit the system prompt's
  push-early-push-often emphasis
- Watch byte-rate consumption deliberately (a 30-min run is much safer
  than a 3-hour one for org budget)
- If a criterion needs >3 iterations, stop and mark it for
  human/calibration review

Beyond that: this lesson informs the v2 form-factor and webCoder
eventing-layer design — both of which already had this shape in mind for
other reasons. Good news: the architectural pressure is already pointing
the same direction.
