---
id: preflight-target-repo-quality-check
title: Before working on a repo, verify its .lighthouse/ pipelines match the language gold-standard
captured_at: 2026-05-19T10:00:00Z
source:
  type: agent_run
  reference: pr_6_phase0_dynamic_mcp_registry
  observer: mike@leartech
  latency_to_capture: minutes
category: calibration
applies_to:
  - initiative_agent
status: open
---

Before starting work on a target repo, the agent must run a **pre-flight quality
check** against the language's gold-standard pipeline configuration. If
critical pipelines are missing (ai-review, security-scan, image-scan,
dynamic-scan) the agent must either:

1. **Warn loudly** in the resulting PR description that AI review + security
   scans will not run on this PR, and recommend a separate
   `chore(lighthouse): align with <lang>-gold-standard` PR be merged first.
2. **Refuse to proceed** (preferred when the missing pipelines would have
   caught known regressions in the planned work).

Pattern observed 2026-05-19: every leartech-automated-agent self-modification
PR (PRs #1-#6) ran only with `lint` + `pr` checks. The repo's
`.lighthouse/jenkins-x/triggers.yaml` referenced `ai-review/*.yaml` and
`security-scan/*.yaml` source files that **did not exist** — so Lighthouse
silently dropped the triggers. The agent's own source was held to a weaker
CI bar than the consumer repos it works on (auth-ui PRs get 11 checks).

Mike's framing: "Part of the automated agent's pre-checks should be to do a
quality check using the templates and maybe warn and fail the review if it
feels it's missing a lot of the gate quality needed."

## How this surfaced

Phase 0 self-modification PR #6 opened by the deployed agent. CI showed only
`az/lint` + `az/pr` + `gcp/lint` + `gcp/pr` — no AI review, no security
scans. On consumer-repo PRs (e.g. auth-ui PR #66) the agent saw a much
richer pipeline. The asymmetry was invisible to the agent because each PR
opens against a different repo with a different (correct or incorrect)
config.

## Gold standards per language (as of 2026-05-19)

| Language | Gold-standard repo | Pipeline directory |
|---|---|---|
| Python (FastAPI service) | `leartech-ai-classifier` | `.lighthouse/jenkins-x/` |
| Angular UI | `leartech-auth-ui` | `.lighthouse/jenkins-x/` |
| Go service | `leartech-go-service-template` | `.lighthouse/jenkins-x/` |
| Rust service | `leartech-rust-service-template` | `.lighthouse/jenkins-x/` |

Required pipelines for production-grade quality (all languages):

- `pullrequest.yaml` (the `pr` check)
- `lint.yaml`
- `ai-review/pullrequest.yaml` + `ai-review/feedback.yaml`
- `security-scan/pullrequest.yaml` + `security-scan/image-scan.yaml` +
  `security-scan/dynamic/pullrequest.yaml`
- `release.yaml`

Language-specific additions:

- Angular: `test.yaml`, `npm-audit.yaml`, `end2end.yaml`, `end2end-ui.yaml`
- Python: tests run inside `pullrequest.yaml`'s pytest step
- Go: integration tests via `pullrequest.yaml`

## Procedure

After cloning the target repo and BEFORE writing any code:

1. **Detect language** from `pyproject.toml` / `package.json` / `go.mod` /
   `Cargo.toml`.
2. **List existing pipeline source files** in `.lighthouse/jenkins-x/`.
3. **Identify the gold-standard repo** for that language (see table above).
4. **Diff** the gold-standard's `.lighthouse/jenkins-x/` against the target's.
5. **Classify missing files**:
   - **Critical**: ai-review, security-scan, image-scan, dynamic-scan (any of these missing → warn/fail)
   - **Language-specific**: e.g. npm-audit for Angular (missing → warn only)
   - **Optional**: experimental pipelines (note in PR description)

## What to do when gaps are found

| Severity | Action |
|---|---|
| All critical pipelines present | Proceed normally |
| 1 critical pipeline missing | Proceed, but add a `## Pre-flight check` section to the PR description listing what's missing + linking to the gold-standard |
| 2+ critical pipelines missing | Refuse to proceed. Open a separate `chore(lighthouse): align with <lang>-gold-standard` PR first; the original initiative is parked until that lands. Use the parking mechanism (return a structured response: `status: parked, reason: preflight_gap, dependent_pr: <url>`). |

## Why "even if the initiative didn't ask for this check"

The agent's role is to apply leartech-wide conventions when producing code.
The PR-pipeline coverage is part of those conventions — a PR opened without
AI review on a repo where it should run is an incomplete shipment. Pre-flight
catching this saves the human reviewer from noticing the asymmetric CI bar.

## Pairs with structural fixes elsewhere

The "could these BE initiatives?" idea in `~/leartech/Hub/status/cluster-registry-auth.md`
includes an `audit-python-services-using-old-release-task` initiative shape.
This calibration lesson and that initiative compose: the proactive sweep
(initiative) catches gaps centrally; this calibration (per-run) catches gaps
locally when the sweep hasn't run recently.

## Self-aware special case

The agent should specifically verify the bar when working on
`leartech-automated-agent` itself — self-modification needs stricter
scrutiny than consumer-repo work because bad agent changes affect every
future run. The gold-standard for the agent's own repo is
`leartech-ai-classifier`.
