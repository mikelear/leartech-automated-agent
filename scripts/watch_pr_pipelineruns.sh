#!/usr/bin/env bash
# watch_pr_pipelineruns.sh — tail Tekton PipelineRuns for a GitHub PR.
#
# Polls `kubectl get pipelinerun -l lighthouse.jenkins-x.io/refs.pull=<PR>`
# and prints state transitions. Cluster-side state only — no GitHub
# GraphQL traffic, so this script does not burn the operator's
# 5000-pts/hour bucket and is safe to leave running alongside agent
# runs.
#
# Why kubectl rather than `gh pr checks --watch`:
#
#   The `gh` CLI hits the GitHub GraphQL API on each watch tick. A
#   single operator running `gh pr checks --watch` on three PRs in
#   parallel can starve agent runs of quota for the rest of the hour
#   (memory `feedback_clone_path_uses_graphql_burns_quota`). Tekton
#   PipelineRuns hold the same status that's mirrored back to GitHub
#   as the check rows, so reading directly from the cluster is both
#   faster (no API round-trip) and free.
#
#   This script complements `gh pr checks` for the GitHub-side view
#   (final merge state, required-check policy) — use both, but reach
#   for kubectl first.
#
# Usage:
#   scripts/watch_pr_pipelineruns.sh gcp 123          # GCP, default 10s poll
#   scripts/watch_pr_pipelineruns.sh az  456 5        # AZ, 5s poll
#   scripts/watch_pr_pipelineruns.sh gcp 123 10 mikelear/leartech-automated-agent
#                                                    # filter by repo too
#
# Stop with Ctrl-C. The script also exits 0 when every PipelineRun
# reaches a terminal state (Succeeded, Failed, Cancelled) so it can be
# composed with `&&` in CI shells.

set -uo pipefail

CLUSTER="${1:-}"
PR="${2:-}"
POLL="${3:-10}"
REPO="${4:-}"

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> <PR-number> [poll-seconds] [owner/repo]"; exit 2 ;;
esac

if [ -z "$PR" ] || ! [[ "$PR" =~ ^[0-9]+$ ]]; then
  echo "Usage: $0 <gcp|az> <PR-number> [poll-seconds] [owner/repo]"
  exit 2
fi

if ! [[ "$POLL" =~ ^[0-9]+$ ]]; then
  echo "Error: poll-seconds must be a number (got '$POLL')."
  exit 2
fi

NS=jx
SELECTOR="lighthouse.jenkins-x.io/refs.pull=$PR"
if [ -n "$REPO" ]; then
  SELECTOR="$SELECTOR,lighthouse.jenkins-x.io/refs.repo=$REPO"
fi

if [ -t 1 ]; then
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'
  CYAN=$'\033[36m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  GREEN=""; YELLOW=""; RED=""; CYAN=""; DIM=""; RESET=""
fi

ts() { date +'%H:%M:%S'; }

echo "${CYAN}── watching PipelineRuns for PR $PR on $CLUSTER (poll=${POLL}s) — Ctrl-C to stop ──${RESET}"
echo "${DIM}selector: $SELECTOR${RESET}"

# State map keyed by PipelineRun name → "status|reason".
declare -A prev_state

while true; do
  # `kubectl get pipelinerun` with custom-columns avoids the verbose default
  # output and gives us a stable parseable shape. The conditions[0].type for a
  # PipelineRun is always "Succeeded"; the status field is True/False/Unknown.
  raw=$(kubectl --context=$CTX -n $NS get pipelinerun -l "$SELECTOR" \
    -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.conditions[0].status}{"|"}{.status.conditions[0].reason}{"\n"}{end}' \
    2>/dev/null)

  if [ -z "$raw" ]; then
    if [ ${#prev_state[@]} -eq 0 ]; then
      printf "%s  %sno PipelineRuns yet for PR %s%s\n" "$(ts)" "$YELLOW" "$PR" "$RESET"
    fi
    sleep "$POLL"
    continue
  fi

  all_terminal=true
  any_failed=false

  # Read line-by-line; `IFS=|` would also work but the loop is clearer with cut.
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    name=$(echo "$line" | cut -d'|' -f1)
    status=$(echo "$line" | cut -d'|' -f2)
    reason=$(echo "$line" | cut -d'|' -f3)

    # Empty status = condition not set yet (just-created PR).
    if [ -z "$status" ]; then
      status="Pending"
      reason="Pending"
    fi

    current="$status|$reason"
    previous="${prev_state[$name]:-}"

    if [ "$current" != "$previous" ]; then
      colour="$YELLOW"
      case "$status" in
        True)  colour="$GREEN" ;;
        False) colour="$RED" ;;
      esac
      printf "%s  %s%-40s%s  %s%-10s%s  %s%s%s\n" \
        "$(ts)" "$CYAN" "$name" "$RESET" "$colour" "$status" "$RESET" "$DIM" "$reason" "$RESET"
      prev_state[$name]="$current"
    fi

    # Terminal-detection: True = Succeeded, False = Failed/Cancelled.
    # Unknown / empty = still running.
    if [ "$status" != "True" ] && [ "$status" != "False" ]; then
      all_terminal=false
    fi
    if [ "$status" = "False" ]; then
      any_failed=true
    fi
  done <<< "$raw"

  if $all_terminal; then
    if $any_failed; then
      echo "${RED}── all PipelineRuns terminal — at least one failed ──${RESET}"
      exit 1
    fi
    echo "${GREEN}── all PipelineRuns succeeded ──${RESET}"
    exit 0
  fi

  sleep "$POLL"
done
