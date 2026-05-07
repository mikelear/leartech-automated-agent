---
id: cite-failing-criteria-when-explaining-fixes
title: Cite specific failing criteria when explaining a fix
captured_at: 2026-05-03T14:00:00Z
source:
  type: agent_run
  reference: design_v1_planning
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
  - review_agent
status: encoded
encoded_in:
  - gate/agent/initiative_prompt.py
encoded_at: 2026-05-04T08:00:00Z
---

When proposing or applying a fix, **always cite the specific criterion name(s)** the
fix is responding to:

- "Fixing `test_coverage_meets_threshold[gcp]`: home.component.ts at 50%, lifting
  with new spec covering the authenticated path."
- "Skipping changes to `.lighthouse/jenkins-x/`: not relevant to
  `test_unit_spec_count_changed_when_app_changed`."

This makes the audit trail searchable, makes the agent's reasoning legible to future
reviewers, and surfaces criteria-gap signals — if you can't name the criterion
driving a change, the change probably shouldn't be made unless explicitly requested.

Same principle in commit messages: include the failing criterion name in the body.
Future-you searching git log for a regression will thank present-you.
