#!/usr/bin/env bash
# watch_agent_cluster.sh — live diff-based monitor for the deployed
# leartech-automated-agent on one cluster.
#
# Auto-discovers the current pod each poll (never hardcodes a name) so it
# survives pod restarts/OOMKills/redeploys. Prints ONLY on state changes
# so the output stays scannable. Watches:
#
#   - Pod identity changes (old-pod → new-pod) with last-state reason
#   - Restart count deltas (OOMKilled, Evicted, CrashLoopBackOff)
#   - Image version changes (deployment rollouts)
#   - Memory / CPU usage (when metrics-server is available)
#   - In-flight initiative count + status breakdown (via the agent's HTTP API)
#
# Usage:
#   scripts/watch_agent_cluster.sh gcp        # GCP cluster
#   scripts/watch_agent_cluster.sh az         # Azure cluster
#   scripts/watch_agent_cluster.sh gcp 5      # poll every 5s instead of 10s
#
# Stop with Ctrl-C.

set -uo pipefail

CLUSTER="${1:-}"
POLL="${2:-10}"

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> [poll-seconds]"; exit 2 ;;
esac

if ! [[ "$POLL" =~ ^[0-9]+$ ]]; then
  echo "Error: poll-seconds must be a number (got '$POLL')."
  echo "Tip: for tool-use markers use './scripts/tail_agent_log.sh $CLUSTER tools'."
  exit 2
fi

NS=jx-staging
SELECTOR='app.kubernetes.io/instance=leartech-automated-agent'
# Earlier deploys lacked the instance label — fall back to name-based match.
SELECTOR_FALLBACK='leartech-automated-agent'

# Colours (auto-disabled when stdout isn't a TTY).
if [ -t 1 ]; then
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'
  DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; YELLOW=""; RED=""; CYAN=""; DIM=""; RESET=""
fi

ts() { date +'%H:%M:%S'; }

# Auto-discover the live agent pod's name + relevant status fields.
get_pod_state() {
  # Prefer label selector; fall back to grep on name pattern.
  local name
  name=$(kubectl --context=$CTX -n $NS get pod -l "$SELECTOR" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [ -z "$name" ]; then
    name=$(kubectl --context=$CTX -n $NS get pod 2>/dev/null \
      | awk -v p="$SELECTOR_FALLBACK" '$1 ~ p && $3 != "Completed" {print $1; exit}')
  fi
  if [ -z "$name" ]; then
    echo "MISSING|||||"
    return
  fi
  # name | phase | ready | restarts | image | lastTerminationReason
  kubectl --context=$CTX -n $NS get pod $name -o jsonpath='{.metadata.name}|{.status.phase}|{.status.containerStatuses[0].ready}|{.status.containerStatuses[0].restartCount}|{.spec.containers[0].image}|{.status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null
}

# Live in-flight initiative summary via the agent's HTTP API (in-pod curl).
get_initiative_summary() {
  local pod_name="$1"
  [ -z "$pod_name" ] && return
  kubectl --context=$CTX -n $NS exec "$pod_name" -- python -c "
import urllib.request, json
try:
    r = urllib.request.urlopen('http://localhost:8080/initiatives', timeout=3).read().decode()
    runs = json.loads(r)
    if not runs:
        print('0 runs')
        return
    counts = {}
    for run in runs:
        counts[run['status']] = counts.get(run['status'], 0) + 1
    print(f\"{len(runs)} runs: \" + ', '.join(f'{v} {k}' for k, v in sorted(counts.items())))
except Exception as e:
    print(f'(api error: {type(e).__name__})')
" 2>/dev/null
}

# Resource usage from metrics-server (if available).
get_pod_metrics() {
  local pod_name="$1"
  [ -z "$pod_name" ] && return
  kubectl --context=$CTX -n $NS top pod "$pod_name" --no-headers 2>/dev/null \
    | awk '{print $2 "/" $3}'
}

# Print a row when something changed. $1=label  $2=current  $3=previous
print_if_changed() {
  if [ "$2" != "$3" ]; then
    printf "%s  %s%-14s%s  %s%s%s  %s← was %s%s\n" \
      "$(ts)" "$CYAN" "$1" "$RESET" "$GREEN" "$2" "$RESET" "$DIM" "$3" "$RESET"
  fi
}

echo "${CYAN}── watching $CLUSTER agent (context=$CTX, poll=${POLL}s) — Ctrl-C to stop ──${RESET}"

prev_state=""
prev_metrics=""
prev_inits=""
prev_pod_name=""

while true; do
  state=$(get_pod_state)
  pod_name=$(echo "$state" | cut -d'|' -f1)
  phase=$(echo "$state" | cut -d'|' -f2)
  ready=$(echo "$state" | cut -d'|' -f3)
  restarts=$(echo "$state" | cut -d'|' -f4)
  image=$(echo "$state" | cut -d'|' -f5)
  last_term=$(echo "$state" | cut -d'|' -f6)

  if [ "$pod_name" = "MISSING" ]; then
    if [ "$prev_state" != "MISSING" ]; then
      printf "%s  %s%-14s%s  %sno pod matching selector — deployment may be rolling%s\n" \
        "$(ts)" "$CYAN" "missing" "$RESET" "$RED" "$RESET"
      prev_state="MISSING"
    fi
    sleep "$POLL"
    continue
  fi

  # First iteration — print full snapshot then track diffs.
  if [ -z "$prev_state" ]; then
    printf "%s  %sinitial snapshot:%s\n" "$(ts)" "$CYAN" "$RESET"
    printf "                pod      %s%s%s\n" "$GREEN" "$pod_name" "$RESET"
    printf "                phase    %s\n" "$phase"
    printf "                ready    %s\n" "$ready"
    printf "                restarts %s\n" "$restarts"
    printf "                image    %s\n" "$image"
    [ -n "$last_term" ] && printf "                lastTerm %s%s%s\n" "$YELLOW" "$last_term" "$RESET"
    prev_state="$state"
    prev_pod_name="$pod_name"
  else
    prev_pod=$(echo "$prev_state" | cut -d'|' -f1)
    prev_phase=$(echo "$prev_state" | cut -d'|' -f2)
    prev_ready=$(echo "$prev_state" | cut -d'|' -f3)
    prev_restarts=$(echo "$prev_state" | cut -d'|' -f4)
    prev_image=$(echo "$prev_state" | cut -d'|' -f5)
    prev_last_term=$(echo "$prev_state" | cut -d'|' -f6)

    if [ "$pod_name" != "$prev_pod" ]; then
      printf "%s  %s%-14s%s  %s%s%s  %s← replaced %s%s\n" \
        "$(ts)" "$CYAN" "POD-NEW" "$RESET" "$GREEN" "$pod_name" "$RESET" "$YELLOW" "$prev_pod" "$RESET"
    fi
    print_if_changed "phase" "$phase" "$prev_phase"
    print_if_changed "ready" "$ready" "$prev_ready"
    if [ "$restarts" != "$prev_restarts" ]; then
      reason="${last_term:-unknown}"
      colour="$YELLOW"
      [ "$reason" = "OOMKilled" ] || [ "$reason" = "Error" ] && colour="$RED"
      printf "%s  %s%-14s%s  %s%s (was %s) — last reason: %s%s%s\n" \
        "$(ts)" "$CYAN" "RESTART" "$RESET" "$colour" "$restarts" "$prev_restarts" "$reason" "$RESET" ""
    fi
    print_if_changed "image" "$image" "$prev_image"

    prev_state="$state"
    prev_pod_name="$pod_name"
  fi

  # Metrics + initiative summary (lower-priority — print only on change).
  metrics=$(get_pod_metrics "$pod_name")
  print_if_changed "cpu/mem" "$metrics" "$prev_metrics"
  prev_metrics="$metrics"

  inits=$(get_initiative_summary "$pod_name")
  if [ -n "$inits" ] && [ "$inits" != "$prev_inits" ]; then
    printf "%s  %s%-14s%s  %s%s%s\n" "$(ts)" "$CYAN" "initiatives" "$RESET" "$GREEN" "$inits" "$RESET"
    prev_inits="$inits"
  fi

  sleep "$POLL"
done
