---
id: sqlite-tests-miss-shared-postgres-races
title: SQLite-in-memory tests cover schema logic but miss CNPG/shared-Postgres runtime races — gates green AND production broken is a real outcome
applies_to:
  - initiative_agent
  - orchestrator_agent
status: open
captured_at: 2026-05-30T10:30:00Z
source:
  type: agent_run
  reference: leartech-orchestrator init #4 cascade — schema correct, runtime broken
  observer: mike.lear@leartech
  latency_to_capture: minutes
category: criteria_gap
slipped_past_criteria: []
proposed_criterion: |
  When initiative_agent uses aiosqlite for tests against code that runs
  against shared CNPG in production, the agent must NOT assume green
  tests imply green deploy. Post-cascade verification — actual psql
  probe against the deployed DSN — is mandatory.

  Concrete enforcement: a deployment health-check criterion that runs
  AFTER chart cascade, hitting an endpoint that touches the DB via the
  real connection path. If that fails, the initiative isn't "complete";
  it's "deployed but unverified".
---

## The principle

When a service uses shared CNPG Postgres in production but `aiosqlite`
for local tests, there's a class of bug that:

- Tests pass (schema logic correct in SQLite)
- Code review passes (logic is right)
- All PR gates go green (no static check covers it)
- Preview deploy works **if it doesn't actually touch the DB**
- Production cascade fails because of a shared-cluster provisioning race

This is a test-environment gap, not a logic gap.

## What SQLite doesn't model

SQLite is a single-file local DB. Tables created per-test from the
Python model. "Database exists" is implicit. SQLite has no equivalent of:

- CNPG operator races (Database CR vs actual database creation)
- Role password reconcile delay  
- Shared-cluster connection limits
- Per-cluster network policy + DSN host resolution
- sslmode requirements
- Per-cluster CNPG primary leadership rotation
- Postgres-specific syntax (JSONB, RETURNING, partial indexes)

These are runtime-environment properties, not code properties. Tests
can't catch them by inspecting code.

## The honest response (not "ship slimmer gates")

The fix is NOT to drop unit tests or weaken gates — they were honest;
they just don't cover this class. The fix is:

1. **Real-DB integration test in preview deploy** is the durable fix.
   Preview deploy already happens for every PR. Adding a
   `wait_for_preview_pod_ready` + `curl /healthz_db` step (or similar
   endpoint that touches the DB) to the PR-time chain would catch this
   class.

2. **Post-cascade verification step** for NEW services — phase D of the
   new-service-bootstrap pattern needs a real-DB connectivity probe,
   not just "kubectl get pods returns Running".

3. **Debug-pod verification** during initiative execution — when an
   initiative cascades a DB-touching change, the agent runs the
   debug-pod probe before declaring complete (per the
   debug-pod-beats-port-forward calibration).

## The bug that surfaced this lesson

`leartech-orchestrator` init #4 (PR #6, merged 2026-05-30):

- All 10 PR-time checks GREEN on both clusters
- Image built + published
- Chart cascade landed
- Database CR created
- BUT actual database `leartech_orchestrator` had never been created
  (the CNPG Database CR vs actual database race)
- Pod stuck in initContainer; old pod still serving `/healthz`
- Tests had no way to catch this — SQLite always has the database

## Cross-agent application

| Agent | Where to wire this |
|---|---|
| `initiative_agent` | When firing a service-bootstrap or DB-touching initiative, defer "complete" until post-cascade real-DB probe succeeds. Pre-push test: bring up a docker postgres in CI, run the migrations + a smoke query |
| `orchestrator_agent` | When running a multi-PR plan that includes a service-bootstrap, include a post-merge VERIFICATION step that hits a real-DB endpoint after cascade. Failed verification = `kind=post_deploy_verification_failed` decision |
| `infrastructure_agent` (future) | The auto-remediation layer for the CNPG-Database-CR race specifically |

## Related lessons

- `cnpg-database-cr-does-not-guarantee-database-exists` — the specific
  recovery recipe for the most common race
- `every-initiative-extends-the-e2e-script` — the broader test-pyramid
  principle this lesson lives within
- `consult-reference-cluster-before-iterating` — an opposite failure
  mode (asymmetric flake) — both lessons inform when to trust vs
  question a green/red signal
