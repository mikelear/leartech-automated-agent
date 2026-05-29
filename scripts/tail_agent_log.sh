#!/usr/bin/env bash
# tail_agent_log.sh — follow the live agent pod's stdout/stderr.
#
# Default: tails the API pod (asyncio-runtime mode where the agent loop
# runs in the same pod as FastAPI). Pass `--run <run-id>` to tail the
# Job pod for a specific Job-runtime run instead (D.4+).
#
# Auto-discovers the current pod via label selector — survives rolling
# deploys when tailing the API pod. Job pods don't roll so the run-id
# pins to a specific pod.
#
# Modes (see below for what each filter):
#   agent     (default) what the agent is doing: prose + tool calls + iteration summaries
#   narrative just the agent's prose between tool calls (cleanest "what is it thinking" view)
#   tools     only `→ Bash` / `→ Read` / `→ Edit` etc tool-use markers
#   results   only `--- turns=X cost=$Y` iteration-end summaries
#   full      everything from kubectl logs, raw (incl. HTTP request log lines)
#
# Usage:
#   scripts/tail_agent_log.sh gcp                       # API pod, default mode
#   scripts/tail_agent_log.sh az  narrative             # API pod, prose only
#   scripts/tail_agent_log.sh az  agent --run abc12345  # Job pod for run abc12345

set -uo pipefail

CLUSTER="${1:-}"
shift || true
# Parse remaining args. Accept --run <id> at any position (after the cluster):
#   tail_agent_log.sh az --run abc12345           # Job pod, default mode
#   tail_agent_log.sh az narrative --run abc12345 # Job pod, narrative mode
#   tail_agent_log.sh az --run abc12345 tools     # Job pod, tools mode
MODE="agent"
RUN_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --run)
      RUN_ID="${2:-}"
      shift 2 || shift
      ;;
    agent|narrative|tools|results|full)
      MODE="$1"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> [mode] [--run <run-id>]"; exit 2 ;;
esac

NS=jx-staging
if [ -n "$RUN_ID" ]; then
  SELECTOR="leartech.io/run-id=$RUN_ID"
  POD_DESC="Job pod for run $RUN_ID"
else
  SELECTOR='app.kubernetes.io/instance=leartech-automated-agent'
  POD_DESC='API pod'
fi

POD=$(kubectl --context=$CTX -n $NS get pod -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "No $POD_DESC found on $CLUSTER. Check 'kubectl --context=$CTX -n $NS get pods'."
  exit 1
fi

echo "── tailing $CLUSTER agent log — pod=$POD mode=$MODE — Ctrl-C to stop ──"

# Noise patterns — both HTTP request log lines (probes + my own polling) and
# uvicorn lifecycle messages. Subtract these from any mode that wants "just
# the agent's work" so my polling scripts don't drown out the signal.
NOISE_REGEX='^INFO:.*"(GET|POST) /(readyz|health|initiatives)'

case "$MODE" in
  tools)
    # Tool-use markers only — one line per tool call the agent makes.
    kubectl --context=$CTX -n $NS logs -f --tail=500 "$POD" 2>&1 | grep --line-buffered -E "^→ "
    ;;
  results)
    # ResultMessage summaries — one line per agent iteration end (turns/cost).
    kubectl --context=$CTX -n $NS logs -f --tail=500 "$POD" 2>&1 | grep --line-buffered -E "^--- turns="
    ;;
  narrative)
    # Agent's prose between tool calls — strip tool markers AND HTTP noise.
    # Leaves the text the agent emits (TextBlock content) + iteration summaries.
    kubectl --context=$CTX -n $NS logs -f --tail=500 "$POD" 2>&1 \
      | grep --line-buffered -vE "$NOISE_REGEX" \
      | grep --line-buffered -vE "^→ " \
      | grep --line-buffered -vE "^INFO: "
    ;;
  full)
    # Everything raw — useful when debugging the pod itself, not the agent's work.
    kubectl --context=$CTX -n $NS logs -f --tail=500 "$POD" 2>&1
    ;;
  agent|*)
    # Default: what the agent is doing. Includes prose + tool markers +
    # iteration summaries. Strips HTTP request log lines and uvicorn noise.
    kubectl --context=$CTX -n $NS logs -f --tail=500 "$POD" 2>&1 \
      | grep --line-buffered -vE "$NOISE_REGEX" \
      | grep --line-buffered -vE "^INFO:     "
    ;;
esac
