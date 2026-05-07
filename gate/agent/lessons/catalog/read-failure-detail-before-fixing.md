---
id: read-failure-detail-before-fixing
title: Always read failure detail before proposing a fix
captured_at: 2026-05-04T10:50:00Z
source:
  type: agent_run
  reference: pr_37_iteration_2
  observer: claude-sonnet-4-6
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
  - review_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/read-failure-detail-before-fixing.md
encoded_at: 2026-05-04T18:00:00Z
---

When the gate fails, **fetch the actual failure detail** before guessing what's wrong.
Concretely:

- For `test_coverage_meets_threshold` failures: read the per-file LCOV breakdown to
  identify *which* uncovered lines are pulling the average down. Don't blindly add
  more tests — add tests that target the uncovered lines.
- For `test_specs_pass` (Playwright) failures: pull the trace.zip / video for the
  failing spec and read the assertion error before editing.
- For `test_unit_tests_pass` failures: read the Karma / pytest stderr — the failure
  message identifies the test + assertion. Compile errors and runtime errors require
  different fixes.
- For pipeline failures: use `~/leartech/Hub/scripts/pr-pipelines.sh <repo> <pr> --failed-only --logs`
  to dump the failing step's stderr to `./pr-logs/<pr>/`.

A guessed fix that doesn't address the root cause wastes a full pipeline cycle
(~10-30 min) and erodes trust. One careful read beats three speculative iterations.
