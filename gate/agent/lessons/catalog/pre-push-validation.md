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
| **Go** | See "Go: run the catalog make targets" below — do NOT reproduce bare commands | image-scan, dynamic-scan, end2end |
| **Python** | `ruff format --check <dirs>`, `ruff check <dirs>`, `mypy <dirs>`, `pytest` (or `uv run ...` equivalents) | image-scan, dynamic-scan, end2end |
| **Angular** | `ng lint`, `ng test --watch=false`, `npm audit` | image-scan, dynamic-scan, end2end-ui |
| **Rust** | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test` | image-scan, dynamic-scan, end2end |

The gate pipeline files are the authoritative source. This table is a
fast-path; always confirm against the actual `.lighthouse/jenkins-x/` files
before pushing to a new or unfamiliar repo.

## Go: run the catalog make targets, NOT bare commands

Go consumer repos' `.lighthouse/jenkins-x/*.yaml` files reference the catalog
via opaque `uses:` refs (e.g. `image: uses:mikelear/leartech-pipeline-catalog/tasks/go-lint/pullrequest.yaml@main`).
There are NO local `script:` blocks to extract — the "extract script blocks"
procedure will find nothing and, without this guidance, agents fall back to
bare `gofmt -l . && golangci-lint run && go test ./...`. Those bare commands
DO NOT match the catalog:

- `go-lint` fetches + merges `go/.golangci.base.yml` from the catalog before
  running `golangci-lint`; bare `golangci-lint run` uses only the repo's
  local config and enables far fewer linters.
- `go-test` enforces a **coverage floor and delta** (`-coverpkg`, threshold,
  delta gate); bare `go test ./...` reports nothing about coverage.

This asymmetry is why Go PRs land RED on lint/coverage after the agent
thought it was green (canonical case: `leartech-mcp-servers` PR #49).

**The rule**: for Go repos, do NOT parse `uses:`-ref pipeline files for
scripts. Instead run the **golden make targets** — the same targets the
catalog tasks (and `leartech-go-service-template`'s Makefile) drive from:

```sh
# Preferred — consumer repo owns a Makefile with the standard targets
# (go-service-template ships this; converged Go repos inherit it):
make pre-push                # fmt vet swag-check tidy-check build test lint vuln secrets
# or individually:
make lint                    # fetches + merges catalog .golangci.base.yml, runs golangci-lint
make test-coverage           # runs tests with coverage, enforces the floor

# Fallback A — baked into the leartech-agent-go image
make -f /usr/local/share/leartech-go.mk lint
make -f /usr/local/share/leartech-go.mk test-coverage

# Fallback B — curl the mk from the catalog raw URL when neither the
# consumer Makefile nor the baked copy is present:
curl -fsSL -o /tmp/leartech-go.mk \
  https://raw.githubusercontent.com/mikelear/leartech-pipeline-catalog/main/go/leartech-go.mk
make -f /tmp/leartech-go.mk lint
make -f /tmp/leartech-go.mk test-coverage
```

**Do NOT push until BOTH `lint` and `test-coverage` are green locally.**
Iterate until they are, then push.

**Version matters — single-sourced now.** The `golangci-lint` version in the
consumer Makefile / baked `leartech-go.mk` MUST match the version the
catalog `go-lint` task installs. Running a different major/minor at push
time will produce false negatives (linters that fire in-cluster but not
locally) or false positives (the other way). If the baked image ships a
newer patch than the consumer Makefile pins, prefer the Makefile's pinned
version — it's what the catalog resolves to. When in doubt, `make lint`
in the consumer repo uses its own pin and is safest.

**PR sticky "Pre-push validation" section** — record both:

```
✅ make lint (golangci-lint v2.11.4 via consumer Makefile): passed
✅ make test-coverage (60% floor, +0.0% delta): passed
```

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

This lesson is **Layer 1** of the pre-push validation design, with a
`uses:`-ref carve-out:

- **Layer 1 — script extraction (Python, Angular, Rust, plus any inline
  Go tasks)**: The agent reads the consumer repo's pipeline YAML files at
  push time and extracts commands from `script:` blocks. Simple, zero
  infrastructure.

- **Layer 1 — `uses:`-ref repos (Go today, more languages later)**: When
  a pipeline file's step is an `image: uses:.../catalog/tasks/<task>@<ref>`
  reference with NO local `script:` block, script extraction returns
  nothing useful. For these repos, **run the catalog's golden make
  targets** — see the "Go: run the catalog make targets, NOT bare commands"
  section above. The Makefile is the single source of truth for tool
  versions and gate logic; parsing the catalog's Tekton YAML to
  re-derive commands is fragile and diverges over time.

- **Layer 2 (follow-up if Layer 1 proves brittle)**: An MCP server
  (`mcp__leartech-gate__list_local_runnable_commands`) parses the pipeline
  catalog, resolves `uses:` references, and returns a structured list of
  `{task, command, toolchain, runnable_locally}` objects. The agent calls
  the MCP tool instead of parsing YAML manually. Layer 2 is a separate
  initiative if/when Layer 1 (both variants) proves insufficient.

## See also

- `preflight-target-repo-quality-check.md` — pre-flight check for whether the
  consumer repo's pipeline *configuration* matches the language gold-standard
  (a different concern: "does the repo HAVE the right pipelines?" vs "do the
  pipelines' commands pass locally?").
