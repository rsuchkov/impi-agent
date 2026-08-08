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
- **Chat** (`ports/chat`): `ChatClient` (reply/react/backfill + files + the widget
  verbs), `Gateway` (a platform connection), `ChatAdmin` (channel administration),
  `InteractionService` (the tool-facing widget/form round-trip), `FileService`
  (the tool-facing "send this file"), `MessageSink` / `Flow` (inbound entry
  point, returning a `TurnOutcome`), `AgentDirectory` (who our agents are), and
  `types` (the neutral vocabulary: `ConversationRef`, `IncomingMessage`,
  `Attachment`, `Action`, `Form`, …).
- **Tasks** (`ports/tasks`): `TaskService` — the tool-facing "schedule this for
  later", kept apart from the chat ports because scheduling is not a chat idea.

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

Controls are described in a **neutral vocabulary** (`FIELD_TYPES` /
`ACTION_KINDS` in the chat ports): each adapter translates a field type into its
own element — Mattermost dialog elements in `gateways/mattermost/dialogs.py`,
Block Kit in `gateways/slack/rendering.py` — and normalizes the answer back to
one string per field. A picker returns a platform id, which
`interactions/labels.py` resolves to `@name (id)` before the agent sees it. The
per-platform table is in [creating-agents.md](creating-agents.md).

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
prompt (with replayed history, below), and calls
`AgentRuntime.run_stateful` → the `pi` subprocess runs the turn → `AgentFlow` posts
the reply.

**Attachments.** Files travel with the message: the gateway downloads whatever
was attached (only for messages it decided to answer), saves it under
`DATA_DIR/attachments/<agent>/<conversation>/` through the `AttachmentStore` —
a plain file store, like the skill library — and hands the flow an
`IncomingMessage` carrying local paths. `AgentFlow` names every file in the
prompt and passes pictures to `run_stateful(..., images=…)`, the runtime port's
one non-text input. Everything else is a path the agent reads with its own
tools. Outbound, the `send_file` tool goes through a `FileService` that resolves
the turn's conversation exactly like the widget service does, polices which
directories the agent may read from, and posts via `ChatClient.post_files`. See
[files.md](files.md).

**Replayed history.** The conversation itself lives in the runtime session, so a
prompt normally carries only the new messages plus sender identity. Two cases add
context: on the **first turn** of a session with prior history (a pre-existing
thread, or a channel session) the whole transcript is replayed; on **later turns**
only what was posted since the agent's last reply (the session's `last_active` is
the cursor). The second case matters because in a channel the agent runs only when
mentioned — anything said in between never reached it as a turn. The agent's own
posts are left out of that catch-up (they are already in its runtime session),
which is why the gateway's login identity is pushed into the flow
(`AgentFlow.set_identity`). Both transcripts are budgeted by character count.

**Tool call.** mid-turn, `pi` invokes a tool → the tool-bridge extension
`POST`s to the tool-server with the agent's token → `ToolServer` authenticates,
checks the allowlist, builds a scoped `ToolContext`, runs `Tool.execute` against
live engine state (e.g. `ctx.require_chat_admin().create_channel(...)`) → the JSON
result returns into `pi`'s turn.

**Screen.** a few commands are answered by the ENGINE rather than an agent —
`/skills` browses the skill library and hands skills to agents. A
`ScreenRegistry` is consulted **before** the agent is chosen; the screen renders
a `View` (text + actions), and each click carries its own state back and
**rewrites the same message** (`ChatClient.update_actions`) — no turn, no second
message. Listing what exists and editing a profile are facts and edits, so a
model in the loop would only add latency and a chance to name a skill that isn't
there. See [skills.md](skills.md).

**Scheduled task.** `crucible.scheduler` is one ticker over the task tables in
the same SQLite file. Each pass reads what is due and decides one of four things
— the previous run is still going (`overlap`), the process only just started
(defer), it is later than its grace window (`missed`), or it runs — and every
decision writes a row, so "why didn't it run" always has an answer. An occurrence
is claimed with a compare-and-swap that advances the schedule in the same
transaction, which is what makes a double fire impossible; the next occurrence is
always computed from the previous SCHEDULED instant, never from the clock, which
is what keeps a restart from skipping a day. The tick runs inside `run()`'s
`gather` under the same supervision as the gateways, and writes a heartbeat at the
end of every pass so an idle scheduler can be told from a dead one. Firing goes
through the agent's own sink (`submit_tracked`), so a scheduled turn is an
ordinary turn that happens to report its outcome. See [tasks.md](tasks.md).

**Command.** a user runs a slash command (Mattermost `POST`s to
`/command/{agent}` on the receiver, verified by the command's token) or picks a
`crux_*` message shortcut (Slack, over the socket) → the transport resolves the
agent and the conversation (thread root) → `InteractionDispatcher.invoke_command`
feeds a synthetic message → an ordinary turn, whose answer is posted into that
conversation like any other reply. A command whose result must stay private (or
be produced deterministically) is better handled beside the agent, with
`run_stateless` — see [commands.md](commands.md).

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
- **Skills are a library; assignments are profiles.** `crucible.skills` is a
  plain file store over `SKILLS_PATH` — scan, install from a directory or a
  pinned git commit, remove. Which agent gets which skill lives in that agent's
  `agent.yaml` (`registry:<name>`), edited round-trip so the comments a person
  wrote survive. One source of truth, visible in a diff.
- **State is inventory, not truth.** The SQLite store maps conversations to
  deterministic session ids; the source of truth is `pi`'s on-disk memory.
- **Schema changes are additive, applied on open.** There is no migration tool:
  `SqliteSessionStore` runs its `CREATE TABLE IF NOT EXISTS` script and then
  `_migrate()`, which reads `PRAGMA table_info` and `ALTER TABLE … ADD COLUMN`s
  whatever is missing — idempotent, so it self-heals whichever version wrote the
  file. Every query names its columns explicitly, so an older engine keeps
  working on a database a newer one has migrated (which is what makes `impi
  update`'s rollback safe). Anything beyond adding a defaulted column — renames,
  backfills, dropped tables — has no mechanism yet and would need a real one.
