# Scripts inventory

Operator/dev helpers for poking at the deployed `leartech-automated-agent`.
The unifying convention: **prefer `kubectl` over `gh` for any state the
cluster already owns**, and call the GitHub API only for state GitHub
actually owns (PR body, reviews, final merge state).

## Why this matters

The operator's GitHub personal access token has a 5000-pts/hour GraphQL
quota. Each `gh pr view --json …` or `gh pr checks --watch` call
charges against that bucket. When a human runs two or three watch loops
in parallel, the quota drains before the hour is out, and **the agent's
own GraphQL calls start 403-ing** — agent runs fail not because the
code is wrong but because the operator was tailing PRs at the same
time (memory `feedback_clone_path_uses_graphql_burns_quota`).

Tekton mirrors PipelineRun state to GitHub as the check rows you see
in the PR UI, but the source of truth lives on the cluster as Tekton
`PipelineRun` objects. Reading them via `kubectl get pipelinerun -l
lighthouse.jenkins-x.io/refs.pull=<PR>` is:

- **Free** — no GraphQL spend
- **Faster** — no API round-trip; just an apiserver list call
- **Strictly more information** — PipelineRuns carry Tekton step state,
  pod names, last-commit SHA, etc. that the GitHub check row doesn't expose

So default to kubectl. Use `gh` only when the question is GitHub-side.

## Script index

| Script | Purpose | Data source |
|---|---|---|
| `watch_run.sh <cluster> <run-id> [poll]` | Tail a specific initiative run's state. | `kubectl exec` into the agent pod → `urllib.request` to `/initiatives/{id}` (in-pod, no GitHub call) |
| `watch_pr_pipelineruns.sh <cluster> <PR> [poll] [owner/repo]` | Tail every Tekton PipelineRun for a PR; print state changes; exit 0 on all-success, 1 on any-failure. | `kubectl get pipelinerun -l lighthouse.jenkins-x.io/refs.pull=<PR>` (no GitHub call) |
| `watch_agent_cluster.sh <cluster> [poll]` | Live diff-based monitor of the deployed agent pod (identity, restarts, image, metrics, initiative summary). | `kubectl get pod` + `kubectl exec` → in-pod `/initiatives` (no GitHub call) |
| `list_runs.sh <cluster> [status]` | List initiative runs the agent has seen. | `kubectl exec` → in-pod `/initiatives` (no GitHub call) |
| `tail_agent_log.sh <cluster> [mode] [--run <run-id>]` | Follow agent pod stdout/stderr, with filter modes (agent/narrative/tools/results/full). | `kubectl logs -f` (no GitHub call) |
| `validate_initiative.sh <yaml>` | Pydantic-validate an initiative YAML locally before POSTing it. | Local-only (`uv run` against the in-repo validator) |
| `upload-secrets-automated-agent-db.sh` | Print the gcloud / vault commands needed to upload the workload DSN secret. | Prints commands only — does not call any API itself. |
| `run_mock_scenario.py <scenario-yaml>` | Drive the mock pipeline MCP scenarios without spawning a real agent. | Local-only (in-process import) |
| `mcp_test_client.py --base <url> [--token T] --list \| --call <server> <tool> '<json>'` | Mini JSON-RPC client to list/call tools on the real Go MCP platform (`leartech-mcp-servers`) — local (auth off) or deployed (port-forward + bearer). Fast MCP test loop, no release cycle. | HTTP JSON-RPC to a running MCP server (no GitHub call); stdlib-only. See README "Testing the MCP tools locally". |

## When `gh` is the right tool

There's a small set of questions only GitHub can answer:

- **PR body / title / description** — `gh pr view <pr> --json body`
- **Review state and reviewer logins** — `gh pr view <pr> --json reviews`
- **Final merge state and merge SHA** — `gh pr view <pr> --json merged,mergeCommit`
- **Posting a comment / `/hold` chatops** — `gh pr comment <pr> --body "…"`
- **Opening / closing a PR** — `gh pr create`, `gh pr close`

None of the scripts in this inventory poll any of those questions in a
loop. If a new helper script needs one of them, fetch once at the start
and cache — never inside the poll loop.

## When `gh pr checks --watch` *is* worth its quota

`gh pr checks --watch` is the right tool **at the very end** of an
agent run when you want one blocking call that returns when every
required check reaches terminal. The agent's `wait_for_terminal` MCP
wraps it for exactly this case. What you should *not* do is run it
as a tail in a separate terminal while the agent is also working —
that's a quota fight the agent will lose.

For the live "what's the state of this PR right now" view, use
`scripts/watch_pr_pipelineruns.sh` instead.

## Adding a new script

Before writing any new operator helper:

1. If the data lives on the cluster (pods, jobs, PipelineRuns, secrets,
   logs, the agent's own `/initiatives` endpoint), reach for `kubectl`.
2. Only call `gh` for GitHub-owned state, and never inside a loop.
3. Add the script to the table above with a one-line "data source"
   note. `tests/test_scripts_lint.py` will syntax-check it on next
   pytest run.
