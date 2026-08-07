# crucible

A reusable agent-runtime library over the [`pi`](https://github.com/earendil-works/pi)
coding agent: platform gateways, a subprocess runtime driver, typed tools, and
interactivity (widgets/forms) — all built against neutral ports so an application
can swap any concrete without touching the rest.

crucible knows nothing about any specific application. The multi-agent app
[`impi`](../impi) is the reference consumer: it composes crucible's pieces from
its own settings into a running engine.

## What's inside

| Area | Package | Responsibility |
|---|---|---|
| Ports | `crucible.ports.agent`, `crucible.ports.chat` | The Protocol contracts everything depends on (no implementations). |
| Runtime | `crucible.runtimes.pi` | Drives `pi --mode rpc` over line-delimited JSON, one subprocess per conversation. |
| Gateways | `crucible.gateways` | Chat-platform adapters (`mattermost`, `slack`) — the only code that imports a platform SDK. |
| Flows | `crucible.flows` | Conversation orchestration: `AgentFlow` (a batch → one reply), `MessageCoalescer`. |
| Tools | `crucible.tools` | The typed-tool framework: `@tool` registry, capability gating, an HTTP tool-server. |
| Interactions | `crucible.interactions` | The widget/form callback machinery (dispatcher, receiver, UI bridge). Stateless: reads an `AgentPresence` the app owns. |
| Store | `crucible.store` | SQLite persistence for sessions, interactions, and forms. |
| Profiles | `crucible.profiles` | Load agent profiles from a directory into neutral `AgentSpec`s. |
| Config | `crucible.config` | `Settings` (pydantic-settings); no module-level singleton. |

## The ports (the abstraction surface)

An application depends only on these; concretes are swappable behind them.

**Agent ports** (`crucible.ports.agent`):
- `AgentRuntime` — drives an agent over a conversation (`run_stateful` /
  `run_stateless`, session lifecycle, `drop_agent_sessions` for hot-reload).
- `AgentSpec` — one agent's neutral, runtime-agnostic config (the machine half of
  its profile). Names no backend.
- `AgentProfile` — an opaque per-agent runtime config a flow holds and passes to
  the runtime untouched.
- `UiBridge` — surface a runtime's mid-turn confirm/select/input to a human.

**Chat ports** (`crucible.ports.chat`):
- `ChatClient` — the agent's outbound platform surface: replies, reactions,
  backfill, files (`post_files`), and the interactive-widget verbs (post
  buttons, open a modal).
- `Gateway` — one agent's connection to one platform (`login`/`run`/`stop`).
- `ChatAdmin` — platform-neutral channel administration used by tools.
- `InteractionService` — what a tool calls to run a widget/form round-trip
  (`ask`, `open_form`): resolve the conversation, register the pending
  interaction, post, and match the callback later.
- `FileService` — what a tool calls to send a file into the conversation the
  turn runs in; it also owns which directories an agent may read from.
- `MessageSink` / `Flow` — where a gateway hands an incoming message.
- `AgentDirectory` — who our agents are (for dispatch decisions).
- `types` — the neutral vocabulary (`ConversationRef`, `IncomingMessage`,
  `Attachment`, `OutgoingFile`, `Action`, `Form`, …),
  including the control types a form may use (`FIELD_TYPES`) and the widget kinds
  (`ACTION_KINDS`); each adapter maps them onto its own platform's elements.

## Layer boundaries

The dependency direction is enforced mechanically by import-linter (`make lint`).
The rules, in plain terms:

- The library never imports the application.
- The **core is platform-blind** — only the gateway adapters (and the app's
  composition root) may import a chat-platform SDK.
- A **gateway** depends on chat ports only — not the runtime, flows, store, or profiles.
- The **`pi` driver** depends only on the agent ports.
- **chat ports are a pure vocabulary** — a leaf that depends on no other layer.
- **flows** work only through ports, never concretes.
- The **tool layer** and the **interactions layer** depend on ports + store, never
  on a gateway, the runtime, or a platform SDK.
- **profile loading is runtime-agnostic** — it produces a neutral `AgentSpec`; the
  runtime maps that onto its own profile.

## Composing it (how `impi` does it)

An application wires the concretes together in one place (its composition root).
Sketch:

```python
sessions = SqliteSessionStore(db_path)
runtime  = PiRuntime(pi_bin=..., extra_extensions=[EXTENSION_PATH, ...], ui_bridge=...)
store    = CompositeProfileStore([FsProfileStore(agents_dir), FsProfileStore(builtin_dir)])
for spec in store.list():
    profile   = build_pi_profile(spec)                 # AgentSpec -> the pi driver's profile
    flow      = AgentFlow(runtime, profile, sessions, agent_name=spec.name)
    coalescer = MessageCoalescer(flow)
    gateway   = MattermostGateway(driver, coalescer, chat, ...)  # or SlackGateway
```

crucible also ships cohesive **wiring helpers** — `GatewayFactory`,
`InteractionWiring`, `ToolWiring` — that assemble the common combinations from
neutral config, so the app's composition root stays short. The app decides which
concretes to build and how to gate each agent's tools. See
[docs/building-an-app.md](../../docs/building-an-app.md) for the pattern and a
skeleton, and `impi/app.py` (`build_app`) for the full reference.

## Runtime-backend seam

Only one runtime backend exists today (`pi`), and `impi`'s composition root
constructs it directly (there is a `TODO(runtime-backend)` marking the spot). When
a second `AgentRuntime` appears, the intended change is a small runtime-builder
selected by a settings key, so an app depends only on the `AgentRuntime` port.

## Status

crucible was extracted from `impi` and lives in this workspace for now; it is
structured to become its own package/repository. It has no dependency on `impi`
(enforced), so it can be lifted out cleanly.
