---
id: every-initiative-extends-the-e2e-script
title: Green PR is a meaningful signal only when the agent has extended the e2e/run script with exercises for the new behavior — same delivery contract as unit tests
applies_to:
  - initiative_agent
  - orchestrator_agent
status: open
captured_at: 2026-05-30T14:30:00Z
source:
  type: agent_run
  reference: leartech-orchestrator init #5 PR #7 → CrashLoop on init #5 cascade, fixed by PR #10
  observer: mike.lear@leartech
  latency_to_capture: minutes
category: criteria_gap
slipped_past_criteria: []
proposed_criterion: |
  When an initiative authors code that adds new behavior (endpoints, modules,
  runtime config, Dockerfile changes), the agent's pre-push checks must
  include extending `scripts/e2e.sh` (or equivalent) with an exercise that
  hits the new surface against the BUILT container. If the repo has no e2e
  script yet, the agent's first action is to add one. Same delivery
  contract as unit tests.

  Concrete enforcement: gate criterion that, when PR diff touches
  `Dockerfile`, `pyproject.toml`, or new top-level dirs, requires a
  corresponding diff in `scripts/e2e.sh` or `.lighthouse/jenkins-x/e2e.yaml`.
  If missing, agent's retrospect must surface as a `deferred_followup`
  finding (not a `preventable` one — author either does it or explains
  why).
---

## The principle (Mike's framing 2026-05-30)

> When the agent writes/changes code, local tests pass. Good. But the
> agent must also ask: "have I introduced something — a new endpoint,
> a new module, a Dockerfile change — that the kaniko build alone won't
> verify?" The PR pipeline runs an e2e script. The agent can SEE that
> script. The agent should EXTEND that script with new exercises for
> the new behavior, just like it extends the unit tests. A green PR is
> only a meaningful signal if the e2e covers the new code path.
> Otherwise green just means "the old code still works" — necessary,
> not sufficient.

## Why this matters — concrete pattern

Source-tree pytest + kaniko-build-smoke is NOT the same as running the
built image. Running the wheel-installed code against the real env
catches an entirely different class of bug:

| Catches | Doesn't catch |
|---|---|
| Unit tests | logic bugs in the new code |
| Kaniko smoke | Dockerfile that doesn't build |
| **e2e** | **Dockerfile builds but image is missing a file. Env var not plumbed through. Lifespan import errors. Endpoint registered at wrong path. Config-vs-code drift.** |

## What this looks like as a deliverable

For EVERY initiative the agent runs, the deliverable list now includes:

| What the initiative adds | Unit tests | e2e script extension |
|---|---|---|
| New endpoint (`POST /plans`) | exercise the handler in-process (TestClient) | hit the endpoint against the running container, assert real response |
| New module imported at startup | import + behaviour tests | start the container, assert it doesn't crash + endpoint that exercises it works |
| New env var consumed | parse + default tests | run container with new env set, assert behaviour reflects it |
| New schema migration | DB-shape tests | run container against fresh DB, exercise the new column/table via API |
| Dockerfile change | n/a (no source-tree analog) | re-run e2e — must still pass |

If repo has no e2e script: agent's first action is to add a minimal one
(`scripts/e2e.sh` + `Makefile` target + a presubmit in
`.lighthouse/jenkins-x/`). Subsequent initiatives extend it.

## The bug that surfaced this lesson

`leartech-orchestrator` init #5 (PR #7, merged 2026-05-30) added
`gate/orch/{job_runner,job_reconciler,plan_runner}.py` and listed `gate`
in `pyproject.toml`'s wheel packages — but the `Dockerfile` only
`COPY`d `app/`. Wheel built without `gate/`. All 10 PR-time gates
passed (`az/pr` ran pytest successfully because TestClient exercises
the never-reached lifespan branch). Image deployed, pod CrashLooped
with `ModuleNotFoundError: No module named 'gate.orch'`.

No source-tree test could have caught it. An e2e script that builds
and RUNS the image — even just `docker run --rm <image> --help` —
would have failed immediately. Fixed properly in PR #10 by also adding
`scripts/e2e.sh` (the deliverable that should have shipped with init #5).

## Routing (per cross-agent-retrospect-routing)

When this lesson's principle is violated, the retrospect finding should
be tagged:

```yaml
category: preventable     # agent KNEW or could have known
target_agent: initiative_agent
suggested_form: pre-push-check
```

If the agent CAN'T ship the e2e step (e.g. no docker in sandbox to
validate dind config), then:

```yaml
category: deferred_followup
target_agent: initiative_agent
suggested_form: initiative
reason_deferred: "<concrete reason>"
```

## Related lessons

- `cross-tier-failure-asymmetry-is-diagnostic-signal` — when e2e-ui
  fails but e2e passes, that asymmetry IS the signal; this lesson is
  about ensuring e2e even exists to fail in the first place
- `e2e-validation-pr37` — concrete worked example of e2e validation
  catching a real bug
