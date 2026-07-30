"""Dashboard ↔ Loki contract test.

Verifies that every panel target in the three shipped Grafana dashboards
(under ``charts/leartech-automated-agent/dashboards/*.json``) is a query
Loki accepts as valid. Concretely:

* Every LogQL expression in every panel target (and every template-var
  ``label_values(...)`` inner selector) is submitted to a live Loki with
  Grafana macros substituted for test defaults.
* A query that errors, exceeds the per-query timeout, or produces an
  ``__error__`` label in any returned stream FAILS the test — the
  ``__error__`` label is exactly what Loki emits when a ``| json`` /
  field / parse stage on the pipeline goes wrong, which is the whole
  class of bug this contract test exists to catch.
* A small curated SENTINEL list of queries MUST return >0 rows against a
  healthy staging Loki (e.g. maestro ``eventName="plan.completed"`` — the
  vocabulary FIX 1 corrects). An empty sentinel FAILS the test.
* For every other panel, zero rows is a WARN (printed) — low traffic
  must never break the gate.

Gate-safety
-----------
The test is **opt-in** and non-blocking. It only runs when
``LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1`` is set in the environment —
CI runs skip it entirely. See ``tests/dashboards/README.md`` for the
exact manual command + required env vars.

It also carries the ``@pytest.mark.dashboards`` marker so the operator
can select or exclude it explicitly (``pytest -m dashboards`` /
``pytest -m 'not dashboards'``).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

DASHBOARDS_DIR = Path(__file__).parents[2] / 'charts' / 'leartech-automated-agent' / 'dashboards'

# Opt-in gate — the test hits a live Loki, so we don't want it running by
# default in the PR pipeline. Set LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1 in
# the shell that invokes pytest to enable.
_ENABLED = os.environ.get('LOKI_ENABLE_DASHBOARD_CONTRACT_TEST', '').strip() in {
    '1',
    'true',
    'yes',
    'on',
}

pytestmark = [
    pytest.mark.dashboards,
    pytest.mark.skipif(
        not _ENABLED,
        reason=(
            'dashboard↔Loki contract test is opt-in — set '
            'LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1 to enable. See '
            'tests/dashboards/README.md for the manual invocation.'
        ),
    ),
]


# ---------------------------------------------------------------------
# Test knobs — env vars with sensible defaults for jx-staging.
# ---------------------------------------------------------------------
LOKI_URL = os.environ.get('LOKI_URL', 'http://loki.jx-observability.svc:3100')
LOKI_NS = os.environ.get('LOKI_NS', 'jx-staging')
QUERY_TIMEOUT_S = float(os.environ.get('LOKI_DASHBOARD_TEST_TIMEOUT_S', '10'))
# Fixed window for all queries so replays are reproducible. Grafana's
# `$__range` / `$__interval` are replaced with this literal.
LOOKBACK_WINDOW = os.environ.get('LOKI_DASHBOARD_TEST_WINDOW', '1h')


# ---------------------------------------------------------------------
# Sentinel queries. Each MUST return >0 rows against a healthy staging
# Loki within the LOOKBACK_WINDOW. An empty sentinel => test FAILS.
# The list is deliberately tiny — we want a canary, not a coverage bar.
# ---------------------------------------------------------------------
SENTINEL_QUERIES: list[tuple[str, str]] = [
    (
        'maestro plan.completed present',
        (
            f'{{namespace="{LOKI_NS}", app="leartech-maestro-service"}} '
            '|~ "plan\\\\.completed" | json | eventName="plan.completed"'
        ),
    ),
    (
        'loop_hop maestro_receive present',
        (f'{{namespace="{LOKI_NS}"}} |~ "loop_hop" | json | loop_hop="maestro_receive"'),
    ),
    (
        'agent run_end event present',
        (f'{{namespace="{LOKI_NS}"}} |~ "run_end" | json | event="run_end"'),
    ),
]


# ---------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------
@dataclass
class QueryTarget:
    """A single LogQL query pulled from a dashboard JSON, with enough
    context to attribute failures back to the source dashboard/panel."""

    dashboard: str
    panel_id: int | None
    panel_title: str
    kind: str  # 'panel_target' | 'template_var'
    expr: str


# Regex — Grafana macros the dashboards use, replaced with test defaults
# so the query is executable outside Grafana.
_MACRO_RANGE = re.compile(r'\$__range')
_MACRO_INTERVAL = re.compile(r'\$__interval')


def _substitute_macros(expr: str) -> str:
    """Replace Grafana template vars and time-range macros with concrete
    values the raw Loki HTTP API accepts.

    The substitutions match Grafana's own resolution rules:
      $ns          → env LOKI_NS (default "jx-staging")
      $plan        → ".+"  (matches all)
      $event_id    → ".+"
      $run         → ".+"  (would resolve to a pod-regex in Grafana; ``.+``
                            is sufficient here — we test the QUERY shape,
                            not Grafana's variable substitution)
      $run_id      → ".+"
      $__range     → LOOKBACK_WINDOW (e.g. "1h")
      $__interval  → LOOKBACK_WINDOW
    """
    expr = expr.replace('$ns', LOKI_NS)
    expr = expr.replace('$plan', '.+')
    expr = expr.replace('$event_id', '.+')
    expr = expr.replace('$run_id', '.+')
    # $run may be enclosed in `pod=~"$run"` — a valid pod-regex substitute
    # is `.+`.
    expr = expr.replace('$run', '.+')
    expr = _MACRO_RANGE.sub(f'{LOOKBACK_WINDOW}', expr)
    expr = _MACRO_INTERVAL.sub(f'{LOOKBACK_WINDOW}', expr)
    return expr


# Regex that pulls the inner selector out of a `label_values(SELECTOR, LABEL)`
# call. Grafana template variables use this form; the SELECTOR half is a
# real LogQL expression we can validate.
_LABEL_VALUES_TWO_ARG = re.compile(
    r'^\s*label_values\s*\(\s*(?P<inner>.+?)\s*,\s*(?P<label>[a-zA-Z_][a-zA-Z0-9_]*)\s*\)\s*$',
    re.DOTALL,
)
# `label_values(labelName)` — no selector, no LogQL to test. Skip.
_LABEL_VALUES_ONE_ARG = re.compile(r'^\s*label_values\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)\s*$')


def _extract_targets(dashboard_path: Path) -> list[QueryTarget]:
    """Discover every LogQL query in a dashboard JSON — both panel
    targets and template-variable ``label_values()`` inner selectors."""
    d = json.loads(dashboard_path.read_text())
    out: list[QueryTarget] = []
    dash_name = dashboard_path.name

    # Template variables — extract the inner selector of each
    # `label_values(<selector>, <label>)`. Skip one-arg forms (no LogQL).
    for var in d.get('templating', {}).get('list', []):
        q = var.get('query')
        if not isinstance(q, str):
            continue
        if _LABEL_VALUES_ONE_ARG.match(q):
            continue
        m = _LABEL_VALUES_TWO_ARG.match(q)
        if not m:
            # Unknown shape — keep the raw string, let Loki decide.
            out.append(
                QueryTarget(
                    dashboard=dash_name,
                    panel_id=None,
                    panel_title=f'template-var ${var.get("name", "?")}',
                    kind='template_var',
                    expr=q,
                )
            )
            continue
        out.append(
            QueryTarget(
                dashboard=dash_name,
                panel_id=None,
                panel_title=f'template-var ${var.get("name", "?")}',
                kind='template_var',
                expr=m.group('inner'),
            )
        )

    # Panel targets — walk every panel, pull each target's `expr`.
    for panel in d.get('panels', []):
        if panel.get('type') == 'row':
            continue
        for target in panel.get('targets', []):
            expr = target.get('expr')
            if not isinstance(expr, str) or not expr.strip():
                continue
            out.append(
                QueryTarget(
                    dashboard=dash_name,
                    panel_id=panel.get('id'),
                    panel_title=panel.get('title', '?'),
                    kind='panel_target',
                    expr=expr,
                )
            )
    return out


def _all_targets() -> list[QueryTarget]:
    out: list[QueryTarget] = []
    for dash in sorted(DASHBOARDS_DIR.glob('*.json')):
        out.extend(_extract_targets(dash))
    return out


# ---------------------------------------------------------------------
# Loki HTTP client (thin — we only need query_range).
# ---------------------------------------------------------------------
def _run_query(expr: str, window: str) -> tuple[dict, float]:
    """Submit a query to Loki's ``/loki/api/v1/query_range`` endpoint.

    Returns ``(json_body, latency_ms)``. Raises on transport / non-2xx.
    We use ``query_range`` (not the instant ``query``) because Grafana
    panels almost always use ``count_over_time(... [$__range])`` shaped
    queries — a range submission is the closest match to how Grafana
    actually calls Loki.
    """
    # `end` is now; `start` is now - window. Window is a Grafana-style
    # duration ("1h", "30m").
    now_ns = int(time.time() * 1_000_000_000)
    match = re.match(r'^(\d+)([smhd])$', window.strip())
    if not match:
        raise ValueError(f'invalid window {window!r}')
    n = int(match.group(1))
    unit = match.group(2)
    seconds = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit] * n
    start_ns = now_ns - (seconds * 1_000_000_000)

    url = LOKI_URL.rstrip('/') + '/loki/api/v1/query_range'
    params = {
        'query': expr,
        'start': str(start_ns),
        'end': str(now_ns),
        'limit': '10',
        'direction': 'backward',
    }
    t0 = time.monotonic()
    with httpx.Client(timeout=QUERY_TIMEOUT_S) as client:
        r = client.get(url, params=params)
    latency_ms = (time.monotonic() - t0) * 1000.0
    r.raise_for_status()
    return r.json(), latency_ms


def _has_error_label(body: dict) -> str | None:
    """Return the ``__error__`` value if any returned stream carries it,
    else None. Loki attaches ``__error__`` to a stream when a pipeline
    stage (`| json`, `| unwrap`, a label filter on a missing field)
    failed on lines in that stream — the exact class of bug the
    initiative is fixing."""
    data = (body or {}).get('data') or {}
    for stream in data.get('result') or []:
        stream_labels = stream.get('stream') or {}
        err = stream_labels.get('__error__')
        if err:
            return str(err)
    return None


def _count_rows(body: dict) -> int:
    data = (body or {}).get('data') or {}
    total = 0
    for stream in data.get('result') or []:
        total += len(stream.get('values') or [])
    return total


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------
def test_dashboard_query_contract() -> None:
    """One test, one Loki interaction per query. Prints a clean summary
    table at the end so a human reading the output sees per-query
    status/latency/rows at a glance.
    """
    targets = _all_targets()
    assert targets, 'no panel targets discovered — dashboards dir empty?'

    rows_report: list[tuple[str, str, int, float, str]] = []
    failures: list[str] = []
    warns: list[str] = []

    for t in targets:
        expr = _substitute_macros(t.expr)
        panel_ref = f'panel id={t.panel_id} "{t.panel_title}"' if t.panel_id is not None else t.panel_title
        try:
            body, latency_ms = _run_query(expr, LOOKBACK_WINDOW)
        except httpx.TimeoutException:
            failures.append(f'{t.dashboard} :: {panel_ref} :: TIMEOUT (>{QUERY_TIMEOUT_S:.0f}s) — expr: {expr}')
            rows_report.append((t.dashboard, panel_ref, -1, QUERY_TIMEOUT_S * 1000.0, 'TIMEOUT'))
            continue
        except (httpx.HTTPError, ValueError) as exc:
            failures.append(f'{t.dashboard} :: {panel_ref} :: transport/HTTP error: {exc} — expr: {expr}')
            rows_report.append((t.dashboard, panel_ref, -1, 0.0, f'ERROR ({exc.__class__.__name__})'))
            continue

        err = _has_error_label(body)
        rows = _count_rows(body)
        if err:
            failures.append(f'{t.dashboard} :: {panel_ref} :: __error__="{err}" (parse/filter mistake) — expr: {expr}')
            rows_report.append((t.dashboard, panel_ref, rows, latency_ms, f'__error__: {err}'))
            continue

        status = 'OK' if rows > 0 else 'WARN (0 rows)'
        if rows == 0:
            warns.append(f'{t.dashboard} :: {panel_ref} :: 0 rows (low traffic; not a failure)')
        rows_report.append((t.dashboard, panel_ref, rows, latency_ms, status))

    # Sentinel — a small curated list that MUST have data.
    sentinel_failures: list[str] = []
    for name, expr in SENTINEL_QUERIES:
        try:
            body, latency_ms = _run_query(expr, LOOKBACK_WINDOW)
        except (httpx.HTTPError, httpx.TimeoutException, ValueError) as exc:
            sentinel_failures.append(f'sentinel "{name}" errored: {exc}')
            rows_report.append(('SENTINEL', name, -1, 0.0, f'ERROR ({exc.__class__.__name__})'))
            continue
        rows = _count_rows(body)
        err = _has_error_label(body)
        if err:
            sentinel_failures.append(f'sentinel "{name}" got __error__="{err}"')
            rows_report.append(('SENTINEL', name, rows, latency_ms, f'__error__: {err}'))
            continue
        if rows == 0:
            sentinel_failures.append(
                f'sentinel "{name}" returned 0 rows over {LOOKBACK_WINDOW} — the vocabulary regressed'
            )
        rows_report.append(('SENTINEL', name, rows, latency_ms, 'OK' if rows > 0 else 'FAIL (0 rows)'))

    # ---------- Print the summary table ----------
    print('\n=== Dashboard ↔ Loki contract summary ===')
    print(f'Loki:   {LOKI_URL}')
    print(f'NS:     {LOKI_NS}')
    print(f'Window: {LOOKBACK_WINDOW}')
    print(f'{"dashboard":<28} {"panel":<58} {"rows":>6} {"lat_ms":>8}  status')
    print('-' * 120)
    for dashboard, panel, rows, latency_ms, status in rows_report:
        rows_disp = '-' if rows < 0 else str(rows)
        print(f'{dashboard:<28} {panel[:58]:<58} {rows_disp:>6} {latency_ms:>8.1f}  {status}')
    if warns:
        print('\nWarnings (0-row panels — low traffic, not a failure):')
        for w in warns:
            print(f'  - {w}')
    if failures or sentinel_failures:
        print('\nFailures:')
        for f in failures + sentinel_failures:
            print(f'  ✗ {f}')

    # ---------- Assertions ----------
    assert not failures, (
        f'{len(failures)} panel queries failed (see summary above): '
        + '; '.join(failures[:3])
        + ('…' if len(failures) > 3 else '')
    )
    assert not sentinel_failures, (
        f'{len(sentinel_failures)} sentinel queries failed (see summary above): '
        + '; '.join(sentinel_failures[:3])
        + ('…' if len(sentinel_failures) > 3 else '')
    )
