#!/usr/bin/env bash
# Quick deterministic agent-run status. Mirror of orchestrator/scripts/plan-status.sh.
#
# Usage:
#   scripts/run-status.sh RUN_ID                    # one-shot pretty
#   scripts/run-status.sh RUN_ID --raw              # full JSON
#   scripts/run-status.sh RUN_ID --terse            # one-line machine-friendly
#
# Env:
#   LEARTECH_AGENT_BASE_URL  default https://leartech-automated-agent-jx-staging.jx.leartech.com

set -u
BASE="${LEARTECH_AGENT_BASE_URL:-https://leartech-automated-agent-jx-staging.jx.leartech.com}"
RUN_ID="${1:-}"
MODE="${2:-pretty}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: run-status.sh RUN_ID [--raw|--terse|--pretty]" >&2
  exit 2
fi

data=$(curl -sS "$BASE/initiatives/$RUN_ID" 2>/dev/null) || data=""
if [ -z "$data" ]; then
  echo "ERROR: empty response from $BASE/initiatives/$RUN_ID" >&2
  exit 1
fi

case "$MODE" in
  --raw)
    echo "$data" | jq .
    ;;
  --terse)
    echo "$data" | jq -r '"\(.status) cost=$\(.cost_usd // 0) turns=\(.turns // 0) pr=\(.pr_number // "none")"'
    ;;
  *)
    echo "$data" | jq -r '
      "── run ──────────────────────────────────────────────────────────────",
      "  id:       " + .id,
      "  init:     " + (.initiative // "?"),
      "  status:   " + .status,
      "  branch:   " + (.branch // "?"),
      "  pr:       " + ((.pr_number // "?") | tostring),
      "  turns:    " + ((.turns // 0) | tostring),
      "  cost:     $" + ((.cost_usd // 0) | tostring),
      "  error:    " + (if .error then (.error | tostring)[0:200] else "none" end)
    '
    ;;
esac
