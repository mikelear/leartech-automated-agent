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

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

ENTRYPOINT ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
