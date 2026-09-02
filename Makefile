.PHONY: install test test-mongo run run-bg stop reload lint relay-lint relay-test installer-lint installer-test e2e-install

install:
	uv sync

test:
	uv run pytest -v

# The store conformance suite against a real Mongo. `make test` covers SQLite
# only, on purpose: the tests here are offline, and the claim protocol rests on
# atomicity a fake would agree with for the wrong reason. Needs the `mongo`
# extra installed (`uv sync --extra mongo`).
MONGO_TEST_PORT ?= 27077
MONGO_RUNTIME ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
test-mongo:
	@$(MONGO_RUNTIME) rm -f impi-test-mongo >/dev/null 2>&1 || true
	$(MONGO_RUNTIME) run -d --rm --name impi-test-mongo \
		-p $(MONGO_TEST_PORT):27017 docker.io/library/mongo:7
	@for i in $$(seq 1 60); do \
		$(MONGO_RUNTIME) exec impi-test-mongo \
			mongosh --quiet --eval 'db.runCommand({ping:1})' >/dev/null 2>&1 && break; \
		[ $$i = 60 ] && { echo "mongo did not come up"; \
			$(MONGO_RUNTIME) rm -f impi-test-mongo; exit 1; }; \
		sleep 1; \
	done
	@MONGO_TEST_URL=mongodb://localhost:$(MONGO_TEST_PORT) \
		uv run pytest tests/test_session_store.py tests/test_scheduler_store.py \
		tests/test_approval_store.py -v; \
		status=$$?; $(MONGO_RUNTIME) rm -f impi-test-mongo >/dev/null; exit $$status

run:
	uv run python -m impi.main

# Background run with a persistent log (survives reboots, unlike /tmp). Appends:
# a restart is usually how you react to something odd, and truncating here would
# destroy the evidence for it.
run-bg:
	@mkdir -p data/logs
	@nohup uv run python -m impi.main >> data/logs/engine.log 2>&1 & \
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
	uv run ruff check packages tests scripts
	uv run lint-imports
	uv run python scripts/check_names.py
	uv run pyright

# The browser relay is Go, so it sits outside ruff/pyright/pytest. The image
# build runs `go vet` and `go test` too — that is what protects a release when
# no Go toolchain is installed here. These targets are for working on it.
RELAY_DIR = packages/browser-relay
relay-lint:
	@command -v go >/dev/null 2>&1 \
		|| { echo "no go toolchain — the image build runs these instead"; exit 0; }
	cd $(RELAY_DIR) && go vet ./... && gofmt -l . | (! grep .)
	@echo "relay-lint OK"

# -race needs cgo, which the static image build cannot use; here it can.
relay-test:
	@command -v go >/dev/null 2>&1 \
		|| { echo "no go toolchain — the image build runs these instead"; exit 0; }
	cd $(RELAY_DIR) && go test -race ./...

# Installer shell sources: shellcheck locally if present, else via a container.
INSTALLER_SH = install.sh installer/main.sh installer/bin/impi installer/lib/*.sh scripts/release.sh
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
# STORE=mongo installs the inventory on MongoDB instead of a SQLite file.
e2e-install:
	bash installer/tests/e2e.sh
