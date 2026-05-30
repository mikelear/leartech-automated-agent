---
id: cross-agent-retrospect-routing-with-deferred-category
title: Retrospect findings should carry category (preventable vs deferred_followup) + target_agent + suggested_form so they fan out to the right downstream agent
applies_to:
  - initiative_agent
  - review_agent
  - orchestrator_agent
status: open
captured_at: 2026-05-30T17:00:00Z
source:
  type: agent_run
  reference: orchestrator init #5 PR #7 retrospect + fix-import-and-add-e2e PR #10 retrospect + init #6 PR #11 retrospect
  observer: mike.lear@leartech
  latency_to_capture: hours
category: architecture
slipped_past_criteria: []
proposed_criterion: |
  Every retrospect finding must be tagged with three fields:

    category: preventable | deferred_followup
    target_agent: initiative_agent | review_agent | orchestrator_agent |
                  infrastructure_agent | shared
    suggested_form: lesson | initiative | criterion | pre-push-check

  V1: findings are filed as text on the retrospect Issue with these
  three fields rendered as a header per finding. V2 (when the
  Orchestrator's retrospect fan-out is built): findings are
  programmatically routed:

    suggested_form=lesson    → new markdown in gate/agent/lessons/catalog/
                               with applies_to=<target_agent>
    suggested_form=initiative→ POST /initiatives with name "<title>"
    suggested_form=criterion → new entry in gate/criteria/
    suggested_form=pre-push-check → new entry in the language's pre-push
                                    catalog hook

  Filter: file Issue if findings_count > 0 (preventable OR
  deferred_followup). The deferred_followup category is critical: today
  agents bury scope decisions in PR description prose; the new field
  makes them queryable.
---

## The principle

Today's retrospect filter only captures **preventable** findings —
things the agent could have done differently given the final gate
state. That misses an important class:

| Category | Definition |
|---|---|
| **Preventable** | "I made a wrong call that gates didn't catch; future-me should know" |
| **Deferred follow-up** | "I deliberately did not do X in this PR for reason Y; X needs future work" |

The PR description is human-readable but not agent-queryable. Without
the second category, deferred work accumulates as unsearched comments
instead of actionable tickets the next agent (Orchestrator,
Infrastructure Agent, future-self) can find.

## The full finding schema

```yaml
findings:
  - category: preventable
    target_agent: initiative_agent
    suggested_form: lesson
    title: "ExternalSecret duplicate-name risk when both backends enabled"
    rationale: "Code rendered two ExternalSecrets with same metadata.name; helm doesn't catch it. Add fail-fast guard."

  - category: deferred_followup
    target_agent: initiative_agent
    suggested_form: initiative
    title: "Add Tekton PR-time e2e presubmit using kaniko-tar-load + crane"
    rationale: "scripts/e2e.sh works locally; the always_run: true presubmit needs dind/kaniko-tar-load + crane work"
    reason_deferred: "no docker in sandbox to validate the Tekton step config; would block every future PR if I shipped a broken always_run: true presubmit"
```

## The bugs that surfaced this lesson

- **2026-05-30 PR #10** (fix-import-and-add-e2e): agent said "follow-up
  will be raised in retrospect after this PR merges" in PR description.
  Retrospect ran with **0 findings** because deferred-scope isn't in the
  current filter. The TODO sits in prose, not in a queryable Issue.

- **2026-05-30 PR #11** (orchestrator-agent-loop): agent filed
  retrospect Issue #12 with 4 preventable findings — each had `Priority`
  + `Suggested form` (lesson / pre-push-check / etc.) but NOT `category`
  or `target_agent`. Partial adoption of the routing pattern.

## What this enables (V2 fan-out)

Today the retrospect Issue is human-read by the operator. The operator
decides whether to act. With this lesson encoded, V2 can auto-dispatch:

```
finding.suggested_form == 'lesson':
  → PR new markdown in gate/agent/lessons/catalog/ with applies_to=target_agent

finding.suggested_form == 'initiative':
  → POST /initiatives with name=finding.title

finding.suggested_form == 'criterion':
  → PR new entry in gate/criteria/

finding.suggested_form == 'pre-push-check':
  → PR new entry in the relevant language's catalog hook
```

The retrospect becomes a tickets-generator instead of a tickets-author
job for humans. Agents teach agents.

## Cross-agent application

- `initiative_agent` retrospect → findings about CODE/PR quality
- `review_agent` retrospect → findings about review process gaps
- `orchestrator_agent` retrospect (V2) → findings about plan-level
  decisions, multi-PR patterns, cluster asymmetry, budget management

All three use the same schema. The `target_agent` field decides who
actually receives each finding.

## Related lessons

- `every-initiative-extends-the-e2e-script` — the deferred case in
  practice (agent CAN'T ship the full thing; defers Tekton step)
- `consult-reference-cluster-before-iterating` — applies preventable
  findings to a specific decision pattern
- existing lesson `agent-posts-iteration-summary-comment` — operator
  visibility into agent decisions, complementary surface
