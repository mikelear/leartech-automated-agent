## Calibrations from past runs

_The following lessons were learned from real agent runs and have been canonicalised. They take precedence when in conflict with general guidance below._

### Cite specific failing criteria when explaining a fix

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

### Always read failure detail before proposing a fix

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
