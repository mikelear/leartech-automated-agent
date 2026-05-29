"""Admin / operator surfaces for the deployed agent service.

Phase B (B.3 + B.4) introduces a cleanup endpoint that an operator (or a
CronJob) can invoke to reconcile two classes of accumulated state:

  - Stuck DB rows: in-flight ``initiative_runs`` whose backing Job is
    gone but the row remains in ``queued`` / ``running``.
  - Superseded PipelineRuns: Lighthouse-spawned Tekton PipelineRuns
    against earlier SHAs of an agent-authored PR; once the PR's tip moves
    forward these are wasting cluster etcd + queue slots.

This module hosts the imperative cleanup primitives; the HTTP surface
lives in ``app/routers/admin.py``.
"""
