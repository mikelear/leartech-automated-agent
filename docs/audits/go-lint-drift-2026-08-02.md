# Go lint/test golden-mk convergence audit — 2026-08-02

Read-only audit of every `mikelear/*` Go repo that uses the leartech-pipeline-catalog
`tasks/go-lint` / `tasks/go-test` uses-tasks, checking whether the repo's `Makefile`
adopts the golden `go/leartech-go.mk` (via `include` or `curl` + `make -f leartech-go.mk lint`/`test-coverage`)
or DRIFTS (bare `golangci-lint run` / bare `go test`).

**Headline**: **only 1 of 20 in-scope repos is converged onto the golden mk** —
everyone else re-implements lint/test in their own Makefile, so laptop-local
`make lint` and the CI catalog task can (and do) drift version-wise.

- Converged: `leartech-mcp-servers` (fetch + `$(MAKE) -f .leartech-go.mk lint/test-coverage`).
- Drifted: 18 repos with bare `golangci-lint run` / bare `go test` in Makefile.
- Neutral: `leartech-go-common` uses the catalog CI tasks but ships no Makefile
  (library — no laptop workflow to drift, so nothing to converge either).

## Scope + methodology

**Source of the golden**: `mikelear/leartech-pipeline-catalog/go/leartech-go.mk` @ `main`.

**In-scope filter**: the repo's `.lighthouse/jenkins-x/lint.yaml` OR `test.yaml`
references `uses:.*mikelear/leartech-pipeline-catalog/tasks/go-lint` or
`tasks/go-test`. Repos wired to the upstream `jenkins-x/jx3-pipeline-catalog/tasks/go`
(pre-catalog) or with no `.lighthouse/` at all are OUT OF SCOPE for this audit —
they need a separate "adopt catalog first" step before mk convergence applies.

**Convergence criterion**: the Makefile either

1. `include leartech-go.mk` at the top-level (one-liner), OR
2. `curl -fsSL $(LEARTECH_GO_MK_URL) -o .leartech-go.mk` in a `fetch-mk` target
   AND its `lint` / `test-coverage` targets shell out with
   `$(MAKE) -f .leartech-go.mk lint` / `test-coverage`.

Anything else — bare `golangci-lint run ./…`, self-bootstrap installers, hand-rolled
coverage floors — is DRIFT.

**Method**: `gh api repos/mikelear/<repo>/contents/<path>` for each of
`.lighthouse/jenkins-x/lint.yaml`, `.lighthouse/jenkins-x/test.yaml`, `Makefile`; grep
for the golden-mk markers and for the bare-tool footprints.

## Result table

| Repo | lint.yaml uses catalog `tasks/go-lint`? | test.yaml uses catalog `tasks/go-test`? | Makefile? | mk converged? | Drift signature |
|---|---|---|---|---|---|
| `leartech-mcp-servers` | ✅ | ✅ | ✅ | ✅ **converged** | `fetch-mk` + `$(MAKE) -f $(LEARTECH_GO_MK) lint/test-coverage` |
| `leartech-go-common` | ✅ | ✅ | ❌ (library) | n/a | no Makefile — nothing to drift |
| `tempo-to-har` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-infra-agent` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-qa-canary` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-sc-event-listener` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-go-service-template` | ✅ | ✅ | ✅ | ❌ drift | self-bootstrap installer + `$$GOLANGCI_BIN run` (see note) |
| `leartech-mortgages-api` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `hello-go7` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-orchestrator-controller` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` + hand-rolled `test-coverage` |
| `leartech-auth-service` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-catalog-mcp` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `webcoder-service` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-forensics-runner` | ✅ | ✅ | ✅ | ❌ drift | hand-rolled `go test` target |
| `leartech-gate` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` + multiple hand-rolled `go test` targets |
| `leartech-maestro-service` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-ai-gateway` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-arrivals-observer` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-mortgages-gw` | ✅ | ✅ | ✅ | ❌ drift | bare `golangci-lint run` |
| `leartech-plan-api` | ✅ (lint only) | ❌ (upstream jx `tasks/go`) | ✅ | ❌ drift + partial-catalog | needs test.yaml migration to catalog first |

### Out of scope — not (yet) using catalog go-lint / go-test

Documented so we don't lose them, but they need a SEPARATE step of adopting the
catalog task first before mk-convergence is even a question:

| Repo | Reason |
|---|---|
| `leartech-prysm` | no `.lighthouse/` (fork of Prysm beacon-chain) |
| `leartech-openapi-generation` | uses upstream `jenkins-x/jx3-pipeline-catalog/tasks/go` |
| `leartech-bus-common` | no `.lighthouse/` (verbatim fork of `mqube-go-common`) |
| `lighthouse` | no `.lighthouse/` (Lighthouse fork) |
| `leartech-podcast-feed` | uses upstream `jenkins-x/jx3-pipeline-catalog/tasks/go` |
| `leartech-soc-collector` | no `.lighthouse/` |

## Note on the golden template

`leartech-go-service-template` is the **repo the rest of the fleet clones from**, and
its own Makefile DRIFTS — it self-bootstraps `golangci-lint` at
`GOLANGCI_LINT_VERSION := v2.11.4` and runs `$$GOLANGCI_BIN run --config .golangci.merged.yml`
directly. Every service scaffolded from the template inherits this drift.

The golden `leartech-go.mk` pins `GOLANGCI_VERSION ?= 2.12.2`; the template Makefile
pins `v2.11.4`. Two different pins, same fleet — this is exactly the drift the
golden mk is meant to prevent.

**The template Makefile should be the FIRST conversion**, so subsequent scaffolds land
converged by default. Everything else is downstream of that.

## Recommended follow-up initiatives

One initiative per repo below, ordered by leverage — template first, then busy
services, then quiet ones. Each is a small change: fetch-mk target, delegate
`lint` and `test-coverage` to `make -f .leartech-go.mk`, delete the hand-rolled
bootstrap. Preserve any repo-specific extras (integration tests, mock generation)
as adjacent targets that don't shadow the golden ones.

### Tier 1 — golden template (unblocks the fleet)

1. `leartech-go-service-template`

### Tier 2 — active services (highest drift-risk)

2. `leartech-orchestrator-controller` — has hand-rolled `test-coverage` (drift on the coverage floor logic itself)
3. `leartech-gate` — has multiple `go test` invocations, easiest to break
4. `leartech-forensics-runner` — hand-rolled `go test`
5. `leartech-auth-service`
6. `leartech-mortgages-api`
7. `leartech-mortgages-gw`
8. `leartech-maestro-service`
9. `leartech-ai-gateway`
10. `leartech-catalog-mcp`
11. `webcoder-service`

### Tier 3 — supporting services + demos

12. `leartech-infra-agent`
13. `leartech-arrivals-observer`
14. `leartech-sc-event-listener`
15. `leartech-qa-canary`
16. `tempo-to-har`
17. `hello-go7`

### Tier 4 — pre-catalog repos (catalog adoption first)

18. `leartech-plan-api` — migrate test.yaml to `tasks/go-test` first, then converge Makefile
19. `leartech-openapi-generation` — migrate lint+test.yaml to catalog, then converge Makefile
20. `leartech-podcast-feed` — migrate lint+test.yaml to catalog, then converge Makefile

### Not applicable

- `leartech-go-common` — library, no Makefile; consider whether it needs a local
  smoke `make lint` shim purely for developer ergonomics. Not urgent.
- `leartech-prysm`, `leartech-bus-common`, `lighthouse`, `leartech-soc-collector` —
  forks / no `.lighthouse/`. Out of scope unless product needs push them into the
  fleet CI.

## Reproducing the audit

```bash
# 1. Enumerate mikelear Go repos
gh api "search/repositories?q=user:mikelear+language:Go&per_page=100" \
  --jq '.items[].name'

# 2. For each repo, check the three files
for repo in <list>; do
  gh api "repos/mikelear/$repo/contents/.lighthouse/jenkins-x/lint.yaml" \
    --jq '.content' | base64 -d | grep -E 'uses:.*tasks/go-lint'
  gh api "repos/mikelear/$repo/contents/.lighthouse/jenkins-x/test.yaml" \
    --jq '.content' | base64 -d | grep -E 'uses:.*tasks/go-test'
  gh api "repos/mikelear/$repo/contents/Makefile" \
    --jq '.content' | base64 -d | grep -E 'leartech-go\.mk|golangci-lint run|go test'
done
```

Raw per-repo captures were held in `/tmp/audit/data/<repo>/` during the audit
run; they are not committed here (recomputable via the script above).

## What this audit does NOT do

- Does not touch any of the drifted repos.
- Does not change the golden mk itself.
- Does not open PRs against downstream repos — those are the follow-up initiatives
  listed above, one per repo, sequenced by tier.
