# Design: record every Plan run, on the PR and in git

Status: proposed, not implemented. Delete this file when the work lands.

## Why

Three Plan runs on 2026-08-19 and 2026-08-28 surfaced eight defects between them: two
prompt instructions naming tools that had been deleted, a `pr` vs `pr_number` argument
error, then the same argument passed as a string instead of an integer, a Go toolchain
cache the agent user cannot write, no git identity in the agent image, a doubled
phase-transition audit line, and a release that failed on one cluster while saying nothing
to its PR.

Every one came from reading Loki by hand and comparing runs. None of that analysis is
recorded: it exists in one session's transcript and two memory files. The next person — or
the next model — starts from nothing, and Loki forgets after 720h.

This is not a test framework. It is the analysis we already do, done automatically and
kept.

## What already exists

- **Correlated trace data.** A five-source join on `run_id` works today: agent Job,
  orchestrator-controller, maestro-service, mcp-servers, plan-api.
- **Loki query tools.** `internal/lokiserver` is mounted in leartech-mcp-servers with
  `reconstruct_run`, `tool_calls` and `service_errors`.
- **A step-appending mechanism.** plan-api already appends the `verify-release-flow` steps
  to every Plan; a recorder step uses the same path.
- **A PR write path.** `prcontextserver` posts and patches; `post_pr_handoff` already
  maintains a sticky comment.
- **A hashable prompt artefact.** `docs/agent-system-prompt.md` is committed, regenerated
  by `scripts/render_system_prompt.py`, and contract-tested.

## What is missing

1. **Aggregation.** The Loki tools return lines, not metrics. Turning 322 lines into
   {turns, tool histogram, errors, wait-tool ratio} is the work — roughly 150 lines,
   belonging in `lokiserver` as a `run_report` tool beside the existing three.
2. **A trigger.** A final Plan step, appended by plan-api, depending on every other step.
3. **Sinks.** See below.

## Two components, split by cadence

**Sidecar — per Plan, deterministic, no LLM.** Computes the record, emits the structured
event, commits the file, posts the sticky comment. This is the delivery path and it must be
boring.

**Analyst — nightly, one LLM call through the ai-gateway on its own virtual key.** Reads
the committed records, writes the narrative, and may post back to individual PRs. PRs accept
comments indefinitely, so a next-morning comment still lands where the next agent will read
it.

Nightly rather than per-run, for four reasons:

- **Cross-run patterns are the valuable output.** "This tool error recurred five nights" is
  an image defect; one occurrence is noise. A per-run narrator structurally cannot see it —
  the unwritable GOMODCACHE looked like a one-off until the same workaround turned up in the
  previous run's Bash calls.
- **A bad prompt night costs a report, not a run.**
- **Cheaper**: one call over N records instead of N calls.
- **It produces the existing house format** — `docs/audits/<name>-<date>.md`, of which
  `go-lint-drift-2026-08-02.md` is the precedent.

So the analyst is not a second sidecar. It is the nightly pass, promoted to a component.

### Detection is deterministic; the analyst only explains

The rule that keeps this honest. Anomaly DETECTION lives in the sidecar as rules — "MCP
calls happened and mcp-servers logged zero lines" is a rule, not a judgement. The analyst is
given the metrics, the fired rules and the verbatim errors, and asked to explain them and to
state what remained unknowable.

If the LLM were finding the problems, a quiet night would be ambiguous: "all good" or "did
not notice". With rules finding and the model explaining, a quiet night means the rules did
not fire, which is a fact. It also bounds the damage from a bad analyst prompt to a poor
write-up over a complete, correct record.

### What the analyst is actually for

The most useful output from analysing these runs by hand was not a metric. It was sentences
like: *"could not establish what each wait call returned from the agent logs — status was
past the 2000-char clip — so the MCP server's own logs were read instead."* That sentence IS
the bug report that became the tool-result verdict fix.

So the analyst's job is a **"what could not be established, and why"** section. That is the
observability backlog, written by the thing that just hit the gap, and it is the part that
resists being computed.

### Analyst inputs and discipline

- **Input is the committed JSON, not the sticky comment.** Comments are prose-shaped and get
  edited; the JSON is structured and outlives Loki retention.
- **Context is the record, not raw logs.** Feeding hundreds of log lines is expensive and
  worse, because the model then re-derives what the aggregator computed reliably.
- **Summarisation, not reasoning** — a Haiku-class model is right, which makes the cost
  negligible even nightly.
- **Its own prompt is version-controlled and hashed**, exactly like
  `docs/agent-system-prompt.md`. Otherwise drift is built into the thing whose job is
  detecting drift — and this session proved how quietly that happens: the agent prompt named
  three deleted tools and a developer's home directory, and nothing failed.
- **If the gateway call fails, the deterministic record still stands** and the narrative is
  recorded as absent. Never retro-fit a missing narrative by guessing.

## Three sinks, three jobs

| Sink | Purpose | Lifetime |
|---|---|---|
| PR sticky comment | narrative for a human and for the next agent; links the prompt and the JSON | forever |
| Structured log event | queryable trends and anomaly detection via LogQL | 720h |
| Committed file in `docs/agent-runs/` | durable corpus for prompt engineering | forever |

The committed file is what survives long enough to answer "did prompt v18 behave better
than v14". It lives in THIS repo deliberately: the prompt sources are here, so `git log`
correlates a prompt change with its measured effect. Accept that per-run commits add noise
to the repo history — `git log docs/agent-system-prompt.md` stays clean because it is a
different path, and that is the history that matters.

Where there is no PR — a check-only Plan, or a run that dies before `open_pr` — the log
event and the committed file still exist. The PR is the nicest sink, never the only one.

## The record

Generated, never hand-edited: `docs/agent-runs/<utc-date>-<plan-name>.json` carrying the
data, and `.md` carrying the same data rendered for a human, both from one source so they
cannot drift.

**The JSON is authoritative; any narrative is commentary.** The sidecar's md is a rendering
of the JSON. The analyst's prose is added later, must cite fields present in the JSON, and
is labelled generated. A committed record asserting "the release failed because of X" when
it did not is worse than no record.

Fields that matter, and why each is there rather than for completeness:

- **`run_id`, plan, step, cluster** — the join key and where it ran.
- **Prompt identity: sha256 of `docs/agent-system-prompt.md`, plus a permalink to it at
  the commit the agent image was built from.** Derivable: image tag `0.50.12` → git tag
  `v0.50.12-{az,gcp}` → commit → `blob/<sha>/docs/agent-system-prompt.md`. Without this a
  metrics change cannot be attributed to a prompt change.
- **Agent image tag.**
- **MCP inventory: which servers the host advertised and which tools were allowed.** Add
  an MCP and the same prompt yields a different trajectory; without the inventory, older
  records become uninterpretable.
- **Line counts per source** (`{agent: 322, controller: 63, maestro: 54, mcp: 9,
  plan-api: 2}`). This is the field that detects observability MISSES: a run with MCP tool
  calls but zero mcp-servers lines is a logging gap, found automatically.
- **Tool histogram**, and the **wait-tool ratio** specifically — fail-fast versus
  full-terminal. That ratio moved from 2:7 to 8:1 after one prompt change, which is the
  clearest evidence a prompt edit worked that we have.
- **Every tool error verbatim.** All four image and prompt defects above came from these.
  Never summarise them.
- **Turn count, wall-clock, MCP call count.**

## Ordering

Analyse → commit the record to `main` → post the PR comment carrying both links. The
commit must come first so the comment can carry a permalink.

Tide merging the PR mid-write is not a problem: GitHub accepts comments on merged PRs. What
would break is writing to the PR BRANCH, which jx deletes after merge — so the recorder
must never push to the branch, only comment, and commit to `main` of this repo.

## The recorder must never fail a Plan

If aggregation, the commit or the comment fails, the Plan's verdict is unaffected. A
recorder that can fail delivery has inverted its own purpose. Log the failure and exit
zero.

## The rules the analyst is handed

Deterministic, evaluated by the sidecar, and the input to every narrative. The valuable
output is ABSENCES and repetition, not drifting metrics:

- a run with MCP calls but no mcp-servers log lines
- **a phase change with no matching `phase_transition` line** — `phase_transition_logging.go`
  states this invariant explicitly and nothing checks it today
- the same tool-error class recurring across nights, which is how an image defect like the
  unwritable `GOMODCACHE` shows up as a pattern rather than a one-off
- steps whose logs are unreachable after `gcpods` reaps the pod
  (see the Tekton logging review)

## What this deliberately is not

Not a fixture-based eval. Metrics across arbitrary Plans are not comparable — a one-line
chart deletion and a 440-line Go change differ in every dimension. This design buys
anomaly detection, which is where all eight findings came from. True A/B comparison needs a
repo pinned at a known commit and re-run with `hold: true`; that is a later phase and not a
prerequisite.

## Open question

Who writes the committed file. A cluster job needs a git token with write access to this
repo — the repo holding the prompts. `tekton-git` exists, but granting the recorder commit
rights here is the one part of this that adds real permission surface rather than reusing
what is already built. Decide it deliberately.
