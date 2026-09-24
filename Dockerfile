# syntax=docker/dockerfile:1
# =====================================================================
# WorkForge image (FastMCP stdio MCP server + worker system)
#
# Build (from repo root):
#   docker build -t workforge:0.1.0 .
#
# Run (stdio MCP server — the MCP client attaches via stdin/stdout):
#   docker run --rm -i \
#     -v workforge-data:/data/workforge \
#     workforge:0.1.0
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
# * No EXPOSE / HEALTHCHECK: the server speaks MCP over stdio, there is
#   no network listener to expose or probe.
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

# Non-root runtime user (least privilege). Home dir = the data volume
# mount point; pre-created and chowned so named volumes inherit the
# right ownership on first mount.
RUN groupadd -r workforge \
    && useradd -r -g workforge -d /data/workforge -s /usr/sbin/nologin workforge \
    && mkdir -p /data/workforge \
    && chown -R workforge:workforge /data/workforge

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    WORKFORGE_HOME=/data/workforge

# Virtualenv (pinned deps + editable workforge install) plus the source
# tree that install points at. tests/, .git, .agents, .venv never enter
# the build context (see .dockerignore).
COPY --from=builder --chown=workforge:workforge /app/.venv /app/.venv
COPY --chown=workforge:workforge src/ /app/src/

# Durable state (scripts/, jobs/): mount a named volume here or job
# records vanish with the container.
VOLUME /data/workforge

USER workforge

# Version label is overridable at build time (CI passes the tag/SHA).
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="WorkForge" \
      org.opencontainers.image.description="MCP server + worker system for AI agents: save Python scripts, run sync/async jobs, stream logs, fetch outputs" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="disillusioners / WorkForge contributors" \
      org.opencontainers.image.source="https://github.com/disillusioners/workforge" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

# No args = stdio MCP server. Subcommands pass through:
#   docker run --rm ... workforge:TAG db-init
ENTRYPOINT ["workforge"]
