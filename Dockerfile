# Canonical Python service Dockerfile, based on leartech-agent-go which itself
# inherits from leartech-agent-base. The base images carry: ca-certificates,
# curl, git, gh CLI, Go toolchain, and uv. We only install what's specific
# to this service on top.
#
# /workspace is NOT created here. The Helm chart mounts it as an emptyDir
# volume — standard K8s pattern for ephemeral writable scratch space, and
# avoids the kaniko --snapshotMode=redo bug where empty dirs are lost
# between snapshots.

FROM ghcr.io/mikelear/leartech-agent-go:0.31.1

# ffmpeg is specific to this service (agent uses it via Bash for video-review
# work). It is NOT in leartech-agent-base or leartech-agent-go, so we install
# it here. ca-certificates, curl, git, and gh are already in the base image.
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

ENV PORT=8080
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV LEARTECH_REPO_ROOT=/workspace

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
