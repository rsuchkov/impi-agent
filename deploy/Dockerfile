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

# The browser tool an agent runs. It attaches to a Chrome that is already
# running in another container, so no browser binaries are installed here —
# `playwright install` is never called and this stays around 14 MB.
#
# Pinned hard: the package is in 0.1.x and its command surface has moved between
# releases, so a bump is a deliberate step that re-reads the web-browsing skill,
# not something a rebuild does on its own.
ARG PLAYWRIGHT_CLI_VERSION=0.1.18
RUN npm install -g @playwright/cli@${PLAYWRIGHT_CLI_VERSION} \
 && npm cache clean --force

# Match the host user so bind-mounted conf/ and agents/ writes stay owned by
# the operator (rootful docker; rootless podman adds userns keep-id on top).
# Either id may already belong to a group/user of the base image — macOS gid 20
# (staff) is Debian's `dialout` — so create only what is missing and address the
# account numerically from here on, since its NAME may not be `impi`.
ARG IMPI_UID=1000
ARG IMPI_GID=1000
RUN set -eu; \
    getent group "${IMPI_GID}" >/dev/null || groupadd -g "${IMPI_GID}" impi; \
    getent passwd "${IMPI_UID}" >/dev/null \
      || useradd -M -u "${IMPI_UID}" -g "${IMPI_GID}" -d /home/impi -s /bin/bash impi

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer first (cached until the lock changes), sources second.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/crucible/pyproject.toml packages/crucible/
COPY packages/impi/pyproject.toml packages/impi/
# The secret tool ships here because agents run it — `secret-exec` has to be on
# their PATH, and `ward-admin` is how an operator drives the broker from this
# side. The BROKER is deliberately not installed: naming the packages is what
# keeps its Vault adapter and its certificate authority out of this image.
COPY packages/wardline/pyproject.toml packages/wardline/
COPY packages/ward/pyproject.toml packages/ward/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace \
        --package impi --package wardline
COPY packages/crucible/ packages/crucible/
COPY packages/impi/ packages/impi/
COPY packages/wardline/ packages/wardline/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package impi --package wardline
# The engine's own support agent reads these at runtime ($IMPI_ROOT/docs) — they
# are part of what it knows, so they ship with it.
COPY docs/ docs/

# Container-fixed paths; the actual content arrives via volumes/bind mounts.
# HOME is pinned rather than inherited from the account: the pi auth volume is
# mounted at /home/impi/.pi, and a pre-existing uid would bring its own home.
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/home/impi \
    DOTENV_PATH=/app/conf/.env \
    AGENTS_PATH=/app/agents \
    SKILLS_PATH=/app/skills \
    DATA_DIR=/app/data \
    IMPI_ROOT=/app \
    PI_BIN=pi
# Every path a named volume lands on has to exist HERE, owned by this user. A
# volume takes its owner from the image directory it is first mounted on, and one
# mounted where the image made nothing belongs to root — which a process running
# as ${IMPI_UID} cannot write. `/app/files` is the per-agent file root and
# `/app/migrate` is where `impi agent migrate` mounts the volume it is filling;
# both were missing, and rootless podman hid it by mapping the owner anyway.
RUN mkdir -p /app/data /app/conf /app/agents /app/skills /app/files /app/migrate \
      /home/impi/.pi \
 && chown -R "${IMPI_UID}:${IMPI_GID}" /app /home/impi
USER ${IMPI_UID}:${IMPI_GID}

CMD ["python", "-m", "impi.main"]
