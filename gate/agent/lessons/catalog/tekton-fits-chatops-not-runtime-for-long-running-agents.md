---
id: tekton-fits-chatops-not-runtime-for-long-running-agents
title: Tekton task is right for chatops *triggering*, wrong for long-running agent *runtime* — split the concerns
captured_at: 2026-05-05T13:30:00Z
source:
  type: agent_run
  reference: slice_e_tekton_task_design
  observer: mike@leartech
  latency_to_capture: minutes
category: architecture
applies_to: []
status: encoded
encoded_in:
  - tekton/README.md
  - memory/project_form_factor_evolution.md
---

When designing how to "move the agent off Mike's laptop", two concerns get
conflated and need separating:

1. **How is the agent fired?** — chatops, webhook, dashboard, scheduled, programmatic
2. **What's the agent's runtime shape?** — ephemeral pod, long-running pod, daemon, controller

Slice E (Tekton-task chatops trigger) addresses **(1) only**. Tekton fits
chatops well — Lighthouse already does it for `ai-review`, `end2end`, etc.
But Tekton's properties make it the **wrong** shape for **(2)** when the agent
is long-running:

- Default 60-minute task ceiling; configurable up to several hours but still bounded
- Ephemeral pod per invocation; no shared state across runs
- No event subscription; Tekton tasks don't subscribe to pub/sub topics
- One-shot model: start → run → exit

For agents that need to live longer than that — **webCoder Phase 3 tenant
generation sessions, multi-day project work, agents subscribing to
`standards.updated` events** — Tekton is the wrong runtime.

## The composition that emerges

```
chatops surface (Tekton — slice E, ephemeral)
       ↓ fires
   short initiative (≤30 min, exits cleanly)         ← Tekton is fine here
       ↓ for longer-running work, the Tekton
       ↓ task SPAWNS a Job and exits early
long-running agent runtime (K8s Job, PVC if needed)  ← v2 form-factor
       ↓
       posts back via Slack / PR sticky / pub/sub event
```

Tekton triggers; the Job/CRD does the long-running work. Two stages, each in
their natural form-factor.

## For webCoder Phase 3 specifically

WebCoder's tenant agents probably **don't use Tekton at all**:

- The trigger is webcoder-service's own dashboard (user types "build me an X")
- The runtime is a tenant Pod that webcoder-service spawns programmatically
- Lifecycle is managed by webcoder-service, not Lighthouse

Slice E (this task) doesn't conflict with that — it just doesn't apply.
WebCoder converges with automated-agent at the *substrate* layer (shared MCPs,
shared lessons catalog, shared image) and at the *initiative pattern*, **not at
the trigger surface**. Different tools, different reasons, same building blocks.

## Generic principle

Two-layer rule for any "where does this run" question:

- **Trigger layer** — match the tool to the human ergonomics (chatops →
  Tekton; dashboard → service; schedule → CronJob; event → pub/sub
  subscription)
- **Runtime layer** — match the shape to the *duration* and *state* needs
  (short stateless → ephemeral pod; long stateful → Job; declarative
  K8s-managed → CRD + controller)

The same agent can be *triggered* via Tekton chatops AND *run* in a long-lived
Job. Slice E is the Tekton trigger; v2 (form-factor ladder) is the Job
runtime; both compose.

## What this means for slice ordering

`project_next_phase_alignment.md` keeps the same order:

- **E** (Tekton chatops trigger) — done as of 2026-05-05
- **F** (BA agent foundation) — when Lovable export ready
- **C** (new-repo creation primitives) — webCoder Phase 3 prep + qa-analysis input
- **v2 form-factor (K8s Job runtime)** — when first long-running agent need
  arrives. Probably webCoder Phase 3 tenant generation. Not blocked on slice E.

Slice E stays valid even after v2 lands; they cover different concerns.
