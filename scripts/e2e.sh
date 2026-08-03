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
# Auth-hardening C1: the app now boots fail-closed by default —
# LEARTECH_AUTH_REQUIRED defaults to true. Local dev/CI has no Hydra to
# validate against, so opt out explicitly. The dedicated auth-mode smoke
# block near the end of this script starts a SECOND uvicorn with
# required=true to exercise the fail-closed + bypass + 401 contract.
env LEARTECH_AUTH_REQUIRED=false \
  uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --log-level warning &
server_pid=$!

# Poll /health until the server starts answering, up to ~15 s. The router
# registers /health + /healthz + /readyz (see app/routers/health.py); the
# legacy /health/live convention some scripts still reference isn't a
# real route.
for _ in $(seq 1 30); do
  if curl -sf "${BASE_URL}/health" >/dev/null; then
    break
  fi
  sleep 0.5
done

if ! curl -sf "${BASE_URL}/health" >/dev/null; then
  echo "✗ server never reached /health; aborting" >&2
  exit 1
fi
echo "✓ /health ready"

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
# FastAPI's default JSONResponse encodes without spaces after ':' — the
# earlier expectation of `"service": "leartech-automated-agent"` (with a
# space) missed the actual payload. Match the compact form the framework
# emits; anything with pretty-printing (indent > 0) would still contain
# this substring too.
grep -q '"service":"leartech-automated-agent"' /tmp/e2e-health-detail.json \
  || { echo "✗ /health/detail: service field missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-mcps.json -w '%{http_code}' "${BASE_URL}/mcps")
assert_status 'GET /mcps' 200 "${status}"
# leartech-jx3-flow is the remote PR-check MCP (replacement for the retired
# in-process leartech-pipeline shim). leartech-criteria remains an in-process
# SDK MCP and anchors the sdk_import probe smoke below.
grep -q '"leartech-jx3-flow"' /tmp/e2e-mcps.json \
  || { echo "✗ /mcps: leartech-jx3-flow missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-roles.json -w '%{http_code}' "${BASE_URL}/roles")
assert_status 'GET /roles' 200 "${status}"
grep -q '"initiative_agent"' /tmp/e2e-roles.json \
  || { echo "✗ /roles: initiative_agent missing" >&2; failures=$((failures + 1)); }

status=$(curl -s -o /tmp/e2e-topo.json -w '%{http_code}' "${BASE_URL}/topology")
assert_status 'GET /topology' 200 "${status}"
grep -q 'Phase 1' /tmp/e2e-topo.json \
  || { echo "✗ /topology: Phase 1 marker missing" >&2; failures=$((failures + 1)); }

# Active probe on an sdk-type MCP — must report 'ready' + sdk_import probe kind.
# leartech-criteria is still an in-process SDK MCP (leartech-pipeline was
# ported to remote leartech-jx3-flow whose probe uses HTTP, not sdk_import).
status=$(curl -s -o /tmp/e2e-mcp-health.json -w '%{http_code}' \
  "${BASE_URL}/mcps/leartech-criteria/health")
assert_status 'GET /mcps/leartech-criteria/health' 200 "${status}"
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

# v6p0.5 step 2 — iteration-loop watcher. Verifies the decision module
# imports cleanly, dispatches the canonical decisions (real_failure →
# RESPAWN, preview_infra → SKIP_INFRA, max_iterations → ESCALATE),
# rebuilds feedback_payloads in the v6p0.5 contract shape, and renders
# the prompt block with screenshot fetch guidance. We exercise it via
# a small in-process Python invocation; HTTP wiring for re-spawn lives
# in step 3 of the v6p0.5 plan and will get its own e2e exercise then.
if uv run python -c "
from gate.tools.end2end_gate import End2EndFailure, End2EndTest
from gate.tools.playwright_artifacts import Artifact
from gate.watcher.iteration_loop import (
    DEFAULT_MAX_ITERATIONS,
    IterationContext,
    IterationDecision,
    build_feedback_payloads,
    decide_action,
    failure_idempotency_key,
    format_feedback_payloads_for_prompt,
    manual_fix_label,
)

# Canonical PR #58 shape: all-infra → SKIP_INFRA on first occurrence.
infra = End2EndFailure(
    gate='az/end2end',
    classification='preview_infra',
    summary='1/4 checks passed',
    failed_tests=(
        End2EndTest(name='01-smoke', status='fail', message='HTTP 000 FAIL'),
    ),
    actionable=False,
)
ctx_infra = IterationContext(
    repo='mikelear/leartech-auth-service',
    pr_number=58,
    failures=(infra,),
    iteration_count=0,
)
assert decide_action(ctx_infra) == IterationDecision.SKIP_INFRA

# Real failure → RESPAWN; iteration count past ceiling → ESCALATE.
real = End2EndFailure(
    gate='az/end2end',
    classification='real_failure',
    summary='2/3 checks passed',
    failed_tests=(
        End2EndTest(name='03-list', status='fail', message='expected 200 got 500'),
    ),
    actionable=True,
)
ctx_real = IterationContext(
    repo='mikelear/leartech-auth-service',
    pr_number=58,
    failures=(real,),
    iteration_count=0,
)
assert decide_action(ctx_real) == IterationDecision.RESPAWN

ctx_max = IterationContext(
    repo='mikelear/leartech-auth-service',
    pr_number=58,
    failures=(real,),
    iteration_count=DEFAULT_MAX_ITERATIONS,
    max_iterations=DEFAULT_MAX_ITERATIONS,
)
assert decide_action(ctx_max) == IterationDecision.ESCALATE_MAX_ITERATIONS

# Idempotency: a key in already_handled_keys filters that failure out.
already = frozenset({failure_idempotency_key(real)})
ctx_handled = IterationContext(
    repo='mikelear/leartech-auth-service',
    pr_number=58,
    failures=(real,),
    iteration_count=0,
    already_handled_keys=already,
)
assert decide_action(ctx_handled) == IterationDecision.NOOP

# Feedback payloads + prompt block: screenshot URLs surface with fetch
# guidance, ai-review findings merge into the same list.
ui_failure = End2EndFailure(
    gate='gcp/end2end-ui',
    classification='real_failure',
    summary='2/3 browser tests passed',
    failed_tests=(End2EndTest(name='01-login', status='fail', message='locator not visible',
                              screenshot_url='https://artifacts.example/login.png'),),
    actionable=True,
    artifact_urls=(Artifact(spec_name='01-login', kind='screenshot',
                            url='https://artifacts.example/login.png', cluster='gcp'),),
)
payloads = build_feedback_payloads(
    [ui_failure],
    ai_review_findings=[{'cluster': 'gcp', 'verdict': 'Needs Work', 'score': 65}],
)
assert payloads[0]['kind'] == 'end2end_failure'
assert payloads[1]['kind'] == 'ai_review_finding'
block = format_feedback_payloads_for_prompt(payloads)
assert 'Previous-attempt failure context' in block
assert 'https://artifacts.example/login.png' in block
assert 'curl' in block.lower()
assert 'AI code review' in block

# Label name is part of the merge-contract; surfacing here so a typo
# elsewhere can't silently break Lighthouse Keeper's hold semantics.
assert manual_fix_label() == 'do-not-merge/manual-fix'
" > /tmp/e2e-iteration-loop.txt 2>&1; then
  echo "✓ iteration_loop watcher (decisions, payloads, prompt block, idempotency)"
else
  echo "✗ iteration_loop watcher smoke failed" >&2
  cat /tmp/e2e-iteration-loop.txt >&2
  failures=$((failures + 1))
fi

# v6p0.6 step 1 — structured artefact parsers (SARIF / JUnit / Trivy /
# govulncheck / coverage / Playwright / results.json). Verifies that the
# parser registry imports cleanly, every documented artefact type has a
# parser registered, the gate-name → artefact-type resolution covers the
# canonical Tekton gates, and the dispatcher returns a GateFailure payload
# in the contract shape from the initiative goal. The fixtures here are the
# same minimal shapes the unit tests use (see
# tests/test_structured_artefact_parsers.py), exercised via the deployed
# wheel so a packaging miss (missing __init__.py, wrong wheel includes)
# fails this script — exactly the gap scripts/e2e.sh is meant to catch per
# `every-initiative-extends-the-e2e-script` lesson.
if uv run python -c "
import json
from gate.tools.parsers import (
    ARTEFACT_PARSERS,
    GATE_TO_ARTEFACT_TYPE,
    GateFailure,
    parse_gate_artefact,
    parse_gate_artefact_auto,
    parse_sarif,
    parse_junit_xml,
    parse_trivy_json,
    parse_govulncheck_json,
    parse_coverage_json,
    parse_playwright_json,
    parse_results_json,
    resolve_artefact_type,
)
from gate.watcher.artefact_dispatch import dispatch_structured_failure

# Registry covers every documented artefact type.
assert set(ARTEFACT_PARSERS) == {
    'sarif', 'junit', 'results_json', 'coverage_json',
    'trivy_json', 'govulncheck_json', 'playwright_json',
}

# Each gate→type mapping points at a real parser.
for gate, atype in GATE_TO_ARTEFACT_TYPE.items():
    assert atype in ARTEFACT_PARSERS, (gate, atype)

# Gate-name resolution handles cluster prefixes both ways.
assert resolve_artefact_type('security-scan') == 'sarif'
assert resolve_artefact_type('gcp/security-scan') == 'sarif'
assert resolve_artefact_type('az/end2end-ui') == 'playwright_json'
assert resolve_artefact_type('lint') is None  # unmapped → fall-back

# SARIF — score-based severity inference (security-severity → critical).
sarif = json.dumps({
    'version': '2.1.0',
    'runs': [{
        'tool': {'driver': {'name': 'trivy'}},
        'results': [{
            'ruleId': 'CVE-2024-9999',
            'level': 'error',
            'message': {'text': 'demo'},
            'properties': {'security-severity': '9.8'},
            'locations': [{'physicalLocation': {
                'artifactLocation': {'uri': 'lib/x'},
                'region': {'startLine': 1},
            }}],
        }],
    }],
})
findings = parse_sarif(sarif)
assert len(findings) == 1 and findings[0].severity == 'critical'

# JUnit XML — one failure, one skipped.
junit_xml = '''<?xml version=\"1.0\"?>
<testsuites><testsuite name=\"t\">
  <testcase classname=\"c\" name=\"ok\"/>
  <testcase classname=\"c\" name=\"bad\"><failure type=\"AssertionError\" message=\"boom\">stack</failure></testcase>
  <testcase classname=\"c\" name=\"skip\"><skipped message=\"ci-only\"/></testcase>
</testsuite></testsuites>'''
findings = parse_junit_xml(junit_xml)
assert len(findings) == 2  # failure + skipped

# Trivy native JSON — vuln + misconfig.
trivy = json.dumps({
    'Results': [{
        'Target': 'alpine',
        'Vulnerabilities': [{
            'VulnerabilityID': 'CVE-2024-1',
            'PkgName': 'openssl',
            'InstalledVersion': '1.0',
            'Severity': 'HIGH',
            'Title': 'demo',
        }],
        'Misconfigurations': [{'ID': 'AVD-1', 'Severity': 'LOW', 'Title': 'as root'}],
    }],
})
findings = parse_trivy_json(trivy)
assert len(findings) == 2

# govulncheck — CALLED finding has trace[0].position; severity high.
govuln = '\\n'.join([
    json.dumps({'osv': {'id': 'GO-1', 'summary': 's', 'references': []}}),
    json.dumps({'finding': {
        'osv': 'GO-1',
        'trace': [{
            'function': {'name': 'pkg.Fn'},
            'position': {'filename': 'main.go', 'line': 1},
        }],
    }}),
])
findings = parse_govulncheck_json(govuln)
assert len(findings) == 1
assert findings[0].severity == 'high'
assert findings[0].extra['called'] is True

# Coverage JSON — total below threshold + per-file findings.
coverage = json.dumps({
    'totals': {'percent_covered': 60.0},
    'files': {'a.py': {'summary': {'percent_covered': 10.0, 'missing_lines': [1, 2]}}},
})
findings = parse_coverage_json(coverage, threshold=80.0)
assert any(f.location == '<total>' for f in findings)
assert any(f.location == 'a.py' for f in findings)

# Playwright JSON — flat suite with one failure (with attachments).
pw = json.dumps({
    'suites': [{
        'title': 'login.spec.ts', 'file': 'login.spec.ts',
        'specs': [{
            'title': 'shows', 'file': 'login.spec.ts',
            'tests': [{
                'results': [{
                    'status': 'failed',
                    'error': {'message': 'timeout'},
                    'attachments': [{'name': 'screenshot', 'url': 'https://x/p.png'}],
                }],
            }],
        }],
    }],
})
findings = parse_playwright_json(pw)
assert len(findings) == 1
assert findings[0].extra['screenshot_urls'] == ['https://x/p.png']

# results.json — v6p0.5 PR #58 shape still parses through the new wrapper.
results = json.dumps({
    'success': False,
    'summary': '0/1 checks passed',
    'tests': [{'name': '01-smoke', 'status': 'fail', 'message': 'HTTP 000 FAIL'}],
})
findings = parse_results_json(results)
assert len(findings) == 1 and findings[0].location == '01-smoke'

# Dispatcher: gate → artefact-type → parser → GateFailure payload.
gf = parse_gate_artefact(gate='az/security-scan', artefact_type='sarif', content=sarif)
assert gf is not None
assert gf.gate == 'az/security-scan'
assert gf.artefact_type == 'sarif'
assert gf.actionable is True
payload = gf.to_dict()
assert payload['kind'] == 'gate_failure'
assert payload['top_severity'] == 'critical'

# Auto-resolve via gate name alone.
gf2 = parse_gate_artefact_auto(gate='gcp/security-scan', content=sarif)
assert gf2 is not None and gf2.artefact_type == 'sarif'

# Watcher seam — dispatch_structured_failure with an injected fetcher.
calls = []
def fake_fetch(gate, prun, cluster):
    calls.append((gate, prun, cluster))
    return sarif.encode('utf-8')
gf3 = dispatch_structured_failure(
    gate='az/security-scan',
    pipelinerun_name='svc-pr1-abc',
    cluster='az',
    artefact_fetcher=fake_fetch,
)
assert gf3 is not None and gf3.artefact_type == 'sarif'
assert calls == [('az/security-scan', 'svc-pr1-abc', 'az')]

# Fall-through: unmapped gate → None (caller uses heuristic dispatcher).
def must_not_be_called(*_):  # type: ignore[no-untyped-def]
    raise AssertionError('fetcher must not be called for unmapped gate')
assert dispatch_structured_failure(
    gate='gcp/lint',
    pipelinerun_name='x',
    cluster='gcp',
    artefact_fetcher=must_not_be_called,
) is None

# Soft-fail: fetcher raises → dispatcher returns None (heuristic fallback).
def raises_fetch(*_):  # type: ignore[no-untyped-def]
    raise RuntimeError('kubectl context error')
assert dispatch_structured_failure(
    gate='az/security-scan',
    pipelinerun_name='x',
    cluster='az',
    artefact_fetcher=raises_fetch,
) is None
" > /tmp/e2e-artefact-parsers.txt 2>&1; then
  echo "✓ structured artefact parsers (sarif/junit/trivy/govulncheck/coverage/playwright/results_json + dispatcher)"
else
  echo "✗ structured artefact parsers smoke failed" >&2
  cat /tmp/e2e-artefact-parsers.txt >&2
  failures=$((failures + 1))
fi

# v6p0.6 step 2 — extended gate dispatcher. Verifies that govulncheck,
# dynamic-scan severity split, and Helm preview-deploy subclasses route
# to the right action (fix_code / escalate / retry) rather than falling
# through to the generic ``security_scan_finding`` / ``preview_deploy_failure``
# escalate buckets. The classifier is pure; no HTTP / cluster needed —
# just an in-process Python invocation.
if uv run python -c "
from gate.agent.step_failure_diagnosis import (
    ACTION_ESCALATE,
    ACTION_FIX_CODE,
    ACTION_RETRY,
    classify_step_failure,
)

# govulncheck — fix_code (module bump)
f = classify_step_failure('govulncheck',
    'Vulnerability #1: GO-2024-3107\nYour code is affected by 1 vulnerability')
assert f.classification == 'govulncheck_vulnerability'
assert f.action == ACTION_FIX_CODE

# dynamic-scan HIGH — fix_code
f = classify_step_failure('dynamic-scan',
    'High (Medium): Cross-Site Scripting (Reflected) [40012]')
assert f.classification == 'dynamic_scan_high_finding'
assert f.action == ACTION_FIX_CODE

# dynamic-scan LOW — escalate
f = classify_step_failure('dynamic-scan',
    'Low (Medium): X-Content-Type-Options Header Missing [10021]')
assert f.classification == 'dynamic_scan_low_finding'
assert f.action == ACTION_ESCALATE

# Helm missing-value — fix_code (chart patch)
f = classify_step_failure('helm-promote',
    'Error: INSTALLATION FAILED: nil pointer evaluating interface {}.repository '
    'at <.Values.image.repository>')
assert f.classification == 'helm_missing_value'
assert f.action == ACTION_FIX_CODE

# Helm missing-secret — escalate (operator must seed)
f = classify_step_failure('helm-promote',
    'Error: INSTALLATION FAILED: secrets \"preview-db-creds\" not found')
assert f.classification == 'helm_missing_secret'
assert f.action == ACTION_ESCALATE

# Helm timeout — retry (transient rollout race)
f = classify_step_failure('helm-promote',
    'Error: UPGRADE FAILED: timed out waiting for the condition')
assert f.classification == 'helm_timeout'
assert f.action == ACTION_RETRY
" > /tmp/e2e-extended-dispatch.txt 2>&1; then
  echo "✓ extended gate dispatcher (govulncheck, dynamic-scan severity, helm subclasses)"
else
  echo "✗ extended gate dispatcher smoke failed" >&2
  cat /tmp/e2e-extended-dispatch.txt >&2
  failures=$((failures + 1))
fi

# v6p0.6 step 4 — ai-review auto-iterate on red findings. Verifies the
# extended parser exposes structured red findings, the watcher's pure
# decision module maps verdicts to RESPAWN / ESCALATE_LOW_CONFIDENCE / NOOP
# correctly across the canonical scenarios (all-green / yellow-only /
# red+high-score / red+low-score), the structured payload round-trips
# through build_feedback_payloads, and idempotency holds (same verdict =
# no double-iterate, superseded verdict = re-evaluate). Same in-process
# Python pattern as the v6p0.5 step-2 smoke above.
if uv run python -c "
from gate.tools.ai_review import (
    AIReviewFinding,
    AIReviewVerdict,
    parse_ai_review_comment,
    parse_ai_review_findings,
)
from gate.watcher.ai_review_iteration import (
    SCORE_CONFIDENCE_THRESHOLD,
    AIReviewDecision,
    AIReviewIterationContext,
    build_ai_review_failure_payload,
    build_ai_review_failure_payloads,
    decide_ai_review_action,
    format_ai_review_failure_payload,
    verdict_idempotency_key,
)
from gate.watcher.iteration_loop import (
    build_feedback_payloads,
    format_feedback_payloads_for_prompt,
)

# Parser: extracts only Issues Found bullets, not Suggestions.
body = '''## :warning: AI Code Review: **95/100 — Good** \`[gcp]\`

### Issues Found

- :red_circle: [claude] \`a.go:1\` real-issue

### Suggestions

- :red_circle: [claude] \`b.go:9\` looks-like-an-issue-but-isnt
'''
findings = parse_ai_review_findings(body)
assert len(findings) == 1, findings
assert findings[0].severity == 'red'
assert findings[0].location == 'a.go:1'

verdict = parse_ai_review_comment(body)
assert verdict is not None
assert verdict.score == 95
assert len(verdict.red_findings) == 1

# Decision matrix:
# - all green: NOOP
green = parse_ai_review_comment('## :white_check_mark: AI Code Review: **95/100 — Excellent** \`[gcp]\`\n')
assert green is not None
ctx_green = AIReviewIterationContext(repo='m/r', pr_number=1, verdicts=(green,))
assert decide_ai_review_action(ctx_green) == AIReviewDecision.NOOP

# - red + score 95 (≥86): RESPAWN
ctx_high = AIReviewIterationContext(repo='m/r', pr_number=1, verdicts=(verdict,))
assert decide_ai_review_action(ctx_high) == AIReviewDecision.RESPAWN

# - red + score 70 (<86): ESCALATE_LOW_CONFIDENCE (Class A)
low_body = '''## :warning: AI Code Review: **70/100 — Needs Work** \`[az]\`
### Issues Found

- :red_circle: [claude] \`Dockerfile:27\` Hardcoded secret
'''
low = parse_ai_review_comment(low_body)
assert low is not None and low.score < SCORE_CONFIDENCE_THRESHOLD
ctx_low = AIReviewIterationContext(repo='m/r', pr_number=1, verdicts=(low,))
assert decide_ai_review_action(ctx_low) == AIReviewDecision.ESCALATE_LOW_CONFIDENCE

# Idempotency: same verdict, key in handled set → NOOP.
key = verdict_idempotency_key(verdict)
ctx_handled = AIReviewIterationContext(
    repo='m/r', pr_number=1, verdicts=(verdict,),
    already_handled_keys=frozenset({key}),
)
assert decide_ai_review_action(ctx_handled) == AIReviewDecision.NOOP

# Superseded verdict: same cluster, different finding text → different key.
superseded_body = '''## :warning: AI Code Review: **95/100 — Good** \`[gcp]\`
### Issues Found

- :red_circle: [claude] \`a.go:1\` real-issue UPDATED with more context
'''
superseded = parse_ai_review_comment(superseded_body)
assert superseded is not None
assert verdict_idempotency_key(superseded) != verdict_idempotency_key(verdict)

# Payload contract: kind='ai_review_failure', carries red findings.
payload = build_ai_review_failure_payload(verdict)
assert payload['kind'] == 'ai_review_failure'
assert payload['cluster'] == 'gcp'
assert payload['score'] == 95
assert len(payload['red_findings']) == 1
assert payload['red_findings'][0]['location'] == 'a.go:1'

# build_ai_review_failure_payloads filters non-red verdicts.
payloads = build_ai_review_failure_payloads([green, verdict])
assert len(payloads) == 1

# build_feedback_payloads routes ai_review_failures into the output list.
out = build_feedback_payloads([], ai_review_failures=[payload])
assert len(out) == 1
assert out[0]['kind'] == 'ai_review_failure'

# Prompt-block rendering surfaces the red finding's location + fix hint.
block = format_feedback_payloads_for_prompt([payload])
assert 'AI code review (gcp)' in block
assert 'a.go:1' in block
assert 'Red findings' in block
" > /tmp/e2e-ai-review-iterate.txt 2>&1; then
  echo "✓ ai-review auto-iterate (red findings → respawn / escalate / noop, idempotency, payload contract)"
else
  echo "✗ ai-review auto-iterate smoke failed" >&2
  cat /tmp/e2e-ai-review-iterate.txt >&2
  failures=$((failures + 1))
fi

# GET /initiatives/catalog pagination — filesystem-fallback path (no DB
# configured on the e2e laptop server). Verifies the fix landed in the
# initiative that added `limit` + `offset` params: the endpoint must
# honour both so the orchestrator's paginated catalog-walk terminates
# on a short final page instead of looping to its cap. Because there's
# no DB here, the response comes from `initiatives/*.yaml` — same
# request/response shape as the DB path per the initiative's
# "identical regardless of is_db_enabled()" contract.
if command -v python3 >/dev/null 2>&1; then
  if python3 - "${BASE_URL}" <<'PY' > /tmp/e2e-catalog-pagination.txt 2>&1; then
import json
import sys
import urllib.request

base = sys.argv[1]


def get(path: str) -> tuple[int, list]:
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        return resp.status, json.load(resp)


# Full catalog first — establishes the total row count for this repo.
status, full = get('/initiatives/catalog?limit=1000&offset=0')
assert status == 200, status
total = len(full)
assert total >= 1, f'no filesystem initiatives visible; expected initiatives/*.yaml to seed. got: {full}'

# Small page must return exactly `limit` when limit < total; a partial
# page when limit >= remaining. Pick a limit small enough to prove
# truncation but large enough that a small local `initiatives/` still
# has more rows than fit in one page.
limit = max(1, min(3, total - 1)) if total > 1 else 1
status, page = get(f'/initiatives/catalog?limit={limit}&offset=0')
assert status == 200
if total > limit:
    assert len(page) == limit, f'expected {limit} rows, got {len(page)}: {[r["name"] for r in page]}'

# Offset must skip the right rows — the paged view of full[offset:offset+limit]
# must match the sliced view of the full listing.
if total > limit:
    status, offset_page = get(f'/initiatives/catalog?limit={limit}&offset=1')
    assert status == 200
    got_names = [r['name'] for r in offset_page]
    want_names = [r['name'] for r in full[1 : 1 + limit]]
    assert got_names == want_names, f'offset slice mismatch: got {got_names}, want {want_names}'

# Walk must terminate — this is the anti-regression for the shipped
# bug. Increasing offsets, up to a safety cap, must eventually see a
# page shorter than `limit`.
offset = 0
seen: list[str] = []
for _ in range(50):  # cap
    status, p = get(f'/initiatives/catalog?limit={limit}&offset={offset}')
    assert status == 200
    seen.extend(r['name'] for r in p)
    if len(p) < limit:
        break
    offset += limit
else:
    raise AssertionError('catalog walk did not terminate — pagination broken')

assert len(seen) == len(set(seen)), f'duplicate names across pages: {seen}'

# Out-of-range params must 422 — validated by FastAPI Query bounds.
import http.client
from urllib.parse import urlparse

parsed = urlparse(base)
conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
conn.request('GET', '/initiatives/catalog?limit=0&offset=0')
resp = conn.getresponse()
assert resp.status == 422, f'limit=0 should 422, got {resp.status}'
resp.read()
conn.close()

print('OK: pagination honoured — total=%d, walk terminated with %d unique names' % (total, len(seen)))
PY
    echo "✓ GET /initiatives/catalog pagination (limit/offset honoured, walk terminates)"
  else
    echo "✗ GET /initiatives/catalog pagination smoke failed" >&2
    cat /tmp/e2e-catalog-pagination.txt >&2
    failures=$((failures + 1))
  fi
else
  echo "· python3 not on PATH — skipping catalog pagination smoke"
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

# ── auth-hardening C1 smoke — fail-closed boot + bypass + enforcement ──
# The main server above runs in optional-mode (LEARTECH_AUTH_REQUIRED=false)
# so the endpoint validation tests can drive JSON without minting real
# bearers. Auth hardening's user-facing invariant is the OPPOSITE default:
# in required-mode, missing bearer on a non-bypass path → 401, and bypass
# paths (/health, /healthz, /readyz, /openapi.json, /.well-known/*) still
# reach the handler. We spawn a second uvicorn briefly to prove both.
#
# JWKS isn't fetched at startup — only when a bearer actually arrives — so
# a placeholder issuer resolves without needing a live Hydra. The audience
# is the well-known short name ``automated-agent`` that the chart's C1
# defaults pin (and the orch's C0 already mints for).
AUTH_PORT="${AUTH_PORT:-18081}"
AUTH_URL="http://127.0.0.1:${AUTH_PORT}"
echo
echo "→ auth-hardening C1 smoke on :${AUTH_PORT}"

auth_server_pid=""
auth_cleanup() {
  if [ -n "${auth_server_pid}" ]; then
    kill "${auth_server_pid}" 2>/dev/null || true
    wait "${auth_server_pid}" 2>/dev/null || true
  fi
}

# Start a second server with required=true. The trap will still call the
# main cleanup; append our teardown so both servers die on exit.
env \
  LEARTECH_AUTH_REQUIRED=true \
  LEARTECH_AUTH_ISSUER='https://hydra-jx-staging.jx.leartech.com' \
  LEARTECH_AUTH_AUDIENCE='automated-agent' \
  uv run uvicorn app.main:app --host 127.0.0.1 --port "${AUTH_PORT}" --log-level warning &
auth_server_pid=$!
trap 'cleanup; auth_cleanup' EXIT

# Poll /health (bypass path) instead of /health/live — /health/live is a
# convention some historical scripts still reference but the actual router
# registers /health and /healthz.
auth_ready=0
for _ in $(seq 1 30); do
  if curl -sf "${AUTH_URL}/health" >/dev/null; then
    auth_ready=1
    break
  fi
  sleep 0.5
done

if [ "${auth_ready}" != "1" ]; then
  echo "✗ auth-mode server never became ready — did fail-closed boot refuse?" >&2
  # If load_settings_from_env raised (because issuer/audience were somehow
  # unset), uvicorn would have exited already. Surface the pid state.
  kill -0 "${auth_server_pid}" 2>/dev/null || echo "  (auth uvicorn pid ${auth_server_pid} exited)"
  failures=$((failures + 1))
else
  # bypass path — should be 200 regardless of bearer presence
  status=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH_URL}/health")
  assert_status 'auth-mode: GET /health (bypass) without bearer' 200 "${status}"

  status=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH_URL}/healthz")
  assert_status 'auth-mode: GET /healthz (bypass) without bearer' 200 "${status}"

  # non-bypass path — should be 401 without a bearer
  status=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${AUTH_URL}/initiatives" \
    -H 'content-type: application/json' --data '{}')
  assert_status 'auth-mode: POST /initiatives without bearer' 401 "${status}"

  # non-bypass path — should be 401 on garbage bearer (proves the bearer
  # actually gets validated, not just accepted as "any string")
  status=$(curl -s -o /dev/null -w '%{http_code}' \
    "${AUTH_URL}/mcps" -H 'Authorization: Bearer garbage')
  assert_status 'auth-mode: GET /mcps with garbage bearer' 401 "${status}"

  # non-bypass path — introspection surface must also enforce
  status=$(curl -s -o /dev/null -w '%{http_code}' "${AUTH_URL}/health/detail")
  assert_status 'auth-mode: GET /health/detail without bearer' 401 "${status}"
fi

auth_cleanup
auth_server_pid=""

# ── BA test harness smoke — draft-by-default contract + --dry-run CLI ──
# The BA (Business Analyst) agent is proven WITHOUT firing repo-factory-
# scale work via:
#   1. a pure-function validator (gate/agent/ba_test_harness.py) that
#      enforces the draft-by-default + verification invariants against
#      any BA-authored plan, and
#   2. a `--dry-run` flag on `python -m gate.agent.ba_agent` that
#      validates + summarises a brief WITHOUT calling the gateway.
# This smoke exercises both surfaces against the three golden fixtures
# (tests/testdata/ba_briefs/*) so a packaging regression that omits
# either module OR the fixtures OR the --dry-run wire fails here.
# See docs/BA-TEST-HARNESS.md for the full path.
if uv run python -c "
from pathlib import Path
import yaml
from gate.agent import ba_agent
from gate.agent.ba_test_harness import (
    CANONICAL_VERIFICATION_ACTION,
    VERIFICATION_ACTION_MARKERS,
    PlanShapeError,
    validate_authored_plan,
    validate_authored_plans,
)

fixtures = Path('tests/testdata/ba_briefs')
assert fixtures.is_dir(), f'missing fixtures dir {fixtures} — BA harness broken'
for name in ('infra-remediation', 'new-website', 'cluster-wide-multi-resolve'):
    brief_path = fixtures / name / 'brief.yaml'
    plans_path = fixtures / name / 'expected-plans.yaml'
    assert brief_path.is_file(), f'missing {brief_path}'
    assert plans_path.is_file(), f'missing {plans_path}'
    brief = ba_agent.load_brief(brief_path.read_text())
    plans = yaml.safe_load(plans_path.read_text())
    # Positive: golden fixtures satisfy the harness.
    validate_authored_plans(brief, plans)

# Negative: each invariant is load-bearing.
brief = ba_agent.load_brief((fixtures / 'infra-remediation' / 'brief.yaml').read_text())
plans = yaml.safe_load((fixtures / 'infra-remediation' / 'expected-plans.yaml').read_text())

import copy
missing_hold = copy.deepcopy(plans[0])
missing_hold['spec']['hold'] = False
try:
    validate_authored_plan(brief, missing_hold)
except PlanShapeError as e:
    assert 'not held' in str(e), e
else:
    raise AssertionError('validator failed to flag hold:false')

missing_ann = copy.deepcopy(plans[0])
missing_ann['metadata']['annotations'] = {}
try:
    validate_authored_plan(brief, missing_ann)
except PlanShapeError as e:
    assert 'draft-annotated' in str(e), e
else:
    raise AssertionError('validator failed to flag missing draft annotation')

bad_final_step = copy.deepcopy(plans[0])
bad_final_step['spec']['steps'][-1]['inputs']['action'] = 'push-config'
try:
    validate_authored_plan(brief, bad_final_step)
except PlanShapeError as e:
    assert 'verification' in str(e).lower(), e
else:
    raise AssertionError('validator failed to flag non-verification final step')

print(f'OK: BA harness validated 3 golden fixtures + 3 negative cases; '
      f'canonical action={CANONICAL_VERIFICATION_ACTION!r}, '
      f'markers={len(VERIFICATION_ACTION_MARKERS)}')
" > /tmp/e2e-ba-harness.txt 2>&1; then
  echo "✓ BA test harness (validator + 3 golden fixtures + 3 negative cases)"
else
  echo "✗ BA test harness smoke failed" >&2
  cat /tmp/e2e-ba-harness.txt >&2
  failures=$((failures + 1))
fi

# --dry-run CLI: brief validates + summary prints WITHOUT the gateway.
# Deliberately does NOT set ANTHROPIC_API_KEY — dry-run must not need it.
if env -u ANTHROPIC_API_KEY uv run python -m gate.agent.ba_agent \
     --brief @tests/testdata/ba_briefs/infra-remediation/brief.yaml \
     --dry-run > /tmp/e2e-ba-dryrun.txt 2>&1; then
  grep -q 'no plans authored' /tmp/e2e-ba-dryrun.txt \
    && grep -q 'fix-gcp-foo-service-release' /tmp/e2e-ba-dryrun.txt \
    && grep -q 'PlanRef' /tmp/e2e-ba-dryrun.txt \
    && echo "✓ BA --dry-run CLI (brief validated, summary printed, no gateway call)" \
    || { echo "✗ BA --dry-run: expected markers missing from output" >&2; cat /tmp/e2e-ba-dryrun.txt >&2; failures=$((failures + 1)); }
else
  echo "✗ BA --dry-run: CLI exited non-zero (expected 0 for a valid brief)" >&2
  cat /tmp/e2e-ba-dryrun.txt >&2
  failures=$((failures + 1))
fi

# --dry-run on an INVALID brief exits with an error (proves the flag doesn't silently pass).
if env -u ANTHROPIC_API_KEY uv run python -m gate.agent.ba_agent \
     --brief 'name: x' --dry-run > /tmp/e2e-ba-dryrun-bad.txt 2>&1; then
  echo "✗ BA --dry-run should reject an invalid brief but exited 0" >&2
  cat /tmp/e2e-ba-dryrun-bad.txt >&2
  failures=$((failures + 1))
else
  echo "✓ BA --dry-run rejects invalid brief (missing goal/successCriteria)"
fi

# ── TEST-MODE surface — module imports, guard-strict-boolean, parse rules ──
# Exercises the ``gate.agent.test_mode`` module in-process so a packaging
# regression (missing __init__.py, wrong wheel includes) OR a guard-parse
# regression fails here. Every assertion protects a load-bearing invariant:
#   - Guard defaults OFF and only case-insensitive "true" flips it on.
#   - Flag off + testMode present → parse returns None (a stray directive in
#     a production plan CANNOT no-op a real run).
#   - Flag on + testMode present → parse returns a validated TestModeSpec.
#   - The Initiative loader accepts `testMode:` at the top level and reads
#     the raw dict back through the alias.
#   - run_test_mode returns 0 for Succeeded / 1 for Failed regardless of
#     whether the agent pod has AgentRun CR patching wired.
# See gate/agent/lessons/catalog/every-initiative-extends-the-e2e-script.md
# for the contract this exercise satisfies.
if uv run python -c "
import asyncio
import os

from gate.agent.test_mode import (
    TEST_MODE_ANNOTATION_KEY,
    TEST_MODE_ANNOTATION_VALUE,
    TEST_MODE_GUARD_ENV,
    TestModeSpec,
    is_test_mode_allowed,
    parse_test_mode,
    run_test_mode,
)

# The env-name and annotation shape are wire-contract with the chart + with
# forensic-query tooling. Guard against silent renames here.
assert TEST_MODE_GUARD_ENV == 'LEARTECH_AGENT_TEST_MODE_ALLOWED'
assert TEST_MODE_ANNOTATION_KEY == 'leartech.io/test-mode'
assert TEST_MODE_ANNOTATION_VALUE == 'true'

# Guard defaults OFF.
env_off = {}
assert is_test_mode_allowed(env=env_off) is False
# Only case-insensitive 'true' flips it — permissive parses are the
# exact accident this guard is designed to prevent.
for value in ('false', '1', 'yes', 'True1', ''):
    assert is_test_mode_allowed(env={TEST_MODE_GUARD_ENV: value}) is False, value
for value in ('true', 'True', 'TRUE', ' true '):
    assert is_test_mode_allowed(env={TEST_MODE_GUARD_ENV: value}) is True, value

# Flag off + testMode present → IGNORED. This is the safety invariant.
os.environ.pop(TEST_MODE_GUARD_ENV, None)
assert parse_test_mode({'testMode': {'finishAs': 'Succeeded'}}) is None

# Flag on + testMode present → parsed spec.
os.environ[TEST_MODE_GUARD_ENV] = 'true'
try:
    spec = parse_test_mode({
        'testMode': {
            'finishAs': 'Succeeded',
            'prOutcome': 'merged',
            'message': 'hello',
            'delaySeconds': 2,
        }
    })
    assert spec is not None
    assert spec.finish_as == 'Succeeded'
    assert spec.pr_outcome == 'merged'
    assert spec.delay_seconds == 2
finally:
    os.environ.pop(TEST_MODE_GUARD_ENV, None)

# Initiative loader accepts testMode as a top-level field.
from gate.initiatives import load_initiative_from_yaml
initiative = load_initiative_from_yaml(
    'name: t\nrepo: leartech-x\nbranch: agent/t\ngoal: g\n'
    'testMode:\n  finishAs: Failed\n  prOutcome: closed\n'
)
assert initiative.test_mode == {'finishAs': 'Failed', 'prOutcome': 'closed'}

# run_test_mode exit codes reflect the plan's declared intent regardless of
# whether AgentRun CR patching is available. LEARTECH_RUN_ID + namespace are
# absent here so the patches are skipped — the exit code MUST still be right.
os.environ.pop('LEARTECH_RUN_ID', None)
os.environ.pop('AGENT_RUN_NAMESPACE', None)
async def _run():
    async def no_sleep(_s):
        pass
    ok_spec = TestModeSpec(finishAs='Succeeded', prOutcome='none')
    bad_spec = TestModeSpec(finishAs='Failed', prOutcome='none')
    ok = await run_test_mode(ok_spec, sleep=no_sleep)
    bad = await run_test_mode(bad_spec, sleep=no_sleep)
    return ok, bad
ok, bad = asyncio.run(_run())
assert ok == 0, ok
assert bad == 1, bad

print('OK: test-mode surface (guard-strict + parse + loader + run_test_mode exit codes)')
" > /tmp/e2e-test-mode.txt 2>&1; then
  echo "✓ test-mode surface (guard-strict + parse + loader wiring + run_test_mode exit codes)"
else
  echo "✗ test-mode surface smoke failed" >&2
  cat /tmp/e2e-test-mode.txt >&2
  failures=$((failures + 1))
fi

# ---------------------------------------------------------------------
# Grafana dashboards — vocabulary + shape smoke (fix/dashboard-query-vocabulary).
# ---------------------------------------------------------------------
# This is an OFFLINE check: it does NOT hit Loki. The live-Loki contract
# test lives at tests/dashboards/test_dashboard_queries.py and is opt-in
# (LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1) — see tests/dashboards/README.md.
#
# Here we just assert that the JSON parses AND carries the corrected
# vocabulary the initiative introduced:
#   • plans.json uses `app="leartech-maestro-service"` (maestro stream)
#     with `eventName="plan.completed"` (NOT the old `event=...` on the
#     controller stream that made the panels "no data").
#   • plan-and-loop.json keeps the loop_hop / event_id vocabulary
#     (correct as-is per FIX 3).
#   • agent-runs.json's $run/$run_id dropdowns are constrained to
#     agent-run Job pods (`[0-9a-f]{12}-[a-z0-9]{5}`) so they don't
#     enumerate every pod ever.
if uv run python -c "
import json, re
from pathlib import Path

root = Path('charts/leartech-automated-agent/dashboards')

# 1. All three dashboards parse and are non-empty.
plans = json.loads((root / 'plans.json').read_text())
loop = json.loads((root / 'plan-and-loop.json').read_text())
runs = json.loads((root / 'agent-runs.json').read_text())
assert plans.get('uid') == 'leartech-plans'
assert loop.get('uid') == 'leartech-plan-and-loop'
assert runs.get('uid') == 'leartech-agent-runs'

# 2. plans.json — every non-row panel target queries the MAESTRO stream
#    with the CORRECT vocabulary. This catches a regression back to the
#    old controller-stream / event= form.
plan_targets = [
    t.get('expr', '')
    for pan in plans.get('panels', [])
    for t in pan.get('targets', [])
    if pan.get('type') != 'row'
]
assert plan_targets, 'plans.json: no panel targets discovered'
for expr in plan_targets:
    assert 'app=\"leartech-maestro-service\"' in expr, (
        'plans.json regressed: panel target does not scope to maestro app label:\\n  '
        + expr
    )
    # At least one of eventName= / plan= must appear — every panel is one
    # or the other flavour.
    assert 'eventName=' in expr or 'plan=~' in expr, (
        'plans.json regressed: panel target missing eventName= or plan= filter:\\n  '
        + expr
    )
# Guard the specific vocabulary tokens the initiative fixed.
plans_text = (root / 'plans.json').read_text()
assert 'eventName=\"plan.completed\"' in plans_text, (
    'plans.json regressed: expected eventName=\"plan.completed\" (was event=\"plan.completed\" pre-fix)'
)
# Guard against the OLD form sneaking back in.
assert '| event=\"plan.completed\"' not in plans_text, (
    'plans.json regressed: OLD event=\"plan.completed\" vocabulary reappeared (should be eventName= on the maestro stream)'
)

# 3. plan-and-loop.json — loop_hop vocabulary preserved.
loop_text = (root / 'plan-and-loop.json').read_text()
assert 'loop_hop=\"maestro_receive\"' in loop_text, (
    'plan-and-loop.json regressed: loop_hop=\"maestro_receive\" vocabulary missing'
)
assert 'loop_hop=\"resolved\"' in loop_text, (
    'plan-and-loop.json regressed: loop_hop=\"resolved\" vocabulary missing'
)
# Template vars scoped to maestro app label (FIX 2).
event_id_var = next(v for v in loop['templating']['list'] if v['name'] == 'event_id')
assert 'app=\"leartech-maestro-service\"' in event_id_var['query'], (
    'plan-and-loop.json regressed: $event_id template var not scoped to maestro app label'
)

# 4. agent-runs.json — $run/$run_id dropdowns restricted to Job pods.
runs_vars = {v['name']: v for v in runs['templating']['list']}
for name in ('run', 'run_id'):
    q = runs_vars[name]['query']
    assert '[0-9a-f]{12}-[a-z0-9]{5}' in q, (
        f'agent-runs.json regressed: ${name} template var not restricted to '
        f'agent-run Job pod pattern; would enumerate every pod ever'
    )

# 5. The contract test file exists with the dashboards marker.
contract = Path('tests/dashboards/test_dashboard_queries.py').read_text()
assert 'pytest.mark.dashboards' in contract, (
    'tests/dashboards/test_dashboard_queries.py: dashboards marker missing'
)
assert 'LOKI_ENABLE_DASHBOARD_CONTRACT_TEST' in contract, (
    'tests/dashboards/test_dashboard_queries.py: opt-in env var guard missing'
)
readme = Path('tests/dashboards/README.md').read_text()
assert 'LOKI_ENABLE_DASHBOARD_CONTRACT_TEST' in readme, (
    'tests/dashboards/README.md: manual invocation env var missing'
)

print('OK: dashboards vocabulary + shape smoke (plans/plan-and-loop/agent-runs + contract test)')
" > /tmp/e2e-dashboards.txt 2>&1; then
  echo "✓ dashboards vocabulary + shape smoke (plans/loop/runs + contract test opt-in)"
else
  echo "✗ dashboards vocabulary smoke failed" >&2
  cat /tmp/e2e-dashboards.txt >&2
  failures=$((failures + 1))
fi

# ---------------------------------------------------------------------
# Individual single-stage release-check actions (release-status /
# promote-status / verify-gate / boot-status / deploy-health) —
# decompose release-health-check into five one-stage actions each of
# which passes/fails on its OWN MCP call and, on FAIL, hands the
# spawned BA Agent a stage-specific "where + how to remediate"
# structured context. Guard that the deployed wheel exposes:
#   1. every action's aggregator + verdict path (PASS + FAIL);
#   2. the BA_STAGE_GUIDANCE map covers every registered action;
#   3. the composed release-health-check still works (regression);
#   4. the release-shepherd template exists and chains the 5 actions
#      in dependsOn order.
# ---------------------------------------------------------------------
if uv run python -c "
import yaml
from pathlib import Path

from gate.agent import infra_agent
from gate.agent.release_health import (
    BA_STAGE_GUIDANCE,
    INDIVIDUAL_STAGE_ACTIONS,
    compute_release_health,
    compute_release_status_verdict,
    compute_promote_status_verdict,
    compute_verify_gate_verdict,
    compute_boot_status_verdict,
    compute_deploy_health_verdict,
    is_individual_stage_action,
)

# Every documented action name is registered.
EXPECTED = {'release-status', 'promote-status', 'verify-gate', 'boot-status', 'deploy-health'}
assert set(INDIVIDUAL_STAGE_ACTIONS) == EXPECTED
for name in EXPECTED:
    assert is_individual_stage_action(name)
    assert name in BA_STAGE_GUIDANCE
    guidance = BA_STAGE_GUIDANCE[name]
    for key in ('mcp', 'expected', 'remediation_hint'):
        assert guidance.get(key), f'{name} guidance missing {key}'

# Per-stage aggregators — PASS + FAIL paths pinned in-process.
r = compute_release_status_verdict('STAGE_STATUS: stage=1 cluster=- verdict=PASS reason=released Succeeded')
assert r.verdict == 'PASS' and r.ba_failure_context is None

r = compute_release_status_verdict(
    'STAGE_STATUS: stage=1 cluster=- verdict=FAIL reason=release PipelineRun failed at step kaniko'
)
assert r.verdict == 'FAIL'
assert r.ba_failure_context is not None
assert 'mcp__leartech-jx-release__release_status' in r.ba_failure_context['mcp']

r = compute_promote_status_verdict(
    'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS\nSTAGE_STATUS: stage=2 cluster=az verdict=PASS'
)
assert r.verdict == 'PASS'

r = compute_verify_gate_verdict(
    'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS reason=promote PR #101 merged\\n'
    'STAGE_STATUS: stage=2 cluster=az verdict=FAIL reason=qa-gate failed on promote PR #102'
)
assert r.verdict == 'FAIL'
assert r.ba_failure_context is not None
assert 'retest_promote' in r.ba_failure_context['mcp']

r = compute_boot_status_verdict(
    'STAGE_STATUS: stage=3 cluster=gcp verdict=PASS reason=jx-boot Job succeeded\\n'
    'STAGE_STATUS: stage=3 cluster=az verdict=PASS reason=jx-boot Job succeeded'
)
assert r.verdict == 'PASS'

# deploy-health FAIL — BA context MUST forbid re-introducing httpx probe.
r = compute_deploy_health_verdict(
    'STAGE_STATUS: stage=4 cluster=gcp verdict=FAIL '
    'reason=healthy=false available_replicas=0 desired_replicas=1'
)
assert r.verdict == 'FAIL'
assert r.ba_failure_context is not None
assert 'httpx' in r.ba_failure_context['remediation_hint']

# Composed release-health-check is UNCHANGED — regression guard.
composed = compute_release_health(
    'STAGE_STATUS: stage=1 cluster=- verdict=PASS\\n'
    'STAGE_STATUS: stage=2 cluster=gcp verdict=PASS\\n'
    'STAGE_STATUS: stage=2 cluster=az verdict=PASS\\n'
    'STAGE_STATUS: stage=3 cluster=gcp verdict=PASS\\n'
    'STAGE_STATUS: stage=3 cluster=az verdict=PASS\\n'
    'STAGE_STATUS: stage=4 cluster=gcp verdict=PASS\\n'
    'STAGE_STATUS: stage=4 cluster=az verdict=PASS\\n'
)
assert composed.verdict == 'PASS'

# infra_agent dispatch — _stage_action_verdict routes to the right aggregator.
r = infra_agent._stage_action_verdict(
    'deploy-health', {'clusters': ['gcp']},
    'STAGE_STATUS: stage=4 cluster=gcp verdict=PASS reason=healthy=true available_replicas=2',
)
assert r.verdict == 'PASS'

# Unknown action name fails closed with a specific BA hint.
r = infra_agent._stage_action_verdict('bogus', {}, '')
assert r.verdict == 'FAIL'
assert r.ba_failure_context is not None
assert 'unregistered' in r.ba_failure_context['remediation_hint']

# release-shepherd template exists and chains the 5 actions in dependsOn order.
template = Path('examples/templates/release-shepherd.yaml')
assert template.is_file(), 'examples/templates/release-shepherd.yaml missing'
data = yaml.safe_load(template.read_text())
actions = [step['inputs']['action'] for step in data['spec']['steps']]
assert actions == [
    'release-status', 'promote-status', 'verify-gate', 'boot-status', 'deploy-health',
], actions
steps = data['spec']['steps']
for i in range(1, len(steps)):
    depends_on = steps[i].get('dependsOn') or []
    assert steps[i - 1]['name'] in depends_on, f'step[{i}] missing dependsOn on step[{i - 1}]'

# authoring_capabilities.yaml advertises the 5 new actions as available.
caps = yaml.safe_load(Path('gate/agent/authoring_capabilities.yaml').read_text())
inf = caps['agent_types']['leartech-agent-infra']['actions']
for name in EXPECTED:
    assert inf[name]['status'] == 'available', f'{name} not advertised as available'
# And release-health-check stays available for the single-step case.
assert inf['release-health-check']['status'] == 'available'

print('OK: 5 individual stage actions (release-status/promote-status/verify-gate/boot-status/deploy-health) '
      'wired, BA context complete, template chains dependsOn, composed action unchanged.')
" > /tmp/e2e-release-check-individual.txt 2>&1; then
  echo "✓ individual single-stage release-check actions (5 aggregators, BA context, dependsOn template, composed regression-guard)"
else
  echo "✗ individual single-stage release-check actions smoke failed" >&2
  cat /tmp/e2e-release-check-individual.txt >&2
  failures=$((failures + 1))
fi

if [ "${failures}" -gt 0 ]; then
  echo
  echo "✗ ${failures} e2e check(s) failed" >&2
  exit 1
fi

echo
echo "✓ all e2e checks passed"
