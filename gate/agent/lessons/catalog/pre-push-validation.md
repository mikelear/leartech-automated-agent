---
id: pre-push-validation
title: Before every git push, run locally-available gate equivalents in the consumer repo
captured_at: 2026-05-25T00:00:00Z
source:
  type: manual_review
  reference: automated-agent-consume-agent-go
  observer: mike@leartech
  latency_to_capture: immediate
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/pre-push-validation.md
encoded_at: 2026-05-25T00:00:00Z
---

BEFORE every `git push` on a working branch, scan the consumer repo's
`.lighthouse/jenkins-x/*/pullrequest.yaml` (and `lint.yaml` if present).
For each gate task, extract the commands inside its `script:` blocks — these
are embedded shell. For each command whose toolchain is locally available
(`command -v <tool>` succeeds), run it in the consumer repo's cwd. If any
command fails, **do NOT push** — fix the issue and retry. If a toolchain is
missing (`command -v` fails), note it in the sticky comment as
"gate `<task>` couldn't be pre-validated (no `<tool>` in image)" and proceed.

Do NOT try to install missing tools — that is a separate concern (extending
the base image). The lesson's goal is fast-fail on errors detectable locally,
not 100% gate parity.

## Language-specific quick-reference

Rather than parsing the pipeline files from scratch each time, use this
mapping (updated as new repos come online):

| Language | Locally-runnable pre-push checks | Skip (needs cluster) |
|---|---|---|
| **Go** | `gofmt -l .` (fail if output non-empty), `golangci-lint run`, `go test ./...`, `govulncheck ./...` if present | image-scan, dynamic-scan, end2end |
| **Python** | `ruff format --check <dirs>`, `ruff check <dirs>`, `mypy <dirs>`, `pytest` (or `uv run ...` equivalents) | image-scan, dynamic-scan, end2end |
| **Angular** | `ng lint`, `ng test --watch=false`, `npm audit` | image-scan, dynamic-scan, end2end-ui |
| **Rust** | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` | image-scan, dynamic-scan, end2end |

The gate pipeline files are the authoritative source. This table is a
fast-path; always confirm against the actual `.lighthouse/jenkins-x/` files
before pushing to a new or unfamiliar repo.

## Checks to always skip pre-push

Some gate tasks require cluster infrastructure and must NOT be attempted
locally:

- `*image-scan*` — Trivy/Grype run against the published container image;
  image doesn't exist until kaniko builds it in-cluster.
- `*dynamic-scan*` — DAST tools run against the live preview deploy.
- `*ai-review*` — Lighthouse AI review pipeline.
- `*end2end*` / `*end2end-ui*` — Playwright suites that run against the
  preview environment.

Pre-push validation is about **fast-fail on local-runnable commands**, not
reproducing the full gate.

## Procedure

```
1. Detect consumer repo language:
     pyproject.toml → Python
     package.json   → Angular (or Node)
     go.mod         → Go
     Cargo.toml     → Rust

2. For each .lighthouse/jenkins-x/*pullrequest.yaml and lint.yaml:
     a. Extract the `script:` blocks from Tekton step specs.
     b. For each command in those blocks:
          - Check `command -v <tool>` in current shell.
          - If available: run it. On non-zero exit → STOP, fix, retry.
          - If missing:   record "gate <task>: no <tool>" for sticky comment.

3. If all available checks pass → push.

4. In the PR sticky comment, add a "Pre-push validation" section:
     ✅ ruff format --check: passed
     ✅ ruff check: passed
     ✅ mypy: passed
     ⚠️  govulncheck: not available in image (noted, not a blocker)
```

## Dogfooding on this repo

`leartech-automated-agent` is a Python service. Its gate runs:
- `ruff format --check app gate tests` (lint.yaml)
- `ruff check app gate tests` (lint.yaml)
- `mypy app gate` (lint.yaml)
- `uv run coverage run -m pytest -v` (pullrequest.yaml)

With `uv` available in the agent image, all of these are locally runnable via
`uv run ruff ...` / `uv run mypy ...` / `uv run pytest`. Run them before
pushing any self-modification PR.

## Layer 1 vs Layer 2

This lesson is **Layer 1** of the pre-push validation design:

- **Layer 1 (this lesson)**: The agent reads the consumer repo's actual
  pipeline YAML files at push time and extracts commands from `script:` blocks.
  Simple, zero infrastructure, brittle only if pipeline scripts are very
  complex (multi-step pipelines with uses: references, templating, etc.).

- **Layer 2 (follow-up if Layer 1 proves brittle)**: An MCP server
  (`mcp__leartech-gate__list_local_runnable_commands`) parses the pipeline
  catalog, resolves `uses:` references, and returns a structured list of
  `{task, command, toolchain, runnable_locally}` objects. The agent calls
  the MCP tool instead of parsing YAML manually. Layer 2 is a separate
  initiative if/when Layer 1 proves insufficient.

## See also

- `preflight-target-repo-quality-check.md` — pre-flight check for whether the
  consumer repo's pipeline *configuration* matches the language gold-standard
  (a different concern: "does the repo HAVE the right pipelines?" vs "do the
  pipelines' commands pass locally?").
