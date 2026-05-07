---
id: defer-preview-shift-left-until-cluster-deploy
title: Defer preview-shift-left integration until cluster-pod deployment is concrete
captured_at: 2026-05-04T12:30:00Z
source:
  type: agent_run
  reference: design_v1_slice4_decision
  observer: mike@leartech
  latency_to_capture: hours
category: architecture
applies_to: []
status: encoded
encoded_in: []
---

**Decision**: slice #4 (preview-shift-left MCP wrapper) is deferred until the
cluster-as-service deployment shape is known.

**Reasoning**: porting it to a service now is wasted effort because the
cluster-as-service deployment will reshape what "fast feedback" means anyway. Today's
local pattern (`make render` / `make test` / `make preview` against a kind cluster
on the laptop) doesn't translate cleanly to a Pod context — kind-in-pod is recursive
and ugly, and only `make render` is genuinely cluster-portable.

**Local pattern stays**: clone `preview-shift-left` separately and point at it manually
when needed. Not formalised into the gate yet.

**When to revisit**: after we have at least one cluster-deployed agent run (Phase 2
of the agent runner) — at that point we'll know whether the missing fast-feedback
loop is actually slowing us down, and what shape it should take.

**Generic lesson**: deferring an integration when the next architectural shift
would invalidate it is the right call — even if it means temporarily missing a
fast-feedback loop. Don't ship infrastructure for hypothetical needs; let real runs
surface the actual gaps.
