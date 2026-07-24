# Architecture

## The shape of it

impi hosts several **agents** in one process. Each agent is a bot account on a
chat platform (Mattermost or Slack). When a message needs an answer, the engine
spawns the [`pi`](https://github.com/earendil-works/pi) coding agent as a
subprocess (`pi --mode rpc`) and drives it over line-delimited JSON. `pi` holds
the model connection and the agent's on-disk memory; the engine holds identity,
routing, persistence, interactivity, and the domain tools.

Two packages, in a uv workspace:

- **`crucible`** — the reusable runtime library (gateways, the `pi` driver, tools,
  interactivity, storage, and the neutral ports). Application-agnostic.
- **`impi`** — the application. It composes crucible into a multi-agent engine and
  adds multi-agent wiring, the gateway factory, inter-agent tools, and the bundled
  `support` agent.

## Layers and boundaries

The dependency direction is enforced mechanically by import-linter (`make lint`);
a breach fails the lint instead of surfacing at review. The rules:

1. **The library never imports the application** — `crucible` never imports `impi`.
2. **The core is platform-blind** — only the gateway adapters (and the app's
   composition root) may import a chat-platform SDK (`mattermostautodriver`,
   `slack_bolt`).
3. **A gateway** depends on chat ports only — not the runtime, flows, store, or profiles.
4. **The `pi` driver** depends only on the agent ports.
5. **chat ports are a pure vocabulary** — a leaf layer depending on nothing else.
6. **flows** work only through ports, never concretes.
7. The **tool layer** depends only on ports — not a gateway, the runtime, or a platform SDK.
8. The **interactions layer** may use chat ports + the store, but not the runtime, flows, tools, gateways, or a platform SDK.
9. **profile loading is runtime-agnostic** — it produces a neutral `AgentSpec`, not a `pi` profile.

The principle: one change = one module. Concrete details of a backend live in one
adapter; everything else depends inward on ports.

## Ports

Ports are Protocol contracts under `crucible.ports`. The important ones:

- **Agent** (`ports/agent`): `AgentRuntime` (drives a conversation), `AgentSpec`
  (an agent's neutral config), `AgentProfile` (opaque per-agent runtime config),
  `UiBridge` (surface a mid-turn confirm/select to a human).
- **Chat** (`ports/chat`): `ChatClient` (reply/react/backfill + the widget verbs),
  `Gateway` (a platform connection), `ChatAdmin` (channel administration),
  `InteractionService` (the tool-facing widget/form round-trip), `MessageSink` /
  `Flow` (inbound entry point), `AgentDirectory` (who our agents are), and `types`
  (the neutral vocabulary: `ConversationRef`, `IncomingMessage`, `Action`, `Form`, …).

## The `pi` runtime driver

`crucible.runtimes.pi` is the concrete `AgentRuntime`. It is layered:
`protocol` (pure JSON encode/decode) → `transport` (subprocess bytes) →
`session` (one turn at a time) → `runtime` (a pool of sessions).

- A conversation maps to a **session id** `"<agent>--<conversation_id>"` (chosen
  by the session store). `PiRuntime.run_stateful` takes or creates the
  per-conversation `PiRpcSession` under a per-session lock; concurrency is bounded
  by a semaphore, and idle sessions are reaped (the on-disk `pi` memory survives,
  so the next message resumes the same session).
- `build_pi_profile(spec)` maps a neutral `AgentSpec` onto `pi`'s own profile
  (config dir, tools, skills, provider/model). The argv the driver assembles
  (highlights):

  ```
  pi --mode rpc --approve
     --session-id <agent>--<conversation>      # or --no-session for a stateless run
     --session-dir <data>/pi-sessions/<agent>
     -e <engine tool-bridge> [-e <extension> ...]
     --tools read,bash,...                      # the ONE allowlist (may be empty)
     --no-skills [--skill <path> ...]           # only the agent's declared skills
     [--provider <p>] [--model <m>]
     [--append-system-prompt <gateway formatting rules>]
  ```

  `pi`'s working directory is the agent's profile dir, so it natively loads that
  agent's `.pi/*` (system prompt, settings). See
  [runtime-notes.md](runtime-notes.md) for what each flag means.

**Hot-reload:** on `SIGHUP` the `ProfileReloader` re-reads every profile, swaps
each flow's profile in place, drops idle sessions, and re-syncs the registry —
conversation memory is preserved. A *new* agent needs a restart (agents are
enumerated at startup); an *edited* agent needs a reload.

## Gateways

`crucible.gateways` holds the platform adapters — the only code that imports a
platform SDK. Each gateway is one bot account: it owns the WebSocket/socket loop,
normalizes platform events into the neutral `IncomingMessage`/`Action` types,
applies the respond decision (a lone resident agent answers everything in a
channel; with several agents present, only an explicit mention) and the
`LoopGuard`, and hands the message to a `MessageSink`.

- **Mattermost** delivers interactive callbacks over **HTTP** (it needs the
  interactions receiver).
- **Slack** (Socket Mode) drives the same interaction dispatcher over its
  **socket** — no HTTP receiver needed.

The `GatewayFactory` that builds these lives in `crucible.gateways`; it takes a
neutral `GatewayConfig` (which transport, which tokens). `impi` only resolves that
config from its own settings (`impi/gateways.py` — `resolve_gateway`), so the
composition root never branches on transport inline.

## Tools

`crucible.tools` is a framework, not a set of tools. A tool is a Python class
registered with `@tool` (name, JSON-Schema parameters, `execute`, and the
capabilities it `requires`). At runtime:

- Each agent gets a per-agent **manifest** (the tools it may use) and a secret
  **token**, injected into its `pi` subprocess env.
- The engine's TypeScript **tool-bridge extension** (shipped with the driver,
  loaded via `-e`) turns a `pi` tool call into `POST /tool/<name>` against a
  localhost **tool-server**, with the token in a header.
- `ToolServer` maps token → agent, checks that agent's allowlist, builds a
  `ToolContext` scoped to that agent (its own `ChatAdmin`, the directory, the
  widget/form services), and runs the tool.

**Capability gating:** a tool declares `requires` (e.g. `CAP_CHAT_ADMIN`,
`CAP_WIDGETS`, `CAP_FORMS`). At composition, each agent's capability set is
assembled from its environment (which gateway it runs on, whether interactivity is
enabled). A tool the agent can't back is dropped from its manifest and allowlist
(and logged) — so a tool never runs without the dependency it declares.

## Interactions (widgets and forms)

`crucible.interactions` is the callback machinery. `InteractionDispatcher` is the
transport-neutral brain: a click either **resolves a blocking mid-turn request**
(a paused turn waiting on a confirm/select via the `UiBridge`) or is consumed as a
**one-shot** widget that feeds a synthetic message back into the agent's turn. The
`InteractionsServer` is the HTTP receiver for HTTP-callback gateways; Slack drives
the same dispatcher over its socket. The `InteractionService` is the outbound half
— it posts widgets and opens modal forms on behalf of a tool (`ask`, `open_form`).

None of these collaborators own per-agent state: they look up an agent's outbound
client (`poster`) and inbound sink (`AgentSink`) through an **`AgentPresence`** at
request time. The application owns that registry (a `{agent: AgentSink}` map wrapped
in `MappingPresence`) and fills it as it builds each agent. So `InteractionWiring`
builds everything up front from the presence — no per-agent `register`, no
post-loop `finalize`.

## The composition root

`impi/app.py` is the one place concrete adapters meet. `build_app(settings)`
assembles everything from an `ImpiSettings`: it loads agent profiles (user agents +
the bundled engine agents, merged), builds the session store, the interaction
plumbing, the `PiRuntime`, the registry, the tool wiring, and the gateway factory;
then it builds one `AgentUnit` per agent (`GatewayHandle` → `AgentFlow` →
`MessageCoalescer`), the tool-server, and the reloader. `run(settings)` starts the
tool-server, then the interactions receiver, then N supervised gateway loops
(each isolated so one flaky agent can't take the engine down), and installs the
`SIGHUP` reload handler.

## Request flows

**Inbound message.** platform event → the gateway normalizes it to an
`IncomingMessage` (applying the respond decision + `LoopGuard`) → `MessageCoalescer`
(one worker per conversation; messages that arrive mid-turn are batched into the
next turn) → `AgentFlow.handle_batch` dedups, gets/creates the session, renders a
prompt (with a one-shot history backfill on the first turn), and calls
`AgentRuntime.run_stateful` → the `pi` subprocess runs the turn → `AgentFlow` posts
the reply.

**Tool call.** mid-turn, `pi` invokes a tool → the tool-bridge extension
`POST`s to the tool-server with the agent's token → `ToolServer` authenticates,
checks the allowlist, builds a scoped `ToolContext`, runs `Tool.execute` against
live engine state (e.g. `ctx.require_chat_admin().create_channel(...)`) → the JSON
result returns into `pi`'s turn.

**Widget click.** a user clicks a button/select → Mattermost `POST`s to the
receiver (`/interact`) or Slack delivers it over the socket → the platform's
`CallbackCodec` normalizes it → `InteractionDispatcher` resolves the blocking
Future if a turn is paused on it, otherwise submits a synthetic message that
starts a new turn. A form's modal submit hits `/dialog` and feeds the field values
back the same way.

## Conventions

- **Modularity first.** Dependencies point inward through ports; the boundaries
  above are enforced by import-linter. One feature = one module.
- **Runtime-neutral core.** The neutral layers (`agent`/`chat` ports, `flows`,
  `tools`, `interactions`, `store`, `profiles`) name no concrete runtime — not in
  imports and not in prose. `pi` specifics live only in `runtimes/pi/` and the
  composition root.
- **English only** in code — strings, logs, comments, docstrings. (An agent's own
  personality in `.pi/SYSTEM.md` may be any language.)
- **State is inventory, not truth.** The SQLite store maps conversations to
  deterministic session ids; the source of truth is `pi`'s on-disk memory.
