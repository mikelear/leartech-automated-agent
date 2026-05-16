FROM python:3.13-slim AS base

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

RUN groupadd -r agent && useradd -r -g agent -u 1000 agent

WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY app/ app/
COPY gate/ gate/
COPY initiatives/ initiatives/
COPY README.md ./

ENV UV_FROZEN=true
RUN uv sync --frozen --no-cache --no-dev 2>/dev/null || uv sync --no-cache --no-dev

ENV PORT=8080
ENV UV_CACHE_DIR=/tmp/uv-cache
ENV LEARTECH_REPO_ROOT=/workspace

RUN mkdir -p /home/agent && chown agent:agent /home/agent

# Lessons catalog must be writable so POST /lessons (qa-arch ring 2 + 3
# integration) can append new lesson files at runtime. The dir is baked
# at build time (24+ files); we chown it so the non-root agent user can
# add to it. Note: writes are still pod-local and lost on restart —
# qa-arch posts should eventually go via a PR-based path for persistence.
RUN chown -R agent:agent /app/gate/agent/lessons/catalog

# /workspace must exist + be writable so run_initiative can clone consumer
# repos on demand (cluster mode — no pre-mounted repos). Without this the
# agent dies at the first `gh repo clone` with PermissionError on the
# parent dir.
RUN mkdir -p /workspace && chown -R agent:agent /workspace

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
