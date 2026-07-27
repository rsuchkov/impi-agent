# syntax=docker/dockerfile:1
# impi engine image: Python 3.13 (engine) + Node 22 (the pi runtime) + the pi
# CLI, installed from the workspace sources. Built locally by the installer
# (`compose build impi`); context = repo root.
#
# Node comes as a binary copy from the official image — both bases are Debian
# bookworm, so the glibc matches. If the copy trick ever breaks, fall back to
# the nodesource apt repository.

FROM docker.io/library/node:22-bookworm-slim AS node

FROM docker.io/library/python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

# The pi coding agent — the engine spawns it per turn (PI_BIN). Pinned; bump
# deliberately with a release, not implicitly on rebuild.
ARG PI_VERSION=0.80.3
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} \
 && npm cache clean --force

# Match the host user so bind-mounted conf/ and agents/ writes stay owned by
# the operator (rootful docker; rootless podman adds userns keep-id on top).
ARG IMPI_UID=1000
ARG IMPI_GID=1000
RUN groupadd -g ${IMPI_GID} impi && useradd -m -u ${IMPI_UID} -g impi impi

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer first (cached until the lock changes), sources second.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/crucible/pyproject.toml packages/crucible/
COPY packages/impi/pyproject.toml packages/impi/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Container-fixed paths; the actual content arrives via volumes/bind mounts.
ENV PATH=/app/.venv/bin:$PATH \
    DOTENV_PATH=/app/conf/.env \
    AGENTS_PATH=/app/agents \
    DATA_DIR=/app/data \
    PI_BIN=pi
RUN mkdir -p /app/data /app/conf /app/agents /home/impi/.pi \
 && chown -R impi:impi /app /home/impi
USER impi

CMD ["python", "-m", "impi.main"]
