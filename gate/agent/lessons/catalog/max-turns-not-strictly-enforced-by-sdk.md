---
id: max-turns-not-strictly-enforced-by-sdk
title: claude_agent_sdk allowed agent to run 157 turns despite max_turns=150 — cap is soft, not strictly enforced
captured_at: 2026-05-06T12:30:00Z
source:
  type: agent_run
  reference: pr_9_webcoder_ui_new_dialog_video_spiral
  observer: mike@leartech
  latency_to_capture: minutes
category: tool_bug
applies_to:
  - initiative_agent
status: open
slipped_past_criteria: []
proposed_criterion: null
---

## What we observed

`gate/agent/initiative.py` sets `DEFAULT_INITIATIVE_MAX_TURNS = 150`,
passed through `ClaudeAgentOptions(max_turns=150)`. The SDK should
terminate the session once 150 turns have been completed.

PR #9's session ran for **157 turns** before terminating — 7 turns past
the cap. Final ResultMessage:

    --- turns=157  in=163  out=112017  cost=$10.2815

The session did NOT terminate from a clean cap-hit (which would have
triggered our client-side detection in `gate/agent/initiative.py`'s
exception handler). Instead it kept going until an Anthropic-side `429`
rate limit blocked further requests.

## Three possible interpretations

1. **Counting mismatch**: the SDK and our consumer count "turns"
   differently. `max_turns` may count something other than what
   `ResultMessage.num_turns` reports (e.g., excluding tool-result turns,
   or counting MCP responses as half-turns, or some other off-by-N).

2. **Soft cap with grace period**: the SDK enforces `max_turns` as a
   "stop after this many turns are *requested*" rather than "stop after
   exactly this many turns *complete*". A few extra turns can complete
   while in-flight requests drain.

3. **Cap not enforced at all once exceeded**: the SDK may silently log
   that the cap is exceeded but continue running until some other
   terminal condition hits (rate limit, error, or natural completion).

We can't distinguish from one observation; need to reproduce with a
controlled small `max_turns` (e.g., 10) on a guaranteed-multi-turn run.

## Why this matters

If `max_turns` is a soft cap, our client-side cap-hit detection (in
`gate/agent/initiative.py`'s exception handler) is misaligned with
reality:

- We check `last_turn_count >= max_turns` to identify "this exception
  was a cap-hit"
- If the SDK lets the agent run 5-10 turns past the cap, then a real
  exception fired *during* those bonus turns will look like a cap-hit
  to our heuristic — false positive
- Conversely, if the agent runs all the way to byte-rate-limit (as in
  PR #9), the cap-hit detection sees `num_turns=1` (after retry reset)
  and falls through to the "real exception" branch — the user sees the
  generic message instead of the cap-aware one

Both directions are bugs in our error reporting that originate from the
SDK's loose enforcement.

## Investigation plan

Before bumping the cap further or relying on it:

1. **Reproduce with `--max-turns 10`** on a deliberately multi-turn
   initiative. Observe: does `ResultMessage.num_turns` come back as
   exactly 10? 11? Some other number?
2. **Check SDK source** at
   `.venv/lib/python3.14/site-packages/claude_agent_sdk/_internal/`
   for the actual cap-enforcement code. Look for where
   `options.max_turns` is read and compared against an internal counter.
3. **File against upstream issue #913** if the cap is genuinely soft —
   either as a clarification request ("what does max_turns mean exactly?")
   or as a bug report ("max_turns isn't enforced").

## Workaround until clarified

The 157-vs-150 overshoot is small (~5%); not catastrophic. Adjust
practical expectations:

- Treat `max_turns` as approximate, not precise. Set it 5-10% below the
  point you genuinely don't want the agent to exceed.
- The byte-rate ceiling (`org-rate-limit-byte-budget-rewards-many-short-sessions`)
  is a much harder limit anyway — long enough sessions hit it before any
  conceivable `max_turns` value.

## Connection to issue #913

Issue #913 is about **the cap-hit termination raising a generic Exception
instead of clean termination**. This lesson is about **the cap not being
strictly enforced in the first place**. They're orthogonal SDK bugs that
both touch the same `max_turns` code path. When investigating, check
both questions in the same dive.
