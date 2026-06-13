#!/usr/bin/env bash
# scripts/e2e.sh — exercise the deployed agent service through real HTTP.
#
# Source-tree pytest is necessary but not sufficient: it doesn't catch
# Dockerfile gaps, missing init code, or env-var plumbing errors. This
# script starts a uvicorn server (using the same `app.main:app` the
# container runs) and hits its HTTP surface end-to-end.
#
# See `gate/agent/lessons/catalog/every-initiative-extends-the-e2e-script.md`
# for the contract — every initiative that adds new behaviour should
# extend this script with an exercise for the new surface.
#
# What's covered today
# ────────────────────
#   • GET  /health/live           — process is up and responding
#   • POST /initiatives/_validate — pre-flight validation (text/plain body)
#   • POST /initiatives/_validate_body
#                                 — alias path added for the inline-body
#                                   feature (`initiative_body` on POST
#                                   /initiatives). Same contract as the
#                                   /_validate endpoint; the dedicated
#                                   route is what callers reach for when
#                                   pre-flighting a body destined for
#                                   inline firing.
#   • POST /initiatives           — validation surface only (422 / 404
#                                   paths) — no K8s available locally so
#                                   we don't drive an actual Job spawn.
#
# Usage:
#   scripts/e2e.sh                 # default — start server, run all checks
#   PORT=18080 scripts/e2e.sh      # override port
#   KEEP_SERVER=1 scripts/e2e.sh   # leave server running for poking
#
# Exit codes: 0 = all green, 1 = some check failed.

set -euo pipefail

PORT="${PORT:-18080}"
BASE_URL="http://127.0.0.1:${PORT}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${REPO_ROOT}"

server_pid=""
cleanup() {
  if [ -n "${server_pid}" ] && [ -z "${KEEP_SERVER:-}" ]; then
    echo "→ stopping uvicorn (pid=${server_pid})"
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "→ starting uvicorn on :${PORT}"
uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --log-level warning &
server_pid=$!

# Poll /health/live until the server starts answering, up to ~15 s.
for _ in $(seq 1 30); do
  if curl -sf "${BASE_URL}/health/live" >/dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "${BASE_URL}/health/live" >/dev/null; then
  echo "✗ server never reached /health/live; aborting" >&2
  exit 1
fi
echo "✓ /health/live ready"

failures=0
assert_status() {
  local label="$1" expected="$2" got="$3"
  if [ "${got}" = "${expected}" ]; then
    echo "✓ ${label}: ${got}"
  else
    echo "✗ ${label}: expected ${expected}, got ${got}" >&2
    failures=$((failures + 1))
  fi
}

# ── /_validate body alias — the new endpoint added alongside /_validate ──
# Sends a minimal-but-valid initiative YAML body and expects a 200 with a
# JSON summary. The catalog endpoint never sees this body; pure dry-run.
inline_body=$(cat <<'EOF'
name: e2e-inline-validate-only
repo: leartech-test
branch: agent/e2e-inline
base: main
goal: |
  E2E exercise of the inline-initiative-body validate path. No-op goal;
  this body is parsed only — never fired against K8s.
EOF
)

status=$(curl -s -o /tmp/e2e-validate-body.json -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives/_validate_body" \
  -H 'content-type: text/plain' \
  --data "${inline_body}")
assert_status 'POST /initiatives/_validate_body (valid body)' 200 "${status}"
grep -q '"e2e-inline-validate-only"' /tmp/e2e-validate-body.json \
  || { echo "✗ /_validate_body: parsed `name:` missing from response" >&2; failures=$((failures + 1)); }

# Same body should round-trip through the legacy /_validate endpoint
# — the two routes are aliases and must stay in lock-step.
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives/_validate" \
  -H 'content-type: text/plain' \
  --data "${inline_body}")
assert_status 'POST /initiatives/_validate (valid body)' 200 "${status}"

# Empty body → 422 on both routes.
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives/_validate_body" \
  -H 'content-type: text/plain' \
  --data '')
assert_status 'POST /initiatives/_validate_body (empty)' 422 "${status}"

# ── POST /initiatives validation surface ─────────────────────────────────
# Spawning a real Job requires POD_NAMESPACE + LEARTECH_INITIATIVE_DEFAULT_IMAGE
# + in-cluster ServiceAccount — none of which are available in a laptop e2e
# context. We exercise the validation paths instead, which DO run in-process.
#
# Both fields set → 422 (XOR validator on StartInitiativeRequest).
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives" \
  -H 'content-type: application/json' \
  --data '{"initiative": "some-name", "initiative_body": "name: x\nrepo: r\nbranch: b\ngoal: g\n"}')
assert_status 'POST /initiatives (both set)' 422 "${status}"

# Neither set → 422.
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives" \
  -H 'content-type: application/json' \
  --data '{}')
assert_status 'POST /initiatives (neither set)' 422 "${status}"

# Malformed inline body → 422 (validator fires before any spawn attempt).
status=$(curl -s -o /dev/null -w '%{http_code}' \
  -X POST "${BASE_URL}/initiatives" \
  -H 'content-type: application/json' \
  --data '{"initiative_body": "not: a\nvalid: initiative\n"}')
assert_status 'POST /initiatives (malformed body)' 422 "${status}"

# ── Introspection surface — feeds the leartech-agent CLI ────────────────
status=$(curl -s -o /tmp/e2e-health-detail.json -w '%{http_code}' \
  "${BASE_URL}/health/detail")
assert_status 'GET /health/detail' 200 "${status}"
grep -q '"service": "leartech-automated-agent"' /tmp/e2e-health-detail.json \
  || { echo "✗ /health/detail: service field missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-mcps.json -w '%{http_code}' "${BASE_URL}/mcps")
assert_status 'GET /mcps' 200 "${status}"
grep -q '"leartech-pipeline"' /tmp/e2e-mcps.json \
  || { echo "✗ /mcps: leartech-pipeline missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-roles.json -w '%{http_code}' "${BASE_URL}/roles")
assert_status 'GET /roles' 200 "${status}"
grep -q '"initiative_agent"' /tmp/e2e-roles.json \
  || { echo "✗ /roles: initiative_agent missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-topo.json -w '%{http_code}' "${BASE_URL}/topology")
assert_status 'GET /topology' 200 "${status}"
grep -q 'Phase 1' /tmp/e2e-topo.json \
  || { echo "✗ /topology: Phase 1 marker missing" >&2; failures=$((failures + 1)); }

# Active probe on an sdk-type MCP — must report 'ready' + sdk_import probe kind.
status=$(curl -s -o /tmp/e2e-mcp-health.json -w '%{http_code}' \
  "${BASE_URL}/mcps/leartech-pipeline/health")
assert_status 'GET /mcps/leartech-pipeline/health' 200 "${status}"
grep -q '"status": *"ready"' /tmp/e2e-mcp-health.json \
  || { echo "✗ /mcps/.../health: status!=ready" >&2; failures=$((failures + 1)); }

# CLI smoke: invoke `leartech-agent health --url <local>` against the same
# server and assert the rendered panel contains the platform identifier.
# (uv run keeps the project deps available without a global install.)
if uv run leartech-agent --url "${BASE_URL}" health > /tmp/e2e-cli-health.txt 2>&1; then
  grep -q 'Platform health' /tmp/e2e-cli-health.txt \
    && echo "✓ leartech-agent health (CLI smoke)" \
    || { echo "✗ leartech-agent health: panel header missing" >&2; failures=$((failures + 1)); }
else
  echo "✗ leartech-agent health: CLI exited non-zero" >&2
  cat /tmp/e2e-cli-health.txt >&2
  failures=$((failures + 1))
fi

# CLI install-resolution smoke: the deployed pod must be able to invoke
# `leartech-agent` as a bare command (no `uv run` prefix), because operators
# reach the CLI via `kubectl exec <pod> -- leartech-agent ...`. We approximate
# the pod's shell PATH by resolving the venv binary that `uv sync` produces
# and asserting it is on disk + executable. This guards the Dockerfile
# `ENV PATH=/app/.venv/bin:...` invariant introduced to fix the install gap.
venv_bin="$(uv run --no-sync python -c 'import sysconfig; print(sysconfig.get_path("scripts"))')"
if [ -x "${venv_bin}/leartech-agent" ]; then
  echo "✓ leartech-agent console_script materialised at ${venv_bin}/leartech-agent"
else
  echo "✗ leartech-agent console_script NOT found at ${venv_bin}/leartech-agent — install path is broken" >&2
  ls -la "${venv_bin}" >&2 || true
  failures=$((failures + 1))
fi

# Bare-command invocation: PATH must already include the venv scripts dir
# (the Dockerfile sets it; locally `uv run` does too). If this fails the
# operator's `kubectl exec pod -- leartech-agent health` will also fail.
if PATH="${venv_bin}:${PATH}" leartech-agent --url "${BASE_URL}" health > /tmp/e2e-cli-bare.txt 2>&1; then
  grep -q 'Platform health' /tmp/e2e-cli-bare.txt \
    && echo "✓ leartech-agent (bare command, no \`uv run\`) — pod-shape PATH check" \
    || { echo "✗ leartech-agent (bare): panel header missing" >&2; failures=$((failures + 1)); }
else
  echo "✗ leartech-agent (bare): CLI exited non-zero — PATH or install is broken" >&2
  cat /tmp/e2e-cli-bare.txt >&2
  failures=$((failures + 1))
fi

# Chat REPL surface smoke — exercise `--help` rather than driving the REPL
# (which needs a TTY) so this stays portable across CI shells. The flag
# parser also picks up the cluster + orch-url options; if the new
# subcommand ever drops out of the entry-point wiring, this catches it.
if uv run leartech-agent chat --help > /tmp/e2e-chat-help.txt 2>&1; then
  grep -q 'interactive chat REPL' /tmp/e2e-chat-help.txt \
    && echo "✓ leartech-agent chat --help (REPL subcommand wired)" \
    || { echo "✗ leartech-agent chat --help: subcommand description missing" >&2; failures=$((failures + 1)); }
else
  echo "✗ leartech-agent chat --help: CLI exited non-zero" >&2
  cat /tmp/e2e-chat-help.txt >&2
  failures=$((failures + 1))
fi

# Config CRUD subcommands — same `--help` exercise covers all three.
if uv run leartech-agent config --help > /tmp/e2e-config-help.txt 2>&1; then
  for sub in set-cluster use-cluster show; do
    grep -q "${sub}" /tmp/e2e-config-help.txt \
      || { echo "✗ leartech-agent config: ${sub} subcommand missing from --help" >&2; failures=$((failures + 1)); }
  done
  echo "✓ leartech-agent config --help (show/set-cluster/use-cluster wired)"
else
  echo "✗ leartech-agent config --help: CLI exited non-zero" >&2
  cat /tmp/e2e-config-help.txt >&2
  failures=$((failures + 1))
fi

# end2end-gate watcher smoke — v6p0.5 step 1. Verifies the module imports
# cleanly, exposes the three behaviours documented in its public surface
# (gate detection, results.json parsing, classification → real_failure /
# preview_infra), and round-trips the JSON payload contract documented in
# `gate/tools/end2end_gate.py`. We exercise the parser via a small in-process
# Python invocation (no HTTP surface yet — wiring into the iteration
# mechanism is step 2 of the v6p0.5 plan).
if uv run python -c "
import json
from gate.tools.end2end_gate import (
    build_end2end_failure,
    classify_end2end_failure,
    is_end2end_gate,
    is_end2end_ui_gate,
    parse_results_json_from_log,
)

# Gate detection.
assert is_end2end_gate('az/end2end')
assert is_end2end_gate('gcp/end2end-ui')
assert is_end2end_ui_gate('gcp/end2end-ui')
assert not is_end2end_gate('lint')

# Canonical PR #58 shape — preview-infra classification, non-actionable.
results = {
    'success': False,
    'summary': '1/4 checks passed',
    'tests': [
        {'name': '00-seed', 'status': 'pass'},
        {'name': '01-smoke', 'status': 'fail',
         'message': 'GET /health/live HTTP 000 FAIL'},
    ],
}
log = '+ ./end2end/01-smoke.sh\n' + json.dumps(results) + '\nexit 1\n'

parsed = parse_results_json_from_log(log)
assert parsed is not None and parsed['summary'] == '1/4 checks passed'
assert classify_end2end_failure(parsed, log) == 'preview_infra'

payload = build_end2end_failure(gate='az/end2end', log_tail=log).to_dict()
assert payload['kind'] == 'end2end_failure'
assert payload['classification'] == 'preview_infra'
assert payload['actionable'] is False
assert payload['failed_tests'][0]['message'].startswith('GET /health/live')
" > /tmp/e2e-end2end-gate.txt 2>&1; then
  echo "✓ end2end_gate watcher (parse, classify, payload contract)"
else
  echo "✗ end2end_gate watcher smoke failed" >&2
  cat /tmp/e2e-end2end-gate.txt >&2
  failures=$((failures + 1))
fi

# pipx-install smoke — the operator-facing "no clone needed" entry point.
# We do NOT drive a real pipx install here (it'd pull from PyPI / GitHub
# every invocation and add ~30s of dep-resolution); instead we verify
# the `pipx install <wheel>` path can resolve the wheel build from
# this source tree. ``hatchling`` is the declared build backend, so
# `uv build` produces the same wheel pipx would install.
if command -v uv >/dev/null 2>&1; then
  build_dir="$(mktemp -d)"
  if uv build --out-dir "${build_dir}" > /tmp/e2e-wheel-build.txt 2>&1; then
    wheel_count=$(find "${build_dir}" -name 'leartech_automated_agent-*.whl' | wc -l | tr -d ' ')
    if [ "${wheel_count}" = "1" ]; then
      echo "✓ wheel build (pipx-installable artifact present in ${build_dir})"
    else
      echo "✗ wheel build: expected exactly 1 .whl, found ${wheel_count}" >&2
      ls -la "${build_dir}" >&2 || true
      failures=$((failures + 1))
    fi
  else
    echo "✗ wheel build (uv build) exited non-zero" >&2
    cat /tmp/e2e-wheel-build.txt >&2
    failures=$((failures + 1))
  fi
  rm -rf "${build_dir}"
else
  echo "· uv not on PATH — skipping wheel-build smoke (CI image only)"
fi

if [ "${failures}" -gt 0 ]; then
  echo
  echo "✗ ${failures} e2e check(s) failed" >&2
  exit 1
fi

echo
echo "✓ all e2e checks passed"
