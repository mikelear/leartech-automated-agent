# Canonical Python service Dockerfile — SIBLING of Dockerfile.runtime.
# Both images FROM leartech-agent-base directly (same pin as Dockerfile.runtime),
# rather than the service sitting three hops downstream of an image this same
# repo publishes (previously: leartech-agent-runtime → leartech-agent-go → this
# service, a producer/consumer loop on our own container build cycle).
#
# leartech-agent-base carries: python:3.14-slim, ca-certificates, curl, git,
# gnupg, build-essential, jq, make, gh CLI, uv (also re-copied below for
# parity with Dockerfile.runtime), helm, yq, kubectl, hadolint. That covers
# every runtime dependency of this service:
#   * uvicorn / FastAPI          → python 3.14 + `uv sync` installs deps
#   * app/routers/initiatives.py → shells out to `gh api` (present)
#   * gate/tools/pr_back.py      → shells out to `gh` (present)
#   * ten `gate.*` imports from  → all pure Python, satisfied by `uv sync`
#     `app/`                       into /app/.venv
# ffmpeg is the ONLY thing that must be added on top; it is not in
# leartech-agent-base and this service uses it for video-review work.
#
# Notably dropped by this rebase (present in leartech-agent-go but not
# needed at runtime for a uvicorn service):
#   * Go toolchain (go 1.26)
#   * golangci-lint
#   * govulncheck
#   * baked leartech-go.mk under /usr/local/share/
# The gate.tools.parsers.govulncheck_json module PARSES JSON produced by
# govulncheck (from files handed to it), it does not shell out to it — so
# dropping the Go binary is safe.
#
# /workspace is NOT created here. The Helm chart mounts it as an emptyDir
# volume — standard K8s pattern for ephemeral writable scratch space, and
# avoids the kaniko --snapshotMode=redo bug where empty dirs are lost
# between snapshots.

# Pinned to the SAME tag Dockerfile.runtime's BASE_IMAGE ARG defaults to so
# the two sibling images always resolve to identical substrates. No digest
# — matches Dockerfile.runtime's convention (tag-only pin).
FROM ghcr.io/mikelear/leartech-agent-base:0.30.1

# ffmpeg is specific to this service (agent uses it via Bash for video-review
# work). It is NOT in leartech-agent-base, so we install it here.
# ca-certificates, curl, git, and gh are already in the base image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN groupadd -r agent && useradd -r -g agent -u 1000 agent \
    && mkdir -p /home/agent \
    && chown agent:agent /home/agent

WORKDIR /app

# COPY --chown bakes ownership at copy time. The agent user can then write to
# /app/gate/agent/lessons/catalog at runtime without a separate chown step.
# This is the modern Docker pattern; cleanly handled by kaniko.
COPY --chown=agent:agent pyproject.toml uv.lock* ./
COPY --chown=agent:agent app/ app/
COPY --chown=agent:agent gate/ gate/
COPY --chown=agent:agent initiatives/ initiatives/
COPY --chown=agent:agent README.md ./

ENV UV_FROZEN=true
RUN uv sync --frozen --no-cache --no-dev 2>/dev/null || uv sync --no-cache --no-dev

# `uv sync` installs the project itself into /app/.venv, which materialises
# the `[project.scripts]` entries (notably `leartech-agent`) under
# /app/.venv/bin/. The ENTRYPOINT below uses an absolute path so uvicorn
# works regardless of PATH — but operators running `kubectl exec <pod> --
# leartech-agent ...` get a default login PATH of /usr/local/sbin:...:/bin,
# which does NOT include the venv. Without this PATH extension,
# `which leartech-agent` returns 1 inside the pod and the operator CLI
# (introduced in PR #95) is effectively unreachable. Putting the venv on
# PATH makes every project-scripts console entry callable as a bare command.
ENV PATH="/app/.venv/bin:${PATH}"

ENV PORT=8080
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV LEARTECH_REPO_ROOT=/workspace

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
