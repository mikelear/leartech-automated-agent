---
id: video-review-priority-toward-new-surface
title: Video review prioritises alphabetical-first specs, missing the AI-drafted spec it should validate
captured_at: 2026-05-05T00:02:00Z
source:
  type: agent_run
  reference: pr_39_full_run
  observer: mike@leartech
  latency_to_capture: minutes
category: criteria_gap
applies_to:
  - test_video_visual_review
status: encoded
encoded_in:
  - gate/criteria/per_repo/auth_ui/test_video_review.py
encoded_at: 2026-05-05T00:15:00Z
slipped_past_criteria:
  - test_video_visual_review
proposed_criterion: |
  Bias `pytest_generate_tests` in test_video_review.py toward specs whose names
  overlap with the diff's new UI surface (component selectors, route paths,
  data-testid prefixes). Same VIDEO_REVIEW_MAX_SPECS cap; just smarter ordering.
---

PR #39's loop demonstrated the AI coverage scanner pattern end-to-end **except** for
the final closing step: the AI-drafted Playwright spec produced a real video on GCS,
but the video-review criterion didn't watch it.

Cause: `pytest_generate_tests` in `gate/criteria/per_repo/auth_ui/test_video_review.py`
sorts gcp specs alphabetically and takes the first `VIDEO_REVIEW_MAX_SPECS` (default 3).
On PR #39 those were three variants of `01-page-loads-*`, leaving `03-profile-page`
unreviewed despite being the spec that should have had the most scrutiny.

## The closed loop intent vs reality

```
new UI surface
  → criterion flags coverage gap
  → AI drafts spec
  → spec runs, produces video
  → video review verifies the AI-drafted spec works visually  ← MISSING
  → green
```

We got steps 1–4. Step 5 was technically running but reviewing the *wrong* specs.

## Fix

Read the PR diff → compute `UISurfaceDelta` → score each gcp spec by name-overlap
with the delta's selectors / routes / testids → sort descending → take top N.

Falls back to alphabetical when there's no UI delta (spec-only / config-only PRs).

After fix: PR #39's `03-profile-page` would score highly (matches `profile` from
both the route and the `app-profile` selector after the `app-` prefix is stripped),
sorting to position #1 in the parametrization.

## Deeper signal

The video-review criterion was designed for static-cap "review N specs per run"
sampling. That's reasonable for cost control but **wrong** when a PR explicitly
introduces new surface — the new surface is exactly where review value is highest.
The lesson generalises: cost-controlled sampling needs a priority signal, not just
a cap.
