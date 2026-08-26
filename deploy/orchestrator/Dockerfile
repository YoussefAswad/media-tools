# Orchestrator image: FastAPI webhook service + worker, with mkvtoolnix + ffmpeg on PATH so
# the pruning step (mkvprune) can run. Built from the workspace source (mkvprune is not on PyPI).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv for a reproducible, frozen install of the whole workspace.
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

WORKDIR /app
# Build context is the monorepo root (see .dockerignore).
COPY . /app
RUN uv sync --frozen --no-dev

ENV ORCHESTRATOR_CONFIG=/config/config.yaml \
    PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /config /state /data
VOLUME ["/config", "/state", "/data"]
EXPOSE 8000

# Run the venv console script directly (no uv at runtime) so the container can run as an
# unprivileged UID without needing a writable uv cache. Host/port come from config.server.
ENTRYPOINT ["orchestrator"]
