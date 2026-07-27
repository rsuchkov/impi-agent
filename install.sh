#!/usr/bin/env bash
# impi bootstrap installer — the curl target:
#
#   curl -fsSL https://raw.githubusercontent.com/rsuchkov/impi-agent/main/install.sh | bash
#
# Deliberately tiny and self-contained: checks the bare prerequisites, clones
# the repo at the latest release tag into $IMPI_HOME/repo, and execs the
# VERSIONED installer from the clone (installer/main.sh) — so the questionnaire
# always matches the release being installed. bash 3.2 compatible (macOS).
#
# Options (also work via env):
#   --home DIR      install root (default ~/.impi;    env IMPI_HOME)
#   --answers FILE  non-interactive answers file      (env: per-key IMPI_*)
#   --ref REF       branch/tag to install (default: newest v* tag, else main)
#   --repo URL      source repository                 (env IMPI_REPO_URL)

set -euo pipefail

REPO_URL="${IMPI_REPO_URL:-https://github.com/rsuchkov/impi-agent}"
IMPI_HOME="${IMPI_HOME:-}"
ANSWERS=""
REF=""

say() { printf '%s\n' "$*"; }
die() { printf '✘ %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --home) IMPI_HOME=$2; shift 2 ;;
        --answers) ANSWERS=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --repo) REPO_URL=$2; shift 2 ;;
        -h|--help)
            sed -n '2,16p' "$0" 2>/dev/null || true
            exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
done

case "$(uname -s)" in
    Linux|Darwin) : ;;
    *) die "only Linux and macOS are supported (got: $(uname -s))" ;;
esac
command -v git >/dev/null 2>&1 || die "git is required — install it and re-run"
command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1 \
    || die "a container runtime is required — install Docker (with compose v2) or podman"

# stdin is the pipe under `curl | bash`; talk to the human via /dev/tty.
if [ -z "$IMPI_HOME" ]; then
    if [ -e /dev/tty ] && [ -z "${IMPI_ASSUME_YES:-}" ] && [ -z "$ANSWERS" ]; then
        printf 'Install directory [%s]: ' "$HOME/.impi" >/dev/tty
        IFS= read -r IMPI_HOME </dev/tty || IMPI_HOME=""
    fi
    IMPI_HOME="${IMPI_HOME:-$HOME/.impi}"
fi
case "$IMPI_HOME" in
    /*) : ;;
    *) IMPI_HOME="$PWD/$IMPI_HOME" ;;
esac

[ -d "$IMPI_HOME/repo/.git" ] && die "already installed at $IMPI_HOME — use \`impi update\`, or remove $IMPI_HOME/repo to reinstall"

if [ -z "$REF" ]; then
    REF=$(git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null \
        | sed 's|.*refs/tags/||' | sort -V | tail -n 1) || REF=""
    if [ -z "$REF" ]; then
        say "no release tags found — installing from main (pre-release)"
        REF=main
    fi
fi

say "→ impi $REF -> $IMPI_HOME"
mkdir -p "$IMPI_HOME"
git clone --quiet "$REPO_URL" "$IMPI_HOME/repo"
git -C "$IMPI_HOME/repo" checkout --quiet "$REF"

exec bash "$IMPI_HOME/repo/installer/main.sh" --home "$IMPI_HOME" ${ANSWERS:+--answers "$ANSWERS"}
