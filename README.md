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

### Testing the MCP tools locally (no release cycle)

The agent calls the real Go MCP platform (`leartech-mcp-servers` — `pr_context`,
`tekton`, `k8s`, `platform_state`, …) via an authed remote client
(`gate/mcp_servers/remote.py`). To exercise those tools **without** waiting for a
full build/release cycle, run the MCP server locally with auth off and drive it
with the bundled mini client:

```sh
# 1. Run the real MCP platform locally (from a leartech-mcp-servers checkout):
AUTH_REQUIRED=false MCP_DEPLOYMENT_MODE=internal PORT=8899 go run ./cmd/server

# 2. From this repo, list every tool on every mounted server:
python3 scripts/mcp_test_client.py --base http://localhost:8899 --list

# 3. Invoke a tool (JSON-RPC tools/call round-trip):
python3 scripts/mcp_test_client.py --base http://localhost:8899 \
    --call tekton step_status '{"pipelinerun_name":"...","cluster":"gcp"}'
```

To test against the **deployed** internal MCP instead, port-forward the service
and pass a bearer token (audience `leartech-mcp`):

```sh
kubectl -n jx-staging port-forward svc/leartech-mcp-servers 8899:80 &
python3 scripts/mcp_test_client.py --base http://localhost:8899 --token "$TOKEN" --list
```

The client speaks raw JSON-RPC over the go-sdk Streamable-HTTP transport
(stateless — no `initialize` handshake), so it has no dependencies beyond the
Python stdlib. See `scripts/mcp_test_client.py`.

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

## Operator CLI — `leartech-agent`

The `leartech-agent` console_script is the operator-facing surface for the deployed service. It can be installed once, globally, without cloning the repo and pointed at any cluster's orchestrator + agent ingress via flag, env, or per-user config.

### Install (no repo clone)

The fastest path is `pipx` against the GitHub HEAD:

```sh
pipx install git+https://github.com/mikelear/leartech-automated-agent.git@main
leartech-agent --version
leartech-agent health    # hits the default cluster (gcp-staging)
```

Upgrade later with `pipx upgrade leartech-automated-agent`.

A PyPI/private-index release (`leartech-agent` package) is planned but not required for the current operator workflow — pipx + GitHub HEAD is the supported entry point today.

### Point at a different cluster

The CLI resolves URLs (orchestrator + agent) in this priority order:

1. Explicit `--orch-url` / `--url` flag (highest)
2. `LEARTECH_ORCH_URL` / `LEARTECH_AGENT_URL` env var
3. `~/.config/leartech-agent/config.yaml` — per-cluster URL map (see below)
4. Built-in staging defaults (`gcp-staging`, `az-staging`)

Manage the on-disk config via subcommands:

```sh
leartech-agent config show
leartech-agent config set-cluster gcp-prod \
    --orch-url https://leartech-orchestrator.product-first.com \
    --agent-url https://leartech-automated-agent.product-first.com
leartech-agent config use-cluster gcp-prod   # flips default_cluster:
```

The config file uses the shape:

```yaml
default_cluster: gcp-staging
clusters:
  gcp-staging:
    orch_url: https://leartech-orchestrator-jx-staging.jx.leartech.com
    agent_url: https://leartech-automated-agent-jx-staging.jx.leartech.com
  az-staging:
    orch_url: https://leartech-orchestrator-jx-staging.az.leartech.com
    agent_url: https://leartech-automated-agent-jx-staging.az.leartech.com
```

The same file is reusable by the future MCP-for-Claude wrapper — keep edits there, not in source.

### Interactive chat (`leartech-agent chat`)

`POST /chat` on the orchestrator preserves conversation state across turns via a `conversation_id`. The `chat` REPL threads that id through the session so successive messages share context:

```sh
leartech-agent chat                          # default cluster's orchestrator
leartech-agent chat --cluster az-staging     # cross-cluster
leartech-agent chat --continue conv-abc123   # resume a prior conversation
```

In-REPL slash commands:

| Command | Effect |
|---|---|
| `:exit` / `:q` | Leave the REPL |
| `:save [path]` | Write transcript to a markdown file |
| `:new` | Drop the conversation id and start fresh |
| `:cost` | Show the session's running cost |
| `:help` | Print the command list |

### Recent-by-default listings

`leartech-agent runs list` defaults to the last 24h so morning triage doesn't page through last week's archive:

```sh
leartech-agent runs list                  # last 24h
leartech-agent runs list --since 7d       # last 7 days
leartech-agent runs list --since 2026-06-01
leartech-agent runs list --all            # full history
```

## Status

**Phase B v1 — service scaffolding.** Repository structure mirrors leartech-ai-classifier; FastAPI scaffold + routers in place; substantive endpoint wiring (initiative runtime, lessons catalog) lands in v1.5. CLI commands work today via `make initiative` etc.

See `memory/project_service_deploy_phase_b.md` (in the parent automated-agent's memory directory) for the full plan.
