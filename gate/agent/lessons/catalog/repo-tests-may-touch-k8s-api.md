---
id: repo-tests-may-touch-k8s-api
title: Before running a repo's test suite, assess whether its tests can reach the live Kubernetes API — a live pod carries live credentials
captured_at: 2026-08-15T00:00:00Z
source:
  type: prod_incident
  reference: leartech-automated-agent-agentrun-status-stomp-2026-08-15
  observer: mike@leartech
  latency_to_capture: hours
category: calibration
applies_to:
  - initiative_agent
status: encoded
encoded_in:
  - gate/agent/lessons/catalog/repo-tests-may-touch-k8s-api.md
  - gate/agent/lessons/catalog/pre-push-validation.md
encoded_at: 2026-08-15T00:00:00Z
---

BEFORE running a repo's test suite (pytest, go test, npm test, cargo test),
**assess whether the tests can reach the live Kubernetes API or any other
ambient production credential**. Your pod carries live AgentRun identity
(`AGENT_RUN_NAME` / `AGENT_RUN_NAMESPACE` / `LEARTECH_AGENTRUN_STATUS`) and
real credentials (`GH_TOKEN`, gateway keys). A test that constructs a real
client can act on live cluster state — **including your own AgentRun**.

## Cheap checks before invoking the suite

- Does the repo import a k8s client? Grep for `kubernetes`, `kubernetes_asyncio`,
  `client-go`, `k8s.io/client-go`, `@kubernetes/client-node`.
- Do the tests use a **fake** client (controller-runtime `fake.NewClientBuilder`,
  `kubernetes.client.CustomObjectsApi` mocked, etc.) or a **real** one?
- Does anything call `load_incluster_config()` / `rest.InClusterConfig()` /
  equivalent? If yes, that path is dormant on a laptop (fails silently) but
  **active in your pod**.

## Why the hazard is non-obvious

- On a laptop the test looks hermetic: `load_incluster_config()` raises, the
  code skips, tests pass.
- In your pod the same call succeeds, and the test uses the pod's mounted
  ServiceAccount to hit the API server. `managedFields` on any object it
  writes will be stamped with that SA — not the controller that "owns" the
  resource.
- The hazard is NOT limited to Kubernetes. `GH_TOKEN` is projected into every
  agent pod, so a test that shells `gh` acts on real repositories. A test
  that reads `LEARTECH_AI_GATEWAY_TOKEN` and makes calls burns budget.

## Language conventions ≠ safety

Go repos are **safer by convention, not by construction**. The
controller-runtime fake client is the house pattern, but a Go test that
constructs a real clientset in-cluster behaves identically to Python. Do not
assume language implies safety — read the test bootstrap before running.

## What to do when you knowingly run such a suite

**LOG LOUDLY.** Say which repo, that its tests may touch cluster APIs, and
what you did about it. A future forensic reader needs that line to exist.
Post it in the PR sticky's pre-push section, e.g.:

```
⚠️ Tests in this repo import kubernetes_asyncio and one path calls
   load_incluster_config(). Ran pytest inside pod anyway; test suite uses
   a mocked AgentRun client (verified in tests/conftest.py::_no_k8s).
```

## Known instance — leartech-automated-agent

This repo **implements** AgentRuns, so its tests exercise the k8s patch path
by definition. Identity is now stripped from subprocess environments before
the Bash tool runs (`gate.identity.capture_and_strip`, PR #218), so a test
can no longer reach the live run. **If AgentRun status still looks wrong
after running this repo's suite, that is a REGRESSION in that guard, not the
original bug.** Verify the strip fired: check obslog for the `identity_stripped`
event or confirm `AGENT_RUN_NAME` is absent from the pytest subprocess env.

## Diagnostic ladder — when a cluster resource looks foreign-written

This is the durable part. Ordered by cost; go top to bottom.

1. **`kubectl logs` is NOT a log store.** It is pod-scoped and dies with the
   pod. **Go to Loki first.** (The 2026-08-15 investigation concluded "there
   is no logging" twice when Loki had the data both times.) Query by
   `run_id` and `namespace` — both survive `capture_and_strip`.

2. **Application logging cannot catch a foreign writer.** If a resource
   changed and your code did not do it, no amount of your own instrumentation
   will show it. Don't grep your own logs for the write — grep for the shape
   that would have skipped it.

3. **`managedFields` on the object is STEP ONE for "who wrote this".** Free,
   always present, names the manager that owns each field:

   ```sh
   kubectl get <kind> <name> -n <ns> -o jsonpath='{.metadata.managedFields}' | jq
   ```

   A manager that is not your controller is the answer. Different
   `manager` values on different fields tell you exactly which write set
   which fields.

4. **GKE audit log** converts that manager / user-agent into a ServiceAccount
   and a timestamp. Note GKE's default policy is **Metadata-level** — request
   BODIES are not captured; you get who and when, not what.

5. **`status.decisions` on a Plan** tells you which controller branch fired.
   Read it before assuming a bug in the controller logic.

## Related lessons

- `pre-push-validation` — the lesson that instructs you to run the gates
  (including pytest) before pushing. This lesson is what you check BEFORE
  that one fires the suite.
- `sqlite-tests-miss-shared-postgres-races` — a different class of
  "tests-green, production-broken" gap; same principle of interrogating what
  the test environment does and does not model.
