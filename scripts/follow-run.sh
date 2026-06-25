#!/usr/bin/env bash
# Poll run-status.sh every N seconds; emit one line ON CHANGE.
# Same pattern as orchestrator/scripts/follow-plan.sh.
#
# Usage:
#   scripts/follow-run.sh RUN_ID                    # poll forever, 60s
#   scripts/follow-run.sh RUN_ID 180                # poll every 180s
#   scripts/follow-run.sh RUN_ID 60 30               # poll 60s, max 30 iters

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN_ID="${1:-}"
INTERVAL="${2:-60}"
MAX_ITERS="${3:-9999}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: follow-run.sh RUN_ID [interval_seconds=60] [max_iterations=9999]" >&2
  exit 2
fi

prev=""
i=0
while [ "$i" -lt "$MAX_ITERS" ]; do
  i=$((i + 1))
  ts="$(date -u +%H:%M:%S)"
  cur="$("$HERE/run-status.sh" "$RUN_ID" --terse 2>/dev/null || echo "fetch-failed")"

  if [ "$cur" != "$prev" ]; then
    printf '[%s] %s\n' "$ts" "$cur"
    prev="$cur"
  fi

  status="${cur%% *}"
  case "$status" in
    complete|failed|cancelled)
      printf '[%s] TERMINAL %s\n' "$ts" "$status"
      exit 0
      ;;
  esac

  sleep "$INTERVAL"
done
printf '[%s] MAX_ITERS reached\n' "$(date -u +%H:%M:%S)"
