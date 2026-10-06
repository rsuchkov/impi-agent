# syntax=docker/dockerfile:1
# impi engine image: Python 3.13 (engine) + Node 22 (the pi runtime) + the pi
# CLI, installed from the workspace sources. Built locally by the installer
# (`compose build impi`); context = repo root.
#
# Node comes as a binary copy from the official image — both bases are Debian
# bookworm, so the glibc matches. If the copy trick ever breaks, fall back to
# the nodesource apt repository.

# Base images pinned by manifest-list digest: a tag moves, a digest does not.
# To bump: `skopeo inspect --raw docker://<image:tag> | sha256sum` and paste
# the digest here (the same three in Dockerfile.agent and Dockerfile.ward).
FROM docker.io/library/node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS node

FROM docker.io/library/python:3.13-slim-bookworm@sha256:5024f48ba9441d4b13a95d3945abc6365538e3a31109833367a1923523c6efed

COPY --from=ghcr.io/astral-sh/uv:0.11@sha256:77280f2f771df71f90786c314fe1bbc1e023feac652969bbf139c280babf2eb7 /uv /uvx /usr/local/bin/
COPY --from=node /usr/local/bin/node /usr/local/bin/
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates procps \
      ripgrep fd-find \
 && ln -s /usr/bin/fdfind /usr/local/bin/fd \
 && rm -rf /var/lib/apt/lists/*

# The pi coding agent — the engine spawns it per turn (PI_BIN) — and the browser
# tool an agent runs (it attaches to a Chrome in another container, so no
# browser binaries are installed here; `playwright install` is never called).
# The browser tool is in 0.1.x and its command surface has moved between
# releases, so its bump re-reads the web-browsing skill.
# The pi coding agent and the browser tool, from a lockfile: deploy/pi/package.json
# pins the versions, package-lock.json pins every dependency with its integrity
# hash, and `npm ci` refuses a mismatch between the two. Install scripts are
# not run — neither package needs them, and a package that did would be
# running code at build time that nobody reviewed. Telemetry is answered
# here, once, for the install and for every run. Bump: edit the version in
# deploy/pi/package.json, then `make pi-lock`, then rebuild.
ENV PI_TELEMETRY=0
COPY deploy/pi/package.json deploy/pi/package-lock.json /opt/pi/
RUN cd /opt/pi \
 && npm ci --ignore-scripts --omit=dev --no-audit --no-fund \
 && npm cache clean --force \
 && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi \
 && ln -s /opt/pi/node_modules/.bin/playwright-cli /usr/local/bin/playwright-cli

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
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer first (cached until the lock changes), sources second.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/crucible/pyproject.toml packages/crucible/
COPY packages/impi/pyproject.toml packages/impi/
# `--extra mongo` brings the second inventory backend. A few hundred kilobytes,
# paid once here so that choosing MongoDB is a setting an operator flips rather
# than an image they rebuild.
# The secret tool ships here because agents run it — `secret-exec` has to be on
# their PATH, and `ward-admin` is how an operator drives the broker from this
# side. The BROKER is deliberately not installed: naming the packages is what
# keeps its Vault adapter and its certificate authority out of this image.
COPY packages/wardline/pyproject.toml packages/wardline/
COPY packages/ward/pyproject.toml packages/ward/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace \
        --package impi --extra mongo --package wardline
COPY packages/crucible/ packages/crucible/
COPY packages/impi/ packages/impi/
COPY packages/wardline/ packages/wardline/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package impi --extra mongo --package wardline
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
