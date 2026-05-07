---
id: video-review-frame-zero-is-load-state
title: Frame 0 of a Playwright video is the navigation moment, not an anomaly
captured_at: 2026-05-04T09:15:00Z
source:
  type: agent_run
  reference: pr_25_video_review_first_run
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: calibration
applies_to:
  - video_review
status: encoded
encoded_in:
  - gate/tools/video_review.py
encoded_at: 2026-05-04T09:30:00Z
---

When reviewing Playwright video frames for visual anomalies:

- The earliest frame(s) almost always capture the initial navigation moment before
  the page paints. A blank, partially-loaded, or white frame at index 0 (and
  occasionally index 1) is normal and **NOT** an anomaly.
- Only flag a blank/unrendered state if it persists across most subsequent frames,
  indicating the page never loaded.
- Brief blank moments mid-flow may be navigation transitions between pages — also
  not anomalies unless content fails to appear in the next frame.
- Use the spec name as a hint about what *should* be visible by mid-to-late frames.

This calibration was added to `VIDEO_REVIEW_SYSTEM_PROMPT` after the first live run
flagged frame 0 of `01-page-loads-app-root-element-renders` as a false-positive.
After the prompt fix, three video reviews passed clean against the same artifacts.
