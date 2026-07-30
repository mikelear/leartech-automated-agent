# Dashboard ↔ Loki contract test

`test_dashboard_queries.py` submits every panel target and every
`label_values()` inner selector from the three shipped Grafana dashboards
(`charts/leartech-automated-agent/dashboards/*.json`) to a live Loki, and
asserts:

- No query errors, times out, or returns an `__error__` label on any
  stream — the `__error__` label is what Loki attaches when a `| json` /
  field / parse stage on the pipeline goes wrong. This is exactly the
  class of bug the "no data" panels were exhibiting before the rebuild
  (`event=` vs `eventName=`, controller stream vs maestro stream).
- A tiny curated **sentinel** list of queries returns >0 rows over the
  lookback window (default 1h) — a canary that the maestro
  `eventName="plan.completed"`, loop_hop `maestro_receive`, and agent
  `event="run_end"` vocabulary is actually populating Loki.
- Every OTHER panel: 0 rows is a WARN (printed), not a failure.

## Why it's opt-in / non-blocking today

The test hits a live Loki, so it's environment-sensitive by design. It
would be noise if it ran on every PR before we have confidence in the
sentinel list. Enrolled path:

1. The test module SKIPs unless `LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1`.
2. It also carries `@pytest.mark.dashboards` (registered in
   `pyproject.toml`) so operators can select or exclude it explicitly.
3. **We run it manually a few times first** (see below), then flip it to
   blocking in a follow-up PR once the sentinel list has been proven
   stable.

## Manual invocation

From a shell that can reach the cluster's Loki (e.g. a pod inside the
cluster, or after `kubectl port-forward svc/loki -n jx-observability
3100:3100`):

```sh
# Minimal — uses in-cluster defaults (LOKI_URL, LOKI_NS below).
LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1 \
    uv run pytest -v tests/dashboards/ -s

# Overrides:
LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1 \
LOKI_URL=http://127.0.0.1:3100 \
LOKI_NS=jx-staging \
LOKI_DASHBOARD_TEST_WINDOW=1h \
LOKI_DASHBOARD_TEST_TIMEOUT_S=10 \
    uv run pytest -v tests/dashboards/ -s

# Marker-select from anywhere:
LOKI_ENABLE_DASHBOARD_CONTRACT_TEST=1 \
    uv run pytest -v -m dashboards -s
```

The `-s` flag is important — the summary table (dashboard, panel, rows,
latency_ms, status) is printed to stdout and pytest captures it by
default without `-s`.

## Env vars

| var | default | meaning |
|---|---|---|
| `LOKI_ENABLE_DASHBOARD_CONTRACT_TEST` | (unset) | Opt-in switch. Set to `1`/`true`/`yes` to run. |
| `LOKI_URL` | `http://loki.jx-observability.svc:3100` | Loki base URL (query API is `/loki/api/v1/query_range`). |
| `LOKI_NS` | `jx-staging` | Namespace substituted for `$ns` in dashboard queries. |
| `LOKI_DASHBOARD_TEST_WINDOW` | `1h` | Fixed lookback window; replaces `$__range` and `$__interval`. |
| `LOKI_DASHBOARD_TEST_TIMEOUT_S` | `10` | Per-query timeout — a hang is a failure. |

## When to flip to blocking

Once (a) 3-5 manual runs land clean AND (b) the sentinel list stops
producing 0-row surprises in normal traffic, promote by:

1. Remove the `LOKI_ENABLE_DASHBOARD_CONTRACT_TEST` opt-in skip in
   `test_dashboard_queries.py`.
2. Add a step to the PR pipeline (or end2end) that sets the env vars
   and invokes `uv run pytest -m dashboards`.
3. Document the promotion in the calibration lesson thread.

Until then: this test's failure modes must NEVER block a PR.
