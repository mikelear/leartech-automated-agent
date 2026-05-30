---
id: cnpg-database-cr-does-not-guarantee-database-exists
title: CNPG Database CR existence does not guarantee the actual Postgres database exists — verify with psql before declaring complete
applies_to:
  - initiative_agent
  - orchestrator_agent
status: open
captured_at: 2026-05-30T10:15:00Z
source:
  type: agent_run
  reference: leartech-orchestrator init #4 cascade — pod stuck in initContainer waiting for database that didn't exist
  observer: mike.lear@leartech
  latency_to_capture: minutes
category: shared_infra_race
slipped_past_criteria: []
proposed_criterion: |
  When an initiative adds or modifies a CNPG `Database` CR (via chart
  cascade or direct apply), the agent's post-cascade verification must
  include an actual psql connection probe against the deployed DSN. If
  the probe returns `FATAL: database "<name>" does not exist`, the agent
  must recover by running `CREATE DATABASE` on the cluster's PRIMARY
  pod (label `cnpg.io/instanceRole=primary`, NOT hardcoded -1).

  Concrete enforcement: a post-cascade verification step in any plan
  that includes a service-bootstrap initiative. Uses the debug-pod
  pattern: `kubectl run --rm --image=postgres:17 -- psql "$DSN" -c 'SELECT 1'`.
---

## The race

The chart creates a CNPG `Database` CR (`<svc>-database` in
`jx-staging`). The CNPG operator SHOULD provision the underlying
Postgres database on the shared cluster — but sometimes doesn't:

- Database CR exists, status `APPLIED` is empty
- Database does not actually exist on the Postgres cluster
- Pod's `migrations` initContainer spins on `psql "$DSN" -c 'SELECT 1'`
- The probe-loop suppresses stderr, so the only visible signal is
  `waiting for database (try N/60)` — the SAME message that appears for
  ordinary transient connection issues

After 60 tries the pod restarts the initContainer, which spins again.
The old pod (if any) keeps serving `/healthz` (no DB needed for the
health endpoint), masking the fact that the new pod can't start.

## The recovery (memorize the exact commands)

```sh
# 1. Find the PRIMARY cnpg pod (NOT hardcoded leartech-staging-1)
PRIMARY=$(kubectl --context=<ctx> -n cnpg-system get pods \
  -l cnpg.io/cluster=leartech-staging,cnpg.io/instanceRole=primary \
  -o jsonpath='{.items[0].metadata.name}')

# 2. Create the database with the correct owner role
kubectl --context=<ctx> -n cnpg-system exec -c postgres $PRIMARY -- \
  psql -U postgres -c "CREATE DATABASE leartech_<svc> OWNER leartech_<svc>;"

# 3. Verify
kubectl --context=<ctx> -n cnpg-system exec -c postgres $PRIMARY -- \
  psql -U postgres -tAc "SELECT datname FROM pg_database WHERE datname='leartech_<svc>'"
```

Pod's initContainer picks up the now-existing database within 60s.

## Why SQLite tests don't catch this

Tests use `aiosqlite` in-memory — schema is created per-test from the
Python model definitions, so missing-database is impossible by
construction. The class of bug is "Postgres physical-database
provisioning race", which has no analog in single-file SQLite. Gates
pass, deploy still fails.

## The bug that surfaced this lesson

`leartech-orchestrator` init #4 (PR #6, merged 2026-05-30):
- Per-cluster helmfile config cascaded (`postgresql.enabled=true`)
- CNPG Database CR `leartech-orchestrator-database` appeared in
  `jx-staging` on both clusters
- BUT `psql FATAL: database "leartech_orchestrator" does not exist`
  on both clusters
- New pod stuck Pending — initContainer at try 57/60, then restart, etc.
- Old pod still Running (no DB needed for `/healthz`)
- AZ recovery: `CREATE DATABASE` on `leartech-staging-1` (which
  happened to be primary)
- GCP recovery: same command FAILED — `leartech-staging-1` was the
  read-only REPLICA. Had to find primary via the label selector

## Cross-agent application

| Agent | Where to wire this |
|---|---|
| `initiative_agent` | Post-cascade verification for any chart change touching `Database` CRs must include a real psql connection probe. Pre-push: don't declare "deploy succeeded" until verified |
| `orchestrator_agent` | When a plan includes a service-bootstrap initiative, post-cascade verification step is mandatory. Record outcome in `plan_decisions` with `kind=db_provisioning_verified` |
| `infrastructure_agent` (future) | Prime auto-remediation target. Watch for new Database CRs; verify the database actually exists; auto-recover if not. Log: "recovery action taken: CREATE DATABASE" |

## Related lessons

- `cnpg-bootstrap-workarounds` (calibration memory) — the earlier
  observation of the same race on automated-agent's bootstrap
- `every-initiative-extends-the-e2e-script` — the broader principle of
  not declaring complete until reality is verified
