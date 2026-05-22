#!/usr/bin/env bash
# list_runs.sh — list the agent's recent initiative runs.
#
# Queries /initiatives on the live agent pod. Shows id, status, initiative
# name, started_at, pr_number — enough to pick a run-id for watch_run.sh.
#
# Runs are durable in Postgres since 2026-05-22 (postgresql.enabled flipped
# on both clusters). Pod restarts no longer lose history — /initiatives
# reads from the initiative_runs table, so this lists every run the
# cluster has seen, not just the current pod lifecycle.
#
# Usage:
#   scripts/list_runs.sh gcp                # GCP, all visible runs
#   scripts/list_runs.sh az                 # AZ
#   scripts/list_runs.sh gcp running        # only runs with status=running

set -uo pipefail

CLUSTER="${1:-}"
STATUS_FILTER="${2:-}"

case "$CLUSTER" in
  gcp) CTX="gke_product-first_us-east1-b_tf-jx-usable-bird" ;;
  az)  CTX="modern-burro" ;;
  *)   echo "Usage: $0 <gcp|az> [status-filter]"; exit 2 ;;
esac

NS=jx-staging
SELECTOR='app.kubernetes.io/instance=leartech-automated-agent'

POD=$(kubectl --context=$CTX -n $NS get pod -l "$SELECTOR" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [ -z "$POD" ]; then
  echo "No agent pod found via selector on $CLUSTER."
  exit 1
fi

kubectl --context=$CTX -n $NS exec "$POD" -- python -c "
import urllib.request, json
r = urllib.request.urlopen('http://localhost:8080/initiatives', timeout=5).read().decode()
runs = json.loads(r)
filt = '$STATUS_FILTER'
if filt:
    runs = [r for r in runs if r['status'] == filt]
if not runs:
    print(f'No runs{\" matching status=\" + filt if filt else \"\"} on $CLUSTER.')
else:
    print(f'{len(runs)} run(s) on $CLUSTER:')
    print(f'  {\"ID\":<14} {\"STATUS\":<10} {\"PR\":<6} {\"STARTED\":<20}  INITIATIVE')
    for r in sorted(runs, key=lambda r: r['started_at'], reverse=True):
        pr = r.get('pr_number') or '-'
        started = r['started_at'][:19] if r.get('started_at') else '?'
        print(f'  {r[\"id\"]:<14} {r[\"status\"]:<10} {pr!s:<6} {started:<20}  {r[\"initiative\"]}')
" 2>&1
