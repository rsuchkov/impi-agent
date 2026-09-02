# impi docs

Documentation for the impi engine — and, deliberately, a knowledge base the
engine's own **support** agent reads at runtime (its environment carries
`IMPI_ROOT`, so it looks here under `$IMPI_ROOT/docs`).

Entries are concise and task-oriented — they are read by an agent, not only by
people. Keep them accurate to the code.

- **[architecture.md](architecture.md)** — how the engine works: the two
  packages, the layers and their boundaries, the `pi` runtime, gateways, tools,
  interactivity, and the request flows end to end.
- **[creating-agents.md](creating-agents.md)** — write and register an agent:
  the `agent.yaml` schema, `.pi/SYSTEM.md`, tool gating, skills, the widget/form
  control types per platform, and how to apply changes (restart vs reload).
- **[skills.md](skills.md)** — the shared skill library: installing from a
  directory or a repository, giving skills to agents, `/skills`, and what
  carries over from other tools' skill formats.
- **[tasks.md](tasks.md)** — scheduled and recurring work: writing a schedule,
  the two run modes, what happens to a missed or failed run, and how to tell
  whether the scheduler is alive.
- **[secrets.md](secrets.md)** — credentials an agent can use but never read:
  the approval card, policies and time-boxed windows, the ledger, and an honest
  account of what the broker protects against and what it doesn't.
- **[browsing.md](browsing.md)** — a real browser the agents drive: what the
  empty profile and the separate network buy, what the tool's own checks do not,
  the limits (no downloads, one browser shared by every agent), and how to turn
  it on in a deployment that already runs.
- **[agent-containers.md](agent-containers.md)** — a container per agent: what
  the isolation buys, how to give one agent a JDK, what it costs (a container
  each, and creating an agent from chat no longer finishing in chat), and the
  migration that is not optional.
- **[storage.md](storage.md)** — where the engine's state lives: the inventory
  (SQLite by default, MongoDB optionally) versus conversation memory, which is
  the runtime's own files and does not move; how to switch, and what a switch
  does not carry across.
- **[files.md](files.md)** — files and photos: what happens to an attachment on
  its way to the agent, where files are kept, the per-platform requirements, and
  the size/retention limits.
- **[commands.md](commands.md)** — slash commands and message shortcuts: how a
  command becomes an ordinary turn, per-platform setup (Mattermost / Slack), and
  the pattern for private, deterministic results (a handler beside the agent).
- **[ws-gateway.md](ws-gateway.md)** — the WebSocket gateway for your own client
  services: the frame protocol, conversation isolation, a minimal client.
- **[configuration.md](configuration.md)** — every configuration knob: the `.env`
  variables, their defaults, and what reads them.
- **[installation.md](installation.md)** — deploying the engine in containers:
  the one-line installer, what it puts where, and the `impi` wrapper.
- **[runtime-notes.md](runtime-notes.md)** — the `pi` flags the engine passes and
  the `pi` behaviors it relies on (provider/model resolution, tools, skills).
- **[troubleshooting.md](troubleshooting.md)** — common issues and how to diagnose
  them (logs, reload, session cleanup, callback networking).
- **[building-an-app.md](building-an-app.md)** — for developers: the composition
  pattern for building your own app on the `crucible` library (wiring helpers, a
  minimal skeleton).

For the project overview and quickstart, see the repository
[`README.md`](../README.md). For the reusable library, see
[`packages/crucible/README.md`](../packages/crucible/README.md).
