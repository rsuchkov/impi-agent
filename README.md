# impi

A personal multi-agent system for chat. Each agent is a bot account on a chat
platform (Mattermost or Slack) with its own personality and tools; the engine
hosts many of them in one process and routes conversations between people and
agents — and between agents.

The engine does **not** call an LLM directly. Every agent turn is delegated to
the external [`pi`](https://github.com/earendil-works/pi) coding agent, spawned
as a subprocess (`pi --mode rpc`) and driven over line-delimited JSON. `pi` owns
the model connection, the built-in tools, and the agent's on-disk memory; the
engine owns identity, routing, persistence, interactivity, and the domain tools.

```
  chat platform (Mattermost / Slack)
        │  message
        ▼
  gateway ──► coalescer ──► AgentFlow ──► pi subprocess (pi --mode rpc)
        ▲                                     │   │
        │  reply                        tool calls │ mid-turn UI
        └───────────────  tool-server ◄───────────┘ (widgets/forms)
```

## Status

Early development (version `0.1.0`; pre-1.0 under SemVer, so interfaces and layout
may still change). Usable for its purpose, but not yet production-hardened.

## Repository layout

This is a [uv](https://docs.astral.sh/uv/) workspace of two packages:

- **`packages/crucible`** — a reusable agent-runtime library: platform gateways,
  the `pi` runtime driver, typed tools, interactivity, session storage, and the
  neutral ports everything is built against. Knows nothing about any specific
  application. See [`packages/crucible/README.md`](packages/crucible/README.md).
- **`packages/impi`** — the application: multi-agent wiring, the Mattermost/Slack
  gateway factory, inter-agent choreography tools, and the bundled `support`
  agent. It composes crucible into a running engine.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** and **Python 3.13** (`.python-version`).
- **The `pi` CLI on `PATH`** — `npm i -g @earendil-works/pi-coding-agent`
  (requires Node.js). Authenticate it once (`pi` supports a subscription login or
  an OpenAI-compatible endpoint — see [docs/runtime-notes.md](docs/runtime-notes.md)).
- **podman or docker** — only for the local Mattermost stack in development.

## Quickstart

```bash
# 1. Install dependencies
make install

# 2. Start a local Mattermost (http://localhost:8065 — create the admin + a team)
podman compose up -d          # or: docker compose up -d

# 3. In Mattermost, create a bot account and copy its access token
#    (System Console → Integrations → Bot Accounts).

# 4. Configure the engine
cp .env.example .env
#    - set AGENTS_PATH to a directory that holds your agents
#    - add the bot token. The simplest first agent is the bundled `support`
#      agent: set AGENTS_MM_TOKEN__SUPPORT=<token> and it appears automatically.

# 5. Run
make run
```

To add your own agent (its profile, tools, and personality), see
[docs/creating-agents.md](docs/creating-agents.md).

## Common commands

| Command | What it does |
|---|---|
| `make install` | `uv sync` — install the workspace |
| `make run` | run the engine in the foreground |
| `make run-bg` | run in the background, logging to `data/logs/engine.log` |
| `make stop` | signal the engine to stop and sweep orphaned `pi` children |
| `make reload` | hot-reload agent profiles (re-read every `agent.yaml` + `.pi/`) |
| `make test` | run the test suite |
| `make lint` | ruff + import-linter (layer boundaries) + pyright |

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the engine works, the layers, and the request flows.
- [docs/creating-agents.md](docs/creating-agents.md) — write and register an agent.
- [docs/configuration.md](docs/configuration.md) — every `.env` / config knob.
- [docs/runtime-notes.md](docs/runtime-notes.md) — the `pi` flags and facts the engine relies on.
- [docs/troubleshooting.md](docs/troubleshooting.md) — common issues and how to diagnose them.

The `docs/` tree is deliberately also a runtime knowledge base: the bundled
`support` agent reads it to answer questions about the engine and build agents.
