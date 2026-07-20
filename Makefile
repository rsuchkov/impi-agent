.PHONY: install test run run-bg stop reload lint

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
