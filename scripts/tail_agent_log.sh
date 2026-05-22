#!/usr/bin/env bash
# tail_agent_log.sh — follow the live agent pod's stdout/stderr.
#
# Auto-discovers the current pod via label selector — survives rolling
# deploys (will reattach to the NEW pod after a restart).
#
# Modes (see below for what each filter):
#   agent     (default) what the agent is doing: prose + tool calls + iteration summaries
#   narrative just the agent's prose between tool calls (cleanest "what is it thinking" view)
#   tools     only `→ Bash` / `→ Read` / `→ Edit` etc tool-use markers
#   results   only `--- turns=X cost=$Y` iteration-end summaries
#   full      everything from kubectl logs, raw (incl. HTTP request log lines)
#
# Usage:
#   scripts/tail_agent_log.sh gcp                # default `agent` mode
#   scripts/tail_agent_log.sh az  narrative      # only the prose
#   scripts/tail_agent_log.sh gcp tools          # tool-call markers
#   scripts/tail_agent_log.sh gcp full           # raw, unfiltered

set -uo pipefail

CLUSTER="${1:-}"
MODE="${2:-agent}"

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> [agent|narrative|tools|results|full]"; exit 2 ;;
esac

NS=jx-staging
SELECTOR='app.kubernetes.io/instance=leartech-automated-agent'

POD=$(kubectl --context=$CTX -n $NS get pod -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "No agent pod found via selector on $CLUSTER. Check 'kubectl --context=$CTX -n $NS get pods'."
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
