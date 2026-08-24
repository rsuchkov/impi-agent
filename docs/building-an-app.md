# Building an app on crucible

`crucible` is a kit, not a framework: it gives you the pieces (a runtime driver,
gateways, tools, interactivity, storage) plus a few **wiring helpers** that
assemble the common combinations from neutral config. Your application writes a
**composition root** — a `build_app` that constructs the concretes and a `run`
that starts them. `impi` (`packages/impi/src/impi/app.py`) is the full reference;
this is the shape. For a much smaller one, read `ward`
(`packages/ward/src/ward/app.py`): under two hundred lines that take a chat
client, the approval primitive, the interaction receiver and the store, and no
runtime at all — an app on this library does not have to run agents.

## The division of labour

- **crucible provides**: the building blocks and cohesive wiring helpers —
  `GatewayFactory` (+ `GatewayConfig`), `InteractionWiring`, `ToolWiring`,
  `PiRuntime`, `FsProfileStore` / `CompositeProfileStore`, `SqliteSessionStore`,
  `ProfileReloader`. Each helper takes a **neutral config** and knows nothing about
  your settings.
- **your app provides**: the composition root (`build_app`/`run`), an
  `AgentDirectory` implementation (who your agents are), and thin **resolvers** that
  map your settings onto crucible's configs (e.g. which agent runs on which
  gateway, with which token). You keep the orchestration; crucible keeps the
  mechanics.

The line to hold: don't ask crucible to own `build_app`. The moment the loop and
the ordering move into the library, you have a framework and lose the freedom to
compose differently.

## The wiring helpers

| Helper | Takes (neutral config) | Gives you |
|---|---|---|
| `GatewayFactory.create(agent, GatewayConfig)` | which transport + tokens | a `GatewayHandle` (chat client, admin, a gateway builder, prompt hint, needs-receiver) |
| `InteractionWiring(IntegrationsSettings, sessions, presence, codec=…, needs_receiver=…)` | the interactivity config, an `AgentPresence`, a `CallbackCodec` | the UI bridge, the dispatcher, the interaction service, and (when `needs_receiver`) the HTTP receiver — all built up front, all reading the presence lazily |
| `ToolWiring(ToolSettings, data_dir=…, interactivity_on=…)` | the tool-server config | per-agent `enroll`/`profile_env` and `build_server` |

`InteractionWiring` holds no per-agent state: it reads an **`AgentPresence`** (a
lookup of `agent -> ChatClient` / `AgentSink`) that the app owns and fills as it
builds agents. Wrap your `{agent: AgentSink}` map in `MappingPresence`. The codec is
**injected** (the interactions layer must not import a gateway) — pass your
HTTP-callback gateway's codec (e.g. `MattermostCallbackCodec()`). Compute
`needs_receiver` up front from the gateway kinds (`needs_http_receiver(kind)`), so
everything can be built in the constructor — there is no `finalize`.

## The composition pattern

1. Load profiles (`FsProfileStore`, merged with `CompositeProfileStore` if you
   bundle engine-owned agents).
2. Resolve each agent's `GatewayConfig` up front; from the kinds, compute
   `needs_receiver`. Create the presence registry (`{agent: AgentSink}` +
   `MappingPresence`) and the interaction wiring (its `ui_bridge` feeds the runtime;
   its `dispatcher` feeds the gateway factory).
3. Build the runtime (`PiRuntime`, or another `AgentRuntime`), the registry
   (`AgentDirectory`), the tool wiring, and the gateway factory.
4. Loop the agents: build each `GatewayHandle` from its config, `enroll` its tools,
   build its `AgentFlow` + `MessageCoalescer`, and record its presence
   (`AgentSink`) in the map.
5. Build the tool server, (optionally) a `ProfileReloader`, and assemble your `App`.
   (No `finalize` — the interaction wiring was built complete in step 2.)

## A minimal skeleton

Illustrative — trimmed of engine-owned agents and hot-reload; see `impi/app.py`
for the complete version.

```python
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways import GatewayConfig, GatewayFactory, needs_http_receiver
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import AgentSink, InteractionWiring, MappingPresence
from crucible.profiles import FsProfileStore
from crucible.runtimes.pi import build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.store.sessions import SqliteSessionStore
from crucible.tools import ToolWiring


def build_app(settings, registry, loop_guard):  # registry implements AgentDirectory
    profiles = FsProfileStore(settings.agents_path)
    sessions = SqliteSessionStore(settings.db_path)

    specs = profiles.list()
    # Resolve gateway configs up front → know which agents run and whether the HTTP
    # receiver is needed, so the interaction plumbing can be built complete.
    configs = {
        s.name: GatewayConfig(kind="mattermost", mattermost_url=settings.mattermost_url,
                              mm_token=settings.token_for(s.name))
        for s in specs
    }
    needs_receiver = any(needs_http_receiver(c.kind) for c in configs.values())

    # The app owns the presence registry (agent -> AgentSink); the loop fills it,
    # interactions read it lazily via MappingPresence.
    sinks_by_agent: dict[str, AgentSink] = {}
    presence = MappingPresence(sinks_by_agent)
    interactions = InteractionWiring(
        settings.integrations, sessions, presence,
        codec=MattermostCallbackCodec(), needs_receiver=needs_receiver,
    )
    runtime = PiRuntime(pi_bin="pi", session_dir=settings.session_dir,
                        ui_bridge=interactions.ui_bridge)
    tools = ToolWiring(settings.tools, data_dir=settings.data_dir,
                       interactivity_on=settings.integrations.enabled)
    factory = GatewayFactory(directory=registry, loop_guard=loop_guard,
                             dispatcher=interactions.dispatcher)

    units = []
    for spec in specs:
        handle = factory.create(spec.name, configs[spec.name])
        if handle is None:
            continue
        tools.enroll(spec, handle.admin)  # before building the profile
        profile = build_pi_profile(spec)  # + apply tools.profile_env(spec) / the prompt hint
        flow = AgentFlow(runtime, profile, sessions, agent_name=spec.name)
        coalescer = MessageCoalescer(flow, on_arrival=interactions.on_arrival_for(spec.name))
        sinks_by_agent[spec.name] = AgentSink(sink=coalescer, chat=handle.chat)  # record presence
        units.append((spec, handle.create_gateway(coalescer)))

    tool_server = tools.build_server(
        directory=registry, interaction_svc=interactions.interaction_svc,
        dotenv_path=settings.dotenv_path,
    )
    return units, tool_server, interactions.receiver, runtime, sessions
```

`run` then starts the tool server, the interaction receiver, and each gateway's
WebSocket/socket loop, and tears them down on exit — see `impi/app.py`'s `run`.

## Composition notes from the field

- **Widget-free bots: turn interactivity off.** If your agents declare no
  `ask_user_*`/form tools and nothing `requires_confirmation`, don't wire
  `InteractionWiring` (or set `INTEGRATIONS_ENABLED=false`). With interactivity
  on, any interactive request the runtime emits makes the UI bridge try to post
  a real widget — on Slack that additionally requires the app's Interactivity
  setup, and a failure is logged and declined. Off = declined locally, quietly.
- **A confirmed tool needs a gate to confirm it.** If any of your tools declares
  `requires_confirmation`, pass `tool_gate=` to `build_server`. Without one the
  server refuses those calls rather than running them: a confirmation nobody can
  answer is not a confirmation, and failing open would make the flag decorative.
- **Sharing tool settings.** `settings_cls` is declared per tool, but several
  tools may declare the **same class**: each gets its own instance, loaded from
  the same env keys — so a tool group with one config (one repo root, one base
  URL) just reuses one `BaseSettings` class. No per-tool duplication needed.
- **Not everything has to be a turn.** `AgentRuntime.run_stateless(profile,
  prompt)` is a one-shot, memoryless run — the runtime used as a plain function.
  It's the right tool when your app must control the prompt and the delivery
  itself: a `/summarize` handler that reads the thread through `ChatClient`,
  runs one stateless call, and posts the result with `ChatAdmin.post_ephemeral`.
  Deterministic where a turn depends on the model choosing a tool, and it leaves
  the agent's session untouched. Worked example: [commands.md](commands.md).
- **Built-ins see the profile dir.** The runtime process's cwd is the agent's
  profile directory; data outside it is reachable by absolute paths or your own
  typed tools — see [runtime-notes.md](runtime-notes.md) "Built-in tools & the
  working directory". A standalone app can also pass `cwd=` per turn on
  `run_stateful` for checkout-scoped runs.

## Why not a `build_engine(config)`?

Because apps diverge: a different app may want different interactivity, different
tool gating, a different runtime backend, or a different agent-selection policy.
The wiring helpers keep the common path short while leaving you free to bypass any
of them and wire the pieces by hand. That composability is the point of the kit.
