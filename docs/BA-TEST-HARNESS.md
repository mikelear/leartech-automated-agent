# BA test harness — how to prove BA WITHOUT firing repo-factory-scale work

The BA agent (`gate/agent/ba_agent.py`) authors DRAFT Plan CRDs from a
brief. Its output can be very high-leverage — one BA plan can trigger
repo creation, cluster wiring, PR gates, and a full jx-promote cycle
across two clusters. That's fine when the BA is producing the RIGHT
plan, but during development we want a fast, LLM-free way to prove the
BA plumbing without spending tokens or making cluster-scale changes we
have to unwind.

This doc describes the two proving paths — the offline (unit-test) path
and the fast on-cluster path — plus what the harness DOES and does NOT
prove.

## What the harness proves

The harness validates that a set of PLANS (as authored by any source —
LLM, hand-crafted, or fixture) satisfies the BA contract:

1. **Draft-by-default.** `spec.hold: true` AND
   `metadata.annotations["leartech.io/draft"] == "true"`. This is the
   whole reason the BA can operate autonomously — its output never runs
   without human review.
2. **Final step verifies successCriteria.** The last step in
   `spec.steps` is verification-shaped (canonically
   `inputs.action: release-health-check`; any action containing
   `health-check`, `verify`, or `health_check` is accepted).
3. **`remediates` matches `resolves`.** If the brief has
   `resolves: [PlanRef, …]`, the plan's `spec.remediates` must include
   every PlanRef by `{name, namespace}` — and if `resolves: []`, no
   `remediates` may be smuggled in.
4. **`spec.steps` is non-empty.** A plan with no steps trivially
   "succeeds" without doing work; treated as a scribe bug.

The harness does **not** prove that:

- The intermediate steps actually solve the problem (only that the
  final step verifies the criteria).
- The remediation is minimal, safe, or the "right" fix (that's the
  human review the draft-by-default hold exists for).
- The BA's live-state correlation was sound (that's a separate
  concern — the harness operates on the AUTHORED output only).

## Path 1 — the offline (unit-test) path

Runs entirely on your laptop. No cluster. No LLM. Zero cost.

```bash
uv run pytest -v tests/test_ba_test_harness.py
```

Golden fixtures live under `tests/testdata/ba_briefs/<scenario>/`:

- `infra-remediation` — one PlanRef in `resolves`, one authored plan.
- `new-website` — greenfield, empty `resolves`, target plan.
- `cluster-wide-multi-resolve` — two PlanRefs (one per cluster), two
  authored plans.

Each fixture ships:

- `brief.yaml` — the input.
- `expected-plans.yaml` — the shape a well-behaved BA is expected to
  author.
- `platform-state-snapshot.yaml` — documentation of what the BA would
  have observed via `platform_state` MCP calls (not consumed by the
  harness; makes the golden reproducible).

### Adding a new scenario

1. Draft a brief with a distinct shape (e.g. amend-plan-heavy,
   multi-service remediation).
2. Hand-craft the expected plans that would satisfy your brief +
   pass the harness.
3. Optionally snapshot what platform_state would have shown.
4. Add the scenario name to `GOLDEN_FIXTURES` in
   `tests/test_ba_test_harness.py`.

If the harness rejects your hand-crafted plans, one of the invariants
is wrong — fix the plan or (rarely) extend the harness. If the harness
accepts them but the goal doesn't make sense, that's a brief-authoring
problem, not a harness problem.

### Sanity-checking a brief before spending LLM tokens

The `ba` entrypoint supports a `--dry-run` flag that validates the
brief and prints a summary without calling the LLM or the gateway:

```bash
uv run python -m gate.agent.ba_agent \
  --brief @path/to/my-brief.yaml \
  --dry-run
```

Output includes:

- The brief `name` and one-line `goal`.
- Each `successCriteria` entry (typos here are the #1 cause of BA
  authoring the wrong final step).
- Number of PlanRef(s) in `resolves`, or an explicit callout when
  greenfield.

Exits 0 on validation success, exits 2 with a clear error message on
schema failure (unknown fields, missing `successCriteria`, empty
`goal`, …).

## Path 2 — the fast on-cluster path

Use this when you want to see WHAT a real BA authors for a real brief
without triggering downstream execution.

**Prerequisites.** The `leartech-agent-ba` AgentType must already
exist on-cluster (that's the `ba-agenttype-wiring` initiative's
deliverable, on which this initiative depends).

### Step 1 — write a standalone AgentRun manifest

Wrap the brief in an `AgentRun`, NOT a `Plan` step. A standalone
AgentRun is a one-shot invocation — no `Plan` context means no
downstream chain reaction.

```yaml
apiVersion: agent.leartech.io/v1alpha1
kind: AgentRun
metadata:
  name: ba-scratch-fix-gcp-foo
  namespace: jx-staging
spec:
  agentType: leartech-agent-ba
  inputs:
    # `inputs` is the brief body verbatim. The controller inlines it
    # into $LEARTECH_INITIATIVE_YAML on the agent Job.
    name: fix-gcp-foo-service-release
    goal: |
      Remediate the gcp release-health-check failures for foo-service.
    successCriteria:
      - foo-service Deployment on gcp reports >=1 available replica
      - HTTP GET /health/live returns 200 for 3 consecutive polls
    resolves:
      - name: foo-service-release
        namespace: jx-staging
        cluster: gcp
```

### Step 2 — apply

```bash
kubectl apply -f agentrun.yaml
```

### Step 3 — inspect the authored plan(s)

The BA runs, correlates live state, authors one or more DRAFT plans,
and exits. Because every authored plan has `hold: true` +
`leartech.io/draft: true`, NOTHING downstream fires. The controller
does not spawn Jobs, the infra_agent does not open PRs, jx-promote is
untouched.

```bash
# What did the BA author?
kubectl get plans -n jx-staging -l leartech.io/draft=true

# Full spec of one plan
kubectl get plan -n jx-staging <draft-name> -o yaml
```

### Step 4 — decide

- **Happy with the shape?** Clear the hold + strip the draft
  annotation, and the plan begins executing:
  ```bash
  kubectl annotate plan -n jx-staging <name> leartech.io/draft-
  kubectl patch plan -n jx-staging <name> --type=merge \
    -p '{"spec":{"hold":false}}'
  ```

- **Unhappy? Delete + iterate.** No downstream work has fired, so
  deleting the draft is safe:
  ```bash
  kubectl delete plan -n jx-staging <name>
  # tweak brief.yaml and re-apply the AgentRun
  ```

- **Want to see what the BA saw?** Look at the AgentRun's logs:
  ```bash
  kubectl logs -n jx-staging job/<agentrun-name>-<sha>
  ```

## What NOT to do

- **Don't apply a raw Plan CRD to test the BA.** That skips the BA
  entirely — you're just testing the controller's Plan reconciler.
- **Don't call the BA via a Plan step during scratch work.** Plan
  steps run in the context of a plan, which may have downstream steps
  that fire on `Succeeded`. Standalone AgentRun is the safer shape.
- **Don't `/hold cancel` the draft plan yourself with an unclear
  intention.** The hold is the safety valve — clear it only when
  you've eyeballed the plan and are confident it's correct.
- **Don't skip the offline path.** The unit-test harness catches shape
  mistakes in seconds; the on-cluster path spends 30+ seconds even in
  the happy case and burns real LLM tokens.

## Related files

- `gate/agent/ba_agent.py` — the entrypoint.
- `gate/agent/ba_test_harness.py` — the pure-function validator.
- `tests/test_ba_test_harness.py` — the offline test suite.
- `tests/testdata/ba_briefs/` — the golden fixtures.
- `examples/plans/` — real Plan examples for reference (not BA
  drafts).
