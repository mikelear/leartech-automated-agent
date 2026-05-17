# Canonical Python service Dockerfile, modelled on leartech-ai-classifier
# (the org's Python gold-standard) with two extras specific to this service:
# (a) gh CLI + ffmpeg in system deps (agent uses these via Bash MCP)
# (b) /workspace pre-created + owned so run_initiative can clone consumer repos
#
# Key kaniko-friendly choices:
# - Single FROM (no AS aliases that don't get used)
# - User creation, home dir, and /workspace all in ONE atomic RUN (consolidating
#   multiple chown-on-empty-dir RUNs that historically tripped kaniko's snapshot
#   detection and produced empty layers)
# - COPY --chown bakes file ownership at copy time, so no separate chown RUN
#   is needed for /app/gate/agent/lessons/catalog or other source-derived paths

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        ffmpeg \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Non-root user + home dir + /workspace (for consumer-repo cloning at runtime)
# all in one atomic RUN. Single layer with explicit chmod 0775 ownership
# avoids the kaniko empty-snapshot edge case that affected the previous
# multi-RUN pattern.
RUN groupadd -r agent && useradd -r -g agent -u 1000 agent \
    && mkdir -p /home/agent /workspace \
    && chown agent:agent /home/agent /workspace

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
