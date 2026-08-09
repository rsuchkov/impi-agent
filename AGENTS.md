# AGENTS.md

Instructions for AI coding agents (and humans) working in this repository.

**impi** is a personal multi-agent system for chat. Each agent is a bot account on
a chat platform (Mattermost or Slack); the engine hosts many in one process. The
engine never calls an LLM directly — every agent turn is delegated to the external
[`pi`](https://github.com/earendil-works/pi) coding agent, spawned as a subprocess
(`pi --mode rpc`) and driven over line-delimited JSON.

- Project overview and quickstart: [README.md](README.md).
- Architecture, configuration, and guides: [docs/](docs/).

## Commands

| Command | What it does |
|---|---|
| `make install` | `uv sync` — install the workspace |
| `make run` | run the engine (`python -m impi.main`) |
| `make run-bg` | run in the background, logging to `data/logs/engine.log` |
| `make stop` | stop the engine and sweep orphaned `pi` children |
| `make reload` | hot-reload agent profiles (re-read every `agent.yaml` + `.pi/`) |
| `make test` | `uv run pytest` |
| `make lint` | ruff + import-linter (layer boundaries) + pyright |
| `make installer-lint` | shellcheck + `bash -n` over `install.sh` / `installer/` |
| `make installer-test` | bats unit tests for the installer libraries |
| `make e2e-install` | full throwaway compose install (Linux; slow, needs podman/docker) |

Run `make lint` and `make test` before considering a change done; both must be
green. Touching `install.sh`, `installer/`, or `deploy/` additionally requires a
green `make installer-lint` + `make installer-test`.

## Project structure

A [uv](https://docs.astral.sh/uv/) workspace of two packages:

- **`packages/crucible`** — the reusable agent-runtime library (gateways, the `pi`
  driver, tools, interactivity, storage, and the neutral ports). Application-agnostic.
- **`packages/impi`** — the application: multi-agent wiring, the gateway factory,
  inter-agent tools, and the bundled `support` agent.

Alongside the packages (deliberately not intertwined with them):

- **`deploy/`** — the deployment Dockerfile + compose base/overlay files.
- **`installer/`** + **`install.sh`** — the curl|bash TUI installer, the host
  `impi` wrapper, and their bats tests. Bash 3.2 compatible (macOS).
- **`scripts/release.sh`** — cut a release (bump `VERSION`, tag `vX.Y.Z`, push).

See [docs/architecture.md](docs/architecture.md) and
[packages/crucible/README.md](packages/crucible/README.md) for detail — don't
duplicate architecture here.

## Development principles

- **Modularity first.** One change = one module. Dependencies point inward through
  ports (Protocols); a backend's specifics live in a single adapter; composition
  happens in `impi/app.py` (`main.py` is a thin entrypoint); wiring is by
  constructor injection. Layer boundaries are enforced by import-linter (`make
  lint`), so a boundary breach fails the lint rather than surfacing at review.

- **Runtime-neutral core.** The neutral layers — `ports/{agent,chat}`, `flows`,
  `tools`, `interactions`, `store`, `profiles` — name no concrete runtime, and not
  only in imports: comments, docstrings, names, and strings there use neutral terms
  ("the runtime", "the runtime's tool extension / session / UI request"), never
  "pi's …". `pi` specifics live only in `runtimes/pi/` and the composition root; the
  one exception is the backend knobs in `config.py` (`pi_*`), an explicit settings
  boundary.

- **English only in code** — strings, logs, comments, docstrings. Exceptions:
  intentional non-ASCII test data (mark it `# Non-ASCII on purpose`); an agent's own
  personality in its `.pi/SYSTEM.md` may be any language.

- **Name service instances with an `_svc` suffix** — a field, parameter, or local
  bound to a service reads as a service, not as data or a port (`widget_svc`,
  `interaction_svc`, not `widgets`). Ports and data keep plain names.

- **Sort imports.** ruff's isort (rule `I`) enforces grouped, sorted imports;
  `make lint` fails on drift. Run `uv run ruff check --fix` to sort.

- **Import at the top of the file, never inside a function** (rule `PLC0415`).
  A module's dependencies belong in one readable place: an import buried in a
  branch hides a layer crossing from review and from import-linter, which reads
  the module graph, and it turns an `ImportError` into a surprise on a path
  nobody exercised. If a top-level import would be circular, that is the design
  telling you the dependency points the wrong way — move the shared piece rather
  than hide the edge. Tests are exempt: there the import is often part of what
  the test asserts.

- **Boundaries are enforced.** `make lint` runs ruff (incl. import sorting),
  import-linter (the layer contracts), and pyright (basic). Keep it green.

## Testing

Tests are offline (no network) and live under `tests/`. Add tests with changes,
keep the suite green (`uv run pytest`), and keep `make lint` green before finishing.

## Commits and changes

- **Commit only when explicitly asked.** Otherwise leave changes in the working tree
  and offer a commit — the maintainer reviews the uncommitted diff.
- **User-visible changes get a bullet in `CHANGELOG.md`** under `Unreleased`
  (features, fixes, behavior changes — not internal refactors). The release
  script refuses to cut a release while that section is empty.
- **Before a MINOR or MAJOR release, review the bundled `support` agent**
  (`packages/impi/src/impi/builtin_agents/agents/support/`) against what is
  shipping: its `agent.yaml` allowlist, `.pi/SYSTEM.md`, and every `.pi/skills/*/SKILL.md`.
  Ask three questions and act on the answers:
  1. Does a new capability need a tool in its allowlist, or a new/updated skill?
  2. Did anything it describes change — a path, a setting, a command, a default?
  3. Does a doc it points at still say what the skill claims it says?

  This agent ships inside the package, so nobody hits its staleness the way they
  hit a bug: it went five releases describing an engine that no longer existed
  and answering 403 on tools the docs promised. A patch release does not need
  this; a minor or major one is exactly when the gap opens.
- **Write commit messages inline** with `git commit -m` (not `-F <file>`).
- **State only what the change does** — concrete facts, not plans, discussion, or
  what was deferred. No verification either: test and lint counts describe the
  moment the commit was made, not the change, and they age badly in a log.
- **No `Co-Authored-By` trailer**, and no other generated attribution.

## Local development

Bring up a local Mattermost for development with compose:

```bash
podman compose up -d        # or: docker compose up -d
```

The web UI is at http://localhost:8065 (first visit: create the admin account).
Create a bot account, copy its token, and put it in `.env`. See
[README.md](README.md) for the full quickstart.

## Requirements and configuration

- **[uv](https://docs.astral.sh/uv/)** and **Python 3.13** (`.python-version`).
- The **`pi` CLI on `PATH`**: `npm i -g @earendil-works/pi-coding-agent` (needs Node.js).
- Configuration is pydantic-settings (`packages/*/src/*/config.py`); secrets live in
  `.env` (git-ignored), templated by `.env.example`. Full reference:
  [docs/configuration.md](docs/configuration.md).
