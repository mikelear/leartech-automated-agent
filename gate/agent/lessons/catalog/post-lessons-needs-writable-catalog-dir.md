---
id: post-lessons-needs-writable-catalog-dir
title: POST /lessons in cluster fails with PermissionError unless the catalog dir is chown'd to the non-root agent user
captured_at: 2026-05-11T15:30:00Z
source:
  type: agent_run
  reference: v0.2.0_smoke_test_deployment
  observer: mike@leartech + claude-opus-4.7
  latency_to_capture: minutes
category: tool_bug
applies_to:
  - initiative_agent
  - operator_cli
status: encoded
encoded_in:
  - Dockerfile
slipped_past_criteria: []
proposed_criterion: |
  test_pod_writable_paths — at image-build time, verify every directory the
  service writes to at runtime is owned by the non-root runtime user
  (uid 1000 for our chart). Static check against the Dockerfile + the
  endpoints' write surface.
---

## What happened

v0.2.0 deploy on GCP came up healthy — `/health`, `/lessons` list, `/mcps`,
`/roles`, `/topology`, `/health/detail` all returned 200. **POST /lessons
returned 500.** Pod log:

    PermissionError: [Errno 13] Permission denied:
    '/app/gate/agent/lessons/catalog/<id>.md.tmp'

Container was built with `gate/` copied as root, then `USER agent` (uid 1000)
applied for runtime — but the catalog dir was never chown'd. The route's
atomic-write code (write to `.tmp`, rename) hit the permission wall on the
first write.

## Why it didn't surface in tests

Tests use `fastapi.testclient.TestClient` against the dev's filesystem,
where the user already has write access to the source tree. The
container-specific permission setup isn't exercised. Coverage was 100% on
the route handler; the failure is one layer down.

## The fix (in v0.2.1+)

Dockerfile: explicit `chown -R agent:agent /app/gate/agent/lessons/catalog`
after the COPY + before `USER agent`. Same pattern as the existing
`mkdir /home/agent && chown` line.

## The deeper issue (not fixed yet)

Pod-local writes are **lost on restart**. The chown unblocks qa-arch
integration testing, but for durable lesson persistence, the right
architecture is:

- qa-arch posts to `POST /lessons`
- Service opens a PR on the automated-agent repo with the lesson YAML
- Human approves (or auto-approves if signed)
- Merge triggers next deploy → catalog rebuilt with the new lesson

This is GitOps-aligned (every change traces to a commit) and survives pod
restarts. Captured as a follow-up; the chown fix is the v0.2.1 unblock.

## Generic principle

When a service runs as non-root in a cluster (Kyverno requires it), every
path it writes to needs explicit chown in the Dockerfile. Tests on the
dev's filesystem won't catch this — it requires either:
- A docker-build + docker-run integration test that exercises every write path
- A static analysis of the route handlers' write paths vs the Dockerfile's chown lines

The proposed criterion (`test_pod_writable_paths`) is the static analysis
version — cheaper, catches this class of bug at PR time.
