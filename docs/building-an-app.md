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
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools import ToolWiring


def build_app(settings, registry, loop_guard):  # registry implements AgentDirectory
    profiles = FsProfileStore(settings.agents_path)
    sessions = open_store(settings.store_backend,
                          name=settings.resolved_db_name, url=settings.db_url)

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
    # Trimmed: a real root also passes confirmations_on/tool_gate, clock,
    # session_proofs and turns — see the composition notes below.
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
  `requires_confirmation`, build the wiring with `confirmations_on=True` and pass
  `tool_gate=` to `build_server` — your own `ToolApproving`, or the one
  `InteractionWiring` builds. The two go together, and `build_server` refuses one
  without the other. With neither, such a tool is not advertised to the agent at
  all: a confirmation nobody can answer is not a confirmation, and a tool that
  is offered and then refused on every call only teaches the model to retry.
  The runtime is never asked — the server's gate is the only one, so one call is
  one question. Pass `clock=runtime` too (`PiRuntime` implements `TurnClock`):
  the turn's timeout then stops while the gate waits for the person, so a slow
  answer never ends the turn — the same pause the runtime takes for the
  interactive requests it raises itself. Two things the pause does not change:
  the runtime's tool extension gives up on a call after `TOOL_CALL_DEADLINE_S`
  (300 s, `crucible.config`), so your gate's own timeout has to end before
  that; and when the caller hangs up — the turn was aborted, or the extension
  gave up — the server withdraws the card (`abandoned` in the ledger) and a
  late click runs nothing.
- **Finding the conversation from inside a tool.** A tool call carries the
  runtime session id (`ToolContext.runtime_session_id`), and it is exactly the
  `runtime_session_id` of the conversation's `SessionRecord` — the engine sets
  `RUNTIME_SESSION_ID` in the runtime's environment and the tool extension
  echoes it on every call. Two conversations never share one (the derivation
  appends a digest whenever it had to coerce a character, and the store
  enforces uniqueness), so an application may key whatever it holds per
  running turn by that id and resolve it from a tool.
- **Say what the call would do, not what the model said.** A confirmed tool
  should implement `describe(ctx, args) -> CallPreview | None` (`Describing` in
  `crucible.tools`; `CallPreview`/`PreviewRow` in `crucible.ports.chat.types`,
  the chat vocabulary, since a preview travels with the card): the
  server calls it before the gate, inside the paused time, with the same
  context `execute` gets, and the card shows its rows — `Status: In Progress →
  Done`, the issue's title beside its key — instead of the raw arguments. Read
  the current state from the system there; the arguments are the model's
  account, and the model reads text other people wrote. `danger=True` puts a
  warning on the card. A `describe` that raises is logged and the card falls
  back to the arguments, so a preview can never block the decision. The gate
  puts the request on the wire as data — `ChatClient.post_confirmation(ref,
  ApprovalCard, actions)` — and each channel draws it: Mattermost as Markdown
  (`crucible.approvals.card_markdown`, hardened by `containment`), Slack as
  native Block Kit fields, the http gateway as a `confirmation` event. Once
  answered, expired or withdrawn, the same post is rewritten with the verdict
  as a card again (`update_confirmation`, no actions left), its facts still on
  it. The preview's `title` is the card's first field (`Action`), not part of
  the headline: the headline is the engine's sentence, every value is the
  tool's text and takes the channel's hardening. A chat client of your own
  must implement both verbs; the table in
  [architecture.md](architecture.md#gateways) says which verb carries the
  model's words and which carries the engine's structure.
- **Mark what strangers wrote.** A tool whose result quotes other people —
  messages, documents, tickets, search results — declares
  `returns_untrusted = True`. The registry then appends one standard sentence to
  its description and the server wraps its result in
  `{"untrusted": true, "note": …, "data": …}`; your `execute` returns the data
  as it always did. Declare it rather than writing the warning into the
  description yourself: one wording for every such tool, and a new one cannot be
  forgotten.
- **Operating the runtime, not only running it.** `PiRuntime` also implements
  `RuntimeControl` (`crucible.ports.agent`): `has_memory(agent, session_id)`
  says whether the conversation would resume or start over — the runtime
  answers from its own files, so an application never has to know their layout;
  `reset(agent, session_id)` ends the process (its slot is free at once) and
  deletes the memory; `cancel(session_id)` interrupts the turn in flight and
  keeps the session for the next one — the turn returns with what it had, or
  the session is dropped if the runtime would not stop, with memory on disk
  untouched; `stats()` gives alive / busy / capacity / waiting for a readiness
  check. A flow needs none of this to run a turn, which is why it is a second
  Protocol — but see the next point for the one question a flow may ask it.
- **Replaying a conversation the runtime forgot.** The runtime's memory is a
  session on its disk; a pod rescheduled without that disk, an idle session
  evicted, an operator's `reset` all leave a conversation the store knows and
  the runtime starts from nothing. Hand the flow `AgentFlow(..., control=runtime)`
  and it asks `has_memory` on every turn of a known conversation; when the
  answer is no, the turn is a first turn again — the thread or the channel is
  replayed from the platform as it would be for a new session. "No" is the
  runtime's word for memory it owns and cannot find; memory it cannot see at
  all — pi's own default directory when the runtime was given no
  `session_dir`, an agent hosted in its own container — counts as remembered,
  because a wrong "no" replays the history into a session that has it, on
  every turn. A **direct
  message has no thread to read back**, and over the http gateway nothing does,
  so for those the flow asks the application: `AgentFlow(..., history=…)`, a
  `HistorySource` (`crucible.ports.chat`) whose `recent(agent, msg)` returns the
  earlier messages as `PostSnippet`s, oldest first, from the transcript you
  keep (keyed, say, by the `turn_id` the hub gave each turn). It is asked only
  when the platform had nothing, and only on a turn the runtime starts from
  nothing — the first turn of a new conversation included, since the
  conversation may be older than the engine's record of it. The runtime's own
  earlier replies belong in it too: it has none of them.
  `AgentFlow(..., context=…)`, a `ContextSource`, is the other half — who is
  asking (`preamble(agent, msg)`: the person's role, the system they are signed
  in to), put in a `[context: who is asking]` block ahead of the history and
  the message, every turn, an empty string adding nothing. The runtime cannot
  learn either from the message itself.
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

## Tools without a gateway

An application can run agents with the engine's tools and no chat platform at
all — a request/turn API of its own, a batch job, a test harness. The pieces it
needs are the ones a bot needs minus the gateway:

```python
from crucible.ports.chat.directory import AgentInfo, StaticDirectory
from crucible.tools import ToolWiring

directory = StaticDirectory([AgentInfo(spec.name, spec.role, spec.description, "", "")])
tools = ToolWiring(settings.tools, data_dir=settings.data_dir,
                   interactivity_on=False, confirmations_on=True)
tools.enroll(spec, None)                       # no admin client: no chat
profile = replace(build_pi_profile(spec), env=tools.profile_env(spec) or {})
runtime = PiRuntime(..., extra_extensions=[str(EXTENSION_PATH.resolve())])
tool_server = tools.build_server(
    directory=directory, interaction_svc=None, dotenv_path=settings.dotenv_path,
    session_resolver=my_resolver,              # runtime session id -> (channel, user)
    tool_gate=my_gate,                         # your ToolApproving: however you ask a person
    clock=runtime,                             # the turn waits while they decide
    session_proofs=proofs,                     # the same SessionProofBook the runtime got
)
```

Give the runtime and the server **one** `SessionProofBook`
(`PiRuntime(session_proofs=proofs)` and `build_server(session_proofs=proofs)`):
the runtime issues each conversation's process a secret for its session, the
server believes a call's session id only with it. Without the book the id is a
claim any process of the agent could make — which, once a turn's scope carries a
credential, is another user's credential.

- `StaticDirectory` is the `AgentDirectory` for a roster fixed at composition.
- `interaction_svc=None` turns the widget/form tools off; a tool that requires
  them is not advertised.
- `confirmations_on=True` with your own `ToolApproving` is how confirmation
  works without a chat card: the server calls your gate, with the tool's
  preview, and pauses the turn's clock through `clock=runtime` while it waits.
- `session_resolver` maps the runtime session id a tool call carries back to
  whatever you key your conversations by; it is the same id you passed to
  `run_stateful`.
- **Per-turn data for tools — a credential the person sent with the message,
  say — rides in `IncomingMessage.turn` as a `TurnScope`.** Build one in your
  gateway or API handler (`TurnSecrets({"cookie": …})` for anything that must
  never be stored or printed — it shows its names, never its values, and
  refuses pickling), hand `AgentFlow(..., turns=TurnRegistry())` and
  `build_server(..., turns=<the same registry>)`; the flow binds the scope to
  the conversation's session for exactly the turn, and a tool reads
  `ctx.turn` or `ctx.require_secret("cookie")` — a `ToolError` that tells the
  model to stop when there is none. `scope.flag("session_expired")` is how a
  tool leaves a note for the turn: when the turn is over — answered or failed —
  the flow posts one notice per flag, with the flag as its `code` and the
  sentence from `AgentFlow(flag_notices={"session_expired": "…"})` as its text
  (the flag itself when none is given). A program reading the journal acts on
  the code; a person reads the sentence. Chat gateways set no scope: nothing on
  Slack or Mattermost belongs to one turn.
- **An HTTP API for your own frontend** is what the `http` gateway is
  (`crucible.gateways.http`, contract in [http-gateway.md](http-gateway.md)):
  `HttpHub(host, port, callers, dispatcher=…, control=runtime)` plus, per
  agent, `HttpChatClient(hub.turns, name)` as the chat client and `hub.turns`
  as the flow's `tracer`; `GatewayFactory(http_hub=hub)` does this for a
  `GatewayConfig(kind="http")`. The piece that is yours is `callers`, a
  `CallerAuthenticator`: `TokenCallers` trusts a bearer token and two headers,
  and an application whose users prove themselves otherwise (a session cookie
  checked upstream) implements `authenticate(request, body) -> Caller | None`
  itself — and builds the `Caller.turn` scope there, so a credential that came
  with the request reaches the tools of that turn and nothing else. The hub
  fills `TurnScope.turn_id` with the turn's id (and builds a bare scope when the
  authenticator gave none), so a flow or tool keeping a transcript can key it by
  the same id the client polls — and leave out, on a reload, the stored answer
  of a turn the client is still replaying from its journal. `None` is
  the plain 401; raising `CallerRefused(status, code, message, **extra)` answers
  with the application's own status and code in the hub's error envelope
  (`jira_session_invalid`, `identity_mismatch`, `rate_limited`, …). What a
  dialog is and whether to keep one is the application's; the journal lives
  ten minutes past the turn. Your own endpoints (a transcript, "new dialog")
  go on the hub's port through `HttpHub(routes=…, middlewares=…)`, with
  `hub.caller()`, `hub.conversation_key()` and `hub.refuse()` for what the
  hub's handlers have; `hub.drain()` / `await hub.idle(timeout)` are the
  shutdown pair — see [http-gateway.md](http-gateway.md).
- For tests without a `pi` binary, `tests/fakes/fake_transport.py` in this
  repository is an in-process `PiTransport` you can copy: a reactor answers the
  commands the session sends, and the session logic runs unchanged.

## Why not a `build_engine(config)`?

Because apps diverge: a different app may want different interactivity, different
tool gating, a different runtime backend, or a different agent-selection policy.
The wiring helpers keep the common path short while leaving you free to bypass any
of them and wire the pieces by hand. That composability is the point of the kit.
