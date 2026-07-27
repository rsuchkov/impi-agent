.PHONY: install test run run-bg stop reload lint installer-lint installer-test e2e-install

install:
	uv sync

test:
	uv run pytest -v

run:
	uv run python -m impi.main

# Background run with a persistent log (survives reboots, unlike /tmp).
run-bg:
	@mkdir -p data/logs
	@nohup uv run python -m impi.main > data/logs/engine.log 2>&1 & \
		echo "engine started in background; logs: data/logs/engine.log"

# Graceful stop + stray cleanup. SIGTERM lets the engine close its pi children;
# the pi sweep is only a backstop for processes orphaned by a prior hard kill.
# The [i] bracket keeps the pattern from matching this recipe's own shell.
# pi masks its argv to "pi" (process.title), so match it by comm + node exe.
stop:
	@pkill -TERM -f '[i]mpi\.main' && echo "engine signalled to stop" || echo "no engine process found"
	@for p in $$(pgrep -x pi 2>/dev/null); do \
		case "$$(readlink /proc/$$p/exe 2>/dev/null)" in \
			*pi-node*) kill "$$p" 2>/dev/null && echo "killed orphan pi process $$p" ;; \
		esac ; \
	done ; true

# Hot-reload agent profiles: re-read every agent.yaml + .pi/* so each agent's
# next turn spawns pi with the new config (conversation memory survives). Sends
# SIGHUP; same [i] bracket trick as `stop` so the pattern skips this recipe.
# -n (newest match only) targets the python engine, not the `uv run` wrapper
# that also matches — signalling both makes uv forward and the engine reload twice.
reload:
	@pkill -HUP -n -f '[i]mpi\.main' && echo "reload signalled" || echo "no engine process found"

lint:
	uv run ruff check packages tests
	uv run lint-imports
	uv run pyright

# Installer shell sources: shellcheck locally if present, else via a container.
INSTALLER_SH = install.sh installer/main.sh installer/bin/impi installer/lib/*.sh
installer-lint:
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck -x -s bash -e SC1091 $(INSTALLER_SH); \
	else \
		podman run --rm -v "$$PWD:/mnt:ro,z" -w /mnt docker.io/koalaman/shellcheck:stable \
			-x -s bash -e SC1091 $(INSTALLER_SH); \
	fi
	@for f in $(INSTALLER_SH); do bash -n $$f || exit 1; done
	@echo "installer-lint OK"

# bats unit tests for the installer libraries.
installer-test:
	@if command -v bats >/dev/null 2>&1; then \
		bats installer/tests; \
	else \
		podman run --rm -v "$$PWD:/code:ro,z" -w /code docker.io/bats/bats:latest installer/tests; \
	fi

# Full local install into a throwaway IMPI_HOME (Linux + podman/docker; slow).
# KEEP=1 leaves the stack running for inspection; E2E_LLM=1 adds a live DM check.
e2e-install:
	bash installer/tests/e2e.sh
