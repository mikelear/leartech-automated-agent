# leartech-automated-agent

Criteria-driven agent runner for leartech, deployed as a long-running FastAPI service. Mirrors the [leartech-ai-classifier](https://github.com/mikelear/leartech-ai-classifier) shape — same JX3 pipeline, same secret refs, same operational profile.

## What this is

A platform that:

- Encodes "is this PR review-ready?" as pytest gates
- Drives initiative-style PR work via Claude Agent SDK
- Exposes the same primitives as MCP servers for cross-agent reuse
- Learns from each run via a calibration lessons catalog

The service is callable from three trigger surfaces, all routing to the same `POST /initiatives` endpoint:

1. **CRD + controller** (production) — webCoder dashboard creates `kind: AgentInitiative`; controller spawns Job; Job invokes the service.
2. **Tekton chatops task** (slice E evolved) — `/agent run <name>` PR comment fires a thin Tekton task that calls the service.
3. **Direct HTTP** (testing/debug) — `curl POST /initiatives`, no orchestration layer.

Cluster-side resource diagnosis (capacity, Pending pods, etc.) lives in the runner Job (surface #1), **not** in this service. The agent service stays small and trusts the verdict it was invoked with.

## Local development

No cluster, no kubectl, no service deployment required for development:

```sh
make setup     # install uv + dependencies
make all       # fmt + lint + test

# HTTP service mode:
make serve     # FastAPI on :8080
make api-test  # smoke-test /health

# Direct CLI mode (no service required):
make initiative INITIATIVE=<name>   # write-mode initiative
make agent REPO=<repo> PR=<n>       # read-only review
make gate REPO=<repo> PR=<n>        # criteria gate
make lessons-list                   # browse catalog
```

The CLI commands operate against the existing `gate/` package directly, with no HTTP layer involved. This is the testing/dev surface — same code path as the deployed service, no orchestration overhead.

## Layout

```
.
├── app/                       FastAPI service code
│   ├── main.py                FastAPI app + router mount
│   └── routers/
│       ├── health.py          /health, /healthz, /readyz
│       ├── initiatives.py     /initiatives* (POST, GET, cancel)
│       └── lessons.py         /lessons* (list, capture)
├── gate/                      Criteria + agent + MCP servers (substrate)
│   ├── tools/                 typed primitives over gh, kubectl, gcs
│   ├── criteria/              pytest-driven gates (shared + per_repo)
│   ├── mcp_servers/           in-process MCP wrappers
│   └── agent/                 Claude Agent SDK orchestration + lessons catalog
├── charts/leartech-automated-agent/   Helm chart for cluster deployment
├── .lighthouse/jenkins-x/     JX3 PR + release pipeline
├── tests/
├── Dockerfile
├── Makefile                   local development surface
├── pyproject.toml
└── README.md
```

## Anthropic API key

The service expects `ANTHROPIC_API_KEY` in env. In cluster deployments, this is mounted from the existing `ai-review-api-keys` Secret (already provisioned for ai-review's use). Locally:

```sh
# Add to ~/.zshrc once:
leartech-claude-key() {
  ANTHROPIC_API_KEY=$(kubectl --context=gke_product-first_us-east1-b_tf-jx-usable-bird \
    -n ai-inference get secret ai-review-api-keys \
    -o jsonpath='{.data.CLAUDE_API_KEY}' | base64 -d) \
    && export ANTHROPIC_API_KEY \
    && echo "ANTHROPIC_API_KEY set (${ANTHROPIC_API_KEY:0:12}...)"
}
```

## Status

**Phase B v1 — service scaffolding.** Repository structure mirrors leartech-ai-classifier; FastAPI scaffold + routers in place; substantive endpoint wiring (initiative runtime, lessons catalog) lands in v1.5. CLI commands work today via `make initiative` etc.

See `memory/project_service_deploy_phase_b.md` (in the parent automated-agent's memory directory) for the full plan.
