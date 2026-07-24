# Building an app on crucible

`crucible` is a kit, not a framework: it gives you the pieces (a runtime driver,
gateways, tools, interactivity, storage) plus a few **wiring helpers** that
assemble the common combinations from neutral config. Your application writes a
**composition root** — a `build_app` that constructs the concretes and a `run`
that starts them. `impi` (`packages/impi/src/impi/app.py`) is the full reference;
this is the shape.

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
| `InteractionWiring(IntegrationsSettings, sessions, codec=…)` | the interactivity config + a `CallbackCodec` | shared `posters`/`sinks`, the UI bridge, the dispatcher, and (via `finalize`) the interaction service + HTTP receiver |
| `ToolWiring(ToolSettings, data_dir=…, interactivity_on=…)` | the tool-server config | per-agent `enroll`/`profile_env` and `build_server` |

`InteractionWiring` needs the codec **injected** (the interactions layer must not
import a gateway); pass your HTTP-callback gateway's codec (e.g.
`MattermostCallbackCodec()`).

## The composition pattern

1. Load profiles (`FsProfileStore`, merged with `CompositeProfileStore` if you
   bundle engine-owned agents).
2. Build the session store and the interaction wiring (its `ui_bridge` feeds the
   runtime; its `dispatcher` feeds the gateway factory).
3. Build the runtime (`PiRuntime`, or another `AgentRuntime`), the registry
   (`AgentDirectory`), the tool wiring, and the gateway factory.
4. Loop the agents: resolve each to a `GatewayConfig`, build its `GatewayHandle`,
   `enroll` its tools, build its `AgentFlow` + `MessageCoalescer`, and `register`
   it with the interaction wiring.
5. `finalize` the interaction wiring, build the tool server, (optionally) a
   `ProfileReloader`, and assemble your `App`.

## A minimal skeleton

Illustrative — trimmed of engine-owned agents and hot-reload; see `impi/app.py`
for the complete version.

```python
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways import GatewayConfig, GatewayFactory
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import AgentSink, InteractionWiring
from crucible.profiles import FsProfileStore
from crucible.runtimes.pi import build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.store.sessions import SqliteSessionStore
from crucible.tools import ToolWiring


def build_app(settings, registry, loop_guard):  # registry implements AgentDirectory
    profiles = FsProfileStore(settings.agents_path)
    sessions = SqliteSessionStore(settings.db_path)

    interactions = InteractionWiring(
        settings.integrations, sessions, codec=MattermostCallbackCodec()
    )
    runtime = PiRuntime(pi_bin="pi", session_dir=settings.session_dir,
                        ui_bridge=interactions.ui_bridge)
    tools = ToolWiring(settings.tools, data_dir=settings.data_dir,
                       interactivity_on=settings.integrations.enabled)
    factory = GatewayFactory(directory=registry, loop_guard=loop_guard,
                             dispatcher=interactions.dispatcher)

    units = []
    for spec in profiles.list():
        config = GatewayConfig(
            kind="mattermost", mattermost_url=settings.mattermost_url,
            mm_token=settings.token_for(spec.name),
        )
        handle = factory.create(spec.name, config)
        if handle is None:
            continue
        tools.enroll(spec, handle.admin)  # before building the profile
        env = tools.profile_env(spec) or {}
        profile = build_pi_profile(spec)  # + apply env / the gateway prompt hint
        flow = AgentFlow(runtime, profile, sessions, agent_name=spec.name)
        coalescer = MessageCoalescer(flow, on_arrival=interactions.on_arrival_for(spec.name))
        interactions.register(
            spec.name, chat=handle.chat, sink=AgentSink(sink=coalescer, chat=handle.chat)
        )
        units.append((spec, handle.create_gateway(coalescer)))

    interactions.finalize(needs_receiver=True)
    tool_server = tools.build_server(
        directory=registry, interaction_svc=interactions.interaction_svc,
        dotenv_path=settings.dotenv_path,
    )
    return units, tool_server, interactions.receiver, runtime, sessions
```

`run` then starts the tool server, the interaction receiver, and each gateway's
WebSocket/socket loop, and tears them down on exit — see `impi/app.py`'s `run`.

## Why not a `build_engine(config)`?

Because apps diverge: a different app may want different interactivity, different
tool gating, a different runtime backend, or a different agent-selection policy.
The wiring helpers keep the common path short while leaving you free to bypass any
of them and wire the pieces by hand. That composability is the point of the kit.
