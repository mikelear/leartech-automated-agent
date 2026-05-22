#!/usr/bin/env bash
# watch_run.sh — tail a specific initiative run's state.
#
# Polls /initiatives/{id} via in-pod python (no port-forward needed). Auto-
# discovers the agent pod each poll so it reattaches across rolling deploys.
# Run state is durable in Postgres since 2026-05-22 — the /initiatives/{id}
# endpoint reads the initiative_runs row, so a watched run survives pod
# restarts (was advertised before, but the in-memory store actually lost
# the row on restart and this script would 404 until DB-backed catalog).
# Diff-based output: only prints when state, pr_number, turns, or cost change.
#
# Usage:
#   scripts/watch_run.sh gcp 005527f67608           # GCP, default 10s poll
#   scripts/watch_run.sh az  005527f67608 5         # AZ, 5s poll

set -uo pipefail

CLUSTER="${1:-}"
RUN_ID="${2:-}"
POLL="${3:-10}"

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> <run-id> [poll-seconds]"; exit 2 ;;
esac

if [ -z "$RUN_ID" ]; then
  echo "Usage: $0 <gcp|az> <run-id> [poll-seconds]"
  exit 2
fi

NS=jx-staging
SELECTOR='app.kubernetes.io/instance=leartech-automated-agent'

if [ -t 1 ]; then
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'
  DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; YELLOW=""; RED=""; CYAN=""; DIM=""; RESET=""
fi

ts() { date +'%H:%M:%S'; }

echo "${CYAN}── watching run $RUN_ID on $CLUSTER (poll=${POLL}s) — Ctrl-C to stop ──${RESET}"

prev_state=""
while true; do
  POD=$(kubectl --context=$CTX -n $NS get pod -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "$POD" ]; then
    printf "%s  %sno pod%s\n" "$(ts)" "$YELLOW" "$RESET"
    sleep "$POLL"; continue
  fi

  raw=$(kubectl --context=$CTX -n $NS exec "$POD" -- python -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:8080/initiatives/$RUN_ID', timeout=5).read().decode()
    d = json.loads(r)
    fields = ['status', 'pr_number', 'pr_repo', 'turns', 'cost_usd', 'error', 'cluster']
    print(json.dumps({k: d.get(k) for k in fields}))
except Exception as e:
    print(json.dumps({'_err': type(e).__name__}))
" 2>/dev/null)

  if [ "$raw" != "$prev_state" ]; then
    echo "$raw" | python3 -c "
import sys, json
d = json.loads(sys.stdin.read())
if '_err' in d:
    print(f'  ${RED}error: {d[\"_err\"]}${RESET}')
else:
    status = d.get('status', '?')
    color = '${GREEN}' if status == 'complete' else ('${RED}' if status == 'failed' else '${YELLOW}')
    print(f'  $(ts)  status={color}{status}${RESET}  pr={d.get(\"pr_number\")}  turns={d.get(\"turns\")}  cost=\${d.get(\"cost_usd\")}  cluster={d.get(\"cluster\")}')
    if d.get('error'): print(f'  ${RED}error: {d[\"error\"][:200]}${RESET}')
    if d.get('pr_number') and d.get('pr_repo'):
        print(f'  ${CYAN}PR:${RESET} https://github.com/{d[\"pr_repo\"]}/pull/{d[\"pr_number\"]}')
"
    prev_state="$raw"
  fi

  # Exit on terminal status
  status=$(echo "$raw" | python3 -c "import sys, json; print(json.loads(sys.stdin.read()).get('status', ''))" 2>/dev/null)
  if [ "$status" = "complete" ] || [ "$status" = "failed" ] || [ "$status" = "cancelled" ]; then
    echo "${CYAN}── run reached terminal state — exiting ──${RESET}"
    break
  fi

  sleep "$POLL"
done
