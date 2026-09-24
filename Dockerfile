# syntax=docker/dockerfile:1
# =====================================================================
# WorkForge image (FastMCP MCP server + worker system)
#
# Build (from repo root):
#   docker build -t workforge:0.1.0 .
# Recommended (with full OCI provenance labels):
#   docker build \
#     --build-arg VERSION=0.1.0 \
#     --build-arg VCS_REF=$(git rev-parse --short HEAD) \
#     --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
#     -t workforge:0.1.0 .
#
# Run (default: streamable-http MCP server on 0.0.0.0:8000):
#   docker run -d --rm -p 8000:8000 \
#     -v workforge-data:/data/workforge \
#     workforge:0.1.0
#   → MCP endpoint http://localhost:8000/mcp (SSE: /sse via
#     --transport sse), health probe http://localhost:8000/health
#
# Remote serving STRONGLY wants a token (scripts run unsandboxed with
# the container user's privileges):
#   docker run -d --rm -p 8000:8000 \
#     -v workforge-data:/data/workforge \
#     -e WORKFORGE_AUTH_TOKEN=<long-random-secret> \
#     workforge:0.1.0
#
# stdio MCP server (the MCP client attaches via stdin/stdout):
#   docker run --rm -i \
#     -v workforge-data:/data/workforge \
#     workforge:0.1.0 --transport stdio
#
# Optional run history (Postgres, TCP DSN from a container):
#   -e WORKFORGE_DATABASE_URL=postgresql://user:pass@host:5432/workforge
#   one-time guarded bootstrap (only ever creates `workforge` /
#   `workforge_test`):
#   docker run --rm -v workforge-data:/data/workforge \
#     -e WORKFORGE_DATABASE_URL=... workforge:0.1.0 db-init
#
# Notes:
# * psycopg[binary] bundles libpq — NO apt postgres client libraries are
#   needed (that's why this image has no apt-get line at all; it stays
#   close to the python:3.12-slim-bookworm base).
# * Dependency source of truth is pyproject.toml + uv.lock
#   (requires-python >=3.10; runtime on python:3.12-slim-bookworm —
#   pinned by distro, no floating :latest). uv is pinned to 0.7.0, the
#   version that generated uv.lock (same pin as the yedda-agent-fleet
#   Dockerfiles whose conventions this file follows).
# * Unlike yedda-agent-fleet (run from source via uvicorn), WorkForge's
#   entry points are console scripts (`workforge`, `workforge-db-init`),
#   so `uv sync` installs the project itself (editable). The runtime
#   stage therefore copies BOTH the built .venv and src/ — at the exact
#   path /app/src the editable install points at.
# * Container default is HTTP serving (streamable-http on 0.0.0.0:8000,
#   see ENV below): EXPOSE + a curl-free HEALTHCHECK probe the
#   unauthenticated /health route. CLI flags override these ENVs, so
#   `--transport stdio` restores the phase-1 behavior unchanged.
# =====================================================================

# ---------- Stage 1: dependency builder ----------
FROM python:3.12-slim-bookworm AS builder

# uv pinned to 0.7.0 — the version that generated uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.7.0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Install only third-party deps into /app/.venv. Layer is cached on
# pyproject.toml/uv.lock changes; app-code edits do not invalidate it.
# --locked: fail the build if uv.lock is stale vs pyproject.toml.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project

# Project source + metadata (README/LICENSE feed hatchling), then
# install the project itself on top — this creates the `workforge` and
# `workforge-db-init` console scripts inside /app/.venv/bin.
COPY src/ ./src/
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim-bookworm

# Non-root runtime user (least privilege). UID is PINNED to 999:
# a future base refresh that already allocates 999 fails the build
# loudly instead of silently shifting ownership, and existing volumes
# (chowned to 999) stay valid across base-image updates.
# Home dir = the data volume mount point; pre-created and chowned so
# named volumes inherit the right ownership on first mount.
RUN groupadd -r workforge \
    && useradd -r -u 999 -g workforge -d /data/workforge -s /usr/sbin/nologin workforge \
    && mkdir -p /data/workforge \
    && chown -R workforge:workforge /data/workforge

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    WORKFORGE_HOME=/data/workforge \
    WORKFORGE_TRANSPORT=streamable-http \
    WORKFORGE_HOST=0.0.0.0 \
    WORKFORGE_PORT=8000

# Virtualenv (pinned deps + editable workforge install) plus the source
# tree that install points at. tests/, .git, .agents, .venv never enter
# the build context (see .dockerignore).
COPY --from=builder --chown=workforge:workforge /app/.venv /app/.venv
COPY --chown=workforge:workforge src/ /app/src/

# Durable state (scripts/, jobs/): mount a named volume here or job
# records vanish with the container.
VOLUME /data/workforge

# MCP endpoint served on $WORKFORGE_PORT (streamable-http default).
EXPOSE 8000

# Container healthcheck. python:3.12-slim ships no curl/wget, so probe
# the unauthenticated /health route with the stdlib. Shell form (not
# exec) so $WORKFORGE_PORT is read per-probe; 127.0.0.1 reaches the
# 0.0.0.0 bind. start-period covers uvicorn's startup.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('WORKFORGE_PORT', '8000'), timeout=4).read(1)"

USER workforge

# OCI labels. VERSION defaults to the package version (0.1.0) so the
# version label always matches the shipped workforge package; CI passes
# the release tag on vX.Y.Z pipelines. VCS_REF/BUILD_DATE identify the
# exact source and build time (revision ≠ version, by design).
ARG VERSION=0.1.0
ARG VCS_REF=""
ARG BUILD_DATE=""
LABEL org.opencontainers.image.title="WorkForge" \
      org.opencontainers.image.description="MCP server + worker system for AI agents: save Python scripts, run sync/async jobs, stream logs, fetch outputs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="disillusioners / WorkForge contributors" \
      org.opencontainers.image.url="https://github.com/disillusioners/workforge" \
      org.opencontainers.image.source="https://github.com/disillusioners/workforge" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

# No args = streamable-http MCP server on 0.0.0.0:8000 (override via ENV
# or CLI flags). stdio stays available: --transport stdio. Subcommands
# pass through:
#   docker run --rm ... workforge:TAG db-init
ENTRYPOINT ["workforge"]
