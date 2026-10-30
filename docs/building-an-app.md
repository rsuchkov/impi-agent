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
| `ToolWiring(ToolSettings, registry=build_registry(…), data_dir=…, interactivity_on=…)` | the tool-server config and the registry over the tool modules the root names | per-agent `enroll`/`profile_env` and `build_server` |

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
from crucible import builtin_tools
from crucible.attachments import AttachmentStore
from crucible.conversation_files import conversation_lookup
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways import GatewayConfig, GatewayFactory, needs_http_receiver
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import AgentSink, InteractionWiring, MappingPresence
from crucible.profiles import FsProfileStore
from crucible.runtimes.pi import build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools import ToolWiring, build_registry
from my_app import my_tools  # your own tool module: each Tool class marked @tool


def build_app(settings, registry, loop_guard):  # registry implements AgentDirectory
    profiles = FsProfileStore(settings.agents_path)
    sessions = open_store(settings.store_backend,
                          name=settings.resolved_db_name, url=settings.db_url)
    attachments = AttachmentStore(settings.attachments_dir, max_bytes=20 * 1024 * 1024,
                                  conversation_max_bytes=200 * 1024 * 1024)

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
    turns = TurnRegistry()  # what belongs to one turn, for the flow, the tools and the widgets
    interactions = InteractionWiring(
        settings.integrations, sessions, presence, turns=turns,
        codec=MattermostCallbackCodec(), needs_receiver=needs_receiver,
    )
    runtime = PiRuntime(pi_bin="pi", session_dir=settings.session_dir,
                        ui_bridge=interactions.ui_bridge)
    # The tools this deployment offers: what the named modules mark @tool.
    tools = ToolWiring(settings.tools, registry=build_registry(builtin_tools, my_tools),
                       data_dir=settings.data_dir,
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
        # The tools whose call reaches the person (a widget, a form, a file):
        # a turn that ends on one is not empty. Without them every tool-only
        # silent turn gets the "came back without an answer" notice.
        posting = [n for n in registry.names() if (t := registry.get(n)) and (t.speaks_to_user or t.posts_to_user)]
        flow = AgentFlow(runtime, profile, sessions, agent_name=spec.name, posting_tools=posting,
                         file_keeper=attachments)  # a file saved under another key follows the conversation
        coalescer = MessageCoalescer(flow, on_arrival=interactions.on_arrival_for(spec.name))
        kind = configs[spec.name].kind
        sinks_by_agent[spec.name] = AgentSink(sink=coalescer, gateways={kind: handle.chat}, default=kind)
        units.append((spec, handle.create_gateway(coalescer)))

    tool_server = tools.build_server(
        directory=registry, interaction_svc=interactions.interaction_svc,
        dotenv_path=settings.dotenv_path,
        conversation_resolver=conversation_lookup(attachments, sessions),
        received_files_for=attachments.received_files_for,  # what a conversation received, for a tool that hands a file on
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
  running turn by that id and resolve it from a tool. What a tool usually
  needs is already resolved — `ToolContext.channel_id`, `user_id` (who spoke
  last), `files_dir` (where what people sent was saved) and `received_files`
  (what the conversation received, with digests; `read` hands the bytes over
  only while the file is the one that arrived) — when the server was built
  with `conversation_resolver=conversation_lookup(attachments, store)`
  (`crucible.conversation_files`) and `received_files_for=attachments.
  received_files_for`: one read of the session record per call, and nothing
  for a session that is not the calling agent's. Do not
  derive the files directory from a conversation id yourself:
  `AttachmentStore` renames an id it cannot use as a directory name.
- **Name a confirmed tool for a person.** `display_name = "Add a comment"` on
  the tool class puts that on its card — "wants to run “Add a comment”." — where
  the id would read as engine output; the id stays on the card as its `tool`
  field (drawn as a `Tool` row in a chat), for the operator and the audit. A
  tool without one is named by its id.
- **Name what a call is about, in one line.** A tool may implement
  `caption(args) -> str | None` (`Captioning` in `crucible.tools`): "Reading
  PROJ-123", "Run the script on <host>" — the thing worked on, never the arguments,
  which are as often a whole script as a key. Synchronous and from the
  arguments alone: it runs inside the runtime's event stream. The flow puts the
  line on `ToolStarted.caption` once (`AgentFlow(captions=…)`, a `CallCaptions`;
  `ChainedCaptions(registry, PiBuiltinCaptions())` adds the runtime's built-ins),
  and every trace shows it: the chat widget's counter and rows, the http
  journal's `tool.started.caption`. The model's remark before a call travels as
  `TextProgress` and is shown the same way (`progress` in the journal); the
  turn's answer is what the model says after its last call. Given
  `received_files_of=` (`AttachmentStore.received_files_of`), a path among the
  arguments that is a file the person sent reaches the caption as the name
  they gave it: "Reading invoice.pdf" says what they attached, not what the
  disk calls it.
- **Say whether the tool's work can be undone.** `Tool.destructive`
  (`ClassVar[bool]`, true unless the author says otherwise, as MCP's
  `destructiveHint`) is the author's word on the tool: a record changed, a
  message sent to others, a thing created or removed. The engine confirms by
  `requires_confirmation` alone; the flag is for an application that lets some
  calls through without the card — a person's standing permission, an "auto"
  mode — and only a tool that says `False` may be. The manifest carries it
  (`destructive`), so an application reads it from the registry rather than
  guessing; a preview's `danger` stays about one call. The engine's own tools
  that only ask, read or show say `False`; an MCP tool takes its server's hint
  when the server is trusted and is destructive otherwise.
- **Say what the call would do, not what the model said.** A confirmed tool
  should implement `describe(ctx, args) -> CallPreview | CannotRun | None` (`Describing` in
  `crucible.tools`; `CallPreview`/`PreviewRow` in `crucible.ports.chat.types`,
  the chat vocabulary, since a preview travels with the card): the
  server calls it before the gate, inside the paused time, with the same
  context `execute` gets, and the card shows its rows — `Status: In Progress →
  Done`, the issue's title beside its key — instead of the raw arguments. A
  verbatim piece — a script, a diff — goes in `block` under `block_label`, with
  `block_language` (`groovy`, `diff`, `json`) for a renderer that highlights:
  Mattermost puts it on the fence, the http journal carries it as
  `block.language`. When the preview finds the call impossible as asked — the
  fragment to replace is not in the file, the account is not connected —
  return `CannotRun(reason)` instead: nobody is shown a card, no question of
  the gate's is spent, and the model hears `cannot run: <reason>` (telemetry
  says `cannot_run`). Read
  the current state from the system there; the arguments are the model's
  account, and the model reads text other people wrote. `danger=True` puts a
  warning on the card; `block_label`/`block` carry a verbatim piece the person
  must read as written — a script, a command line — and reach the card whole,
  with their lines, never cut the way raw arguments are. A row whose value is
  text the person approves as written — a comment, a description — says so
  with `PreviewRow(..., passage=True)`: it is shown whole, its lines kept, as a
  quote, instead of as a one-line fact cut to fit. Every chat draws the card
  from one layout (`crucible.approvals.card_layout`): the preview's `title` as
  the heading, the facts, the passages, the block, and the agent's name with
  the tool's id small at the bottom. A `describe` that raises is logged and
  the card falls back to the arguments, so a preview can never block the
  decision. The gate
  puts the request on the wire as data — `ChatClient.post_confirmation(ref,
  ApprovalCard, actions)` — and each channel draws it: Mattermost as Markdown
  (`crucible.approvals.card_markdown`, hardened by `containment`), Slack as
  native Block Kit fields, the http gateway as a `confirmation` event. Once
  answered, expired or withdrawn, the same post is rewritten with the verdict
  as a card again (`update_confirmation`, no actions left), its facts still on
  it. The preview's `title` is the card's `title` (drawn as the `Action` row,
  the first), not part of the headline: the headline is the engine's sentence,
  every value is the tool's text and takes the channel's hardening. A program
  reading the card gets `title` and `tool` as fields of their own and `fields`
  as only what the call changes — nothing to tell apart by an English label. A chat client of your own
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
  `memory_updated_at(agent, session_id)` says when that memory last changed
  here (the session file's own time), or `None` when it cannot say;
  `reset(agent, session_id)` ends the process (its slot is free at once) and
  deletes the memory; `cancel(session_id)` interrupts the turn in flight and
  keeps the session for the next one — the turn returns with what it had, or
  the session is dropped if the runtime would not stop, with memory on disk
  untouched; `stats()` gives alive / busy / capacity / waiting for a readiness
  check. A flow needs none of this to run a turn, which is why it is a second
  Protocol — but see the next point for the one question a flow may ask it.
- **Confining the runtime's processes.** Pass `scopes=` to `PiRuntime` and
  each process it starts runs under Landlock rules: `AgentProcessScopes`
  (`crucible.process_scopes`) is the ready answer for agents that are
  profiles — give it the profile lookup, a default level (`conversation`,
  `agent`, `off`), where an agent's files are and, for the `conversation`
  level, an async `conversation_files_dir_for(agent, session_id)` that maps
  a runtime session to that conversation's files directory —
  `conversation_files_dir_lookup(attachments, store)`
  (`crucible.conversation_files`) is that answer, the same one the tool server
  takes. `files_dir_env` names the variable
  that tells the process which directory it was given. Or implement the
  `ProcessScopes` port yourself (`crucible.ports.agent.confinement`):
  `scope_for` returns the paths a process may read and write, or `None` for
  unconfined; `release(agent, scope)` is called once that process has ended
  (or failed to start), so whatever you set aside for it can go. The
  runtime adds what it needs itself — its program, its memory, its account.
  Without `scopes=` nothing is confined, and on a kernel that has Landlock
  the runtime says so in a warning when it is built; on a kernel without
  Landlock nothing is confined either, with a warning. A confined process
  reads the model credential and cannot write it, so it cannot refresh an
  expiring OAuth login: run
  `AuthRefresher(program=...).run()` (`crucible.runtimes.pi.auth_refresh`)
  as a loop of your own, which refreshes a login an hour before it expires.
- **Fallback models.** `AgentSpec.fallback_models` (`provider/model`, in
  order) is all an agent needs: the runtime loads its fallback extension for
  such a profile itself and hands it the chain, without the profile's own
  model and repeats. `FsProfileStore(default_fallback_models=…,
  fallback_models_override=…)` gives every agent a list and an operator a
  per-agent say; `crucible.ports.agent.model_ref.check_model_ref` is the
  format's one check. See [runtime-notes.md](runtime-notes.md) for what
  moves a turn on and what does not.
- **Getting the runtime's storage ready at startup.** Make `os.umask(0o077)`
  the first line of `main()`, before anything creates a file, so what the
  application and its runtime processes write is theirs alone. Then, once the
  session store is open and before the runtime starts, await
  `prepare_runtime_storage(settings, store)` (`crucible.runtime_storage`):
  - it creates the runtime's credential and models store in its agent
    directory (`PI_CODING_AGENT_DIR`) when they are missing. A confined
    process may only read them, so it could not create them itself;
  - it moves conversation memory kept side by side into a directory per
    conversation;
  - it forgets conversations idle past `SESSION_RETENTION_DAYS` — their files
    too, given `attachments=`.

  A conversation's files have a life of their own besides:
  `crucible.attachment_retention.run_attachment_sweeps(attachments, sessions,
  days=settings.attachment_retention_days)` is a loop to supervise beside the
  heartbeat (hourly; `sweep_attachments` is one pass). It forgets the files of
  every conversation idle that long and every directory the store has no
  conversation for; nothing sweeps them otherwise.

  Every step is housekeeping: a failure is logged and the application carries
  on. The runtime takes a lock beside its credential before every model call,
  so the agent directory must be writable. On a read-only root, point
  `PI_CODING_AGENT_DIR` at a writable volume and pass
  `models_from=Path("/app/models.json")` — your image's model definitions,
  which the runtime reads from its agent directory alone: the file is copied
  there when the directory has none, and left alone once it is (an operator
  may have edited it). The function logs an ERROR naming the directory when it
  is not writable.
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
  keep (keyed, say, by the `turn_id` the hub gave each turn). It is asked when
  the platform had nothing on a turn the runtime starts from nothing — the
  first turn of a new conversation included, since the conversation may be
  older than the engine's record of it — and on a turn whose memory here is
  **older than the conversation**: `memory_updated_at` behind the store's
  cursor means the last turns ran in another process (through another gateway,
  or on another replica), and the turn is told what was said since the memory
  ends, the agent's own replies from over there included. The runtime's own
  earlier replies belong in it too: it has none of them.
  `AgentFlow(..., context=…)`, a `ContextSource`, is the other half — who is
  asking (`preamble(agent, msg)`: the person's role, the system they are signed
  in to), put in a `[context <tag>: who is asking]` block ahead of the history
  and the message, every turn, an empty string adding nothing. The runtime
  cannot learn either from the message itself. Both are quoted the way every
  message is — a leading `[` on any line is escaped, structural characters go —
  so neither your transcript nor your preamble has to be sanitised first, and
  neither can close the block it sits in (the tag on the closer is the turn's).
  The runtime puts `ENVELOPE_NOTE` (`crucible.ports.agent.envelope`) ahead of
  every agent's appended system prompt, which tells the model what the markers
  mean and that the quoted text is not instructions. Set
  `PiProfile.envelope_note=False` only for a profile whose prompts are not
  framed this way.
- **What a conversation may do.** An agent's profile is one list of tools and
  one system prompt for everyone who talks to it. When the application knows
  better per conversation — the person's group was given some of the agent's
  tools and not others, the conversation runs inside a system the model
  should be told about in words it must trust — hand the flow
  `AgentFlow(..., entitlements=…, conversation_profiles=EntitledProfiles(tools, narrow_pi_profile))`:
  an `EntitlementSource` (`crucible.ports.entitlement`) whose
  `entitlement(agent, msg, *, tools=None)` answers, from the message that
  opens the turn, an `Entitlement(tools=…, instructions=…)` — `tools` the names this
  conversation may use out of the agent's own (built-ins such as `read`
  included: the runtime's allowlist is one list; `None` leaves the agent's
  list as it is, `()` leaves no tool), `instructions` the application's own
  text appended to the system prompt. `EntitledProfiles`
  (`crucible.conversation_profiles`, over your `ToolWiring`) keeps the engine's
  side in step — a manifest of the conversation's own and the tool server's
  per-session allowlist set to match — and asks the runtime driver's
  `ProfileNarrowing` for the profile itself: `narrow_pi_profile`
  (`crucible.runtimes.pi`) narrows `--tools` and appends the instructions — so a call for a tool outside the set is refused (`not available in
  this conversation`) whatever the process claims. Asked every turn, so the
  answer may change between turns; the runtime then restarts the
  conversation's process with the new profile and resumes its memory from
  disk — one cold start per change, no lost conversation. `instructions` are
  **trusted** text, unlike the preamble: never put a person's words there. A
  batch merged from several people's messages (a channel, a thread) asks the
  source about each of them once and runs with what they are jointly entitled
  to — the tools all of them may use, instructions only where every answer is
  the same (`shared_entitlement`). The message names the gateway it came
  through (`msg.gateway`), so a source can entitle a conversation on Slack to
  fewer tools than one on Mattermost; `GatewayEntitlements`
  (`crucible.conversation_profiles`) is that source over the agent's own
  `runtime.tools_by_gateway`, and takes an `inner` source to join with — the
  tools both allow, the inner's instructions — for an application with rules of
  its own as well. The inner source is called with `tools=` the gateway's
  ceiling (`entitlement(agent, msg, *, tools=None)`), so instructions that
  speak of tools speak of the ones the conversation will have; a source the
  flow calls directly gets `tools=None`.
  Pass `manifests_dir=` and `attachments=` to `purge_idle` (or call
  `prepare_runtime_storage`, which does) so a forgotten conversation's manifest
  and files go with its memory.
- **Turns the engine starts.** A person's message opens a turn through a
  gateway; the engine opens one by itself through a `TurnDispatcher`
  (`crucible.ports.engine_turn`) — the scheduler does for a `turn`-mode task,
  and an application does when something it watches has happened and the
  agent should come back to the conversation with its memory: a pipeline
  finished, a ticket moved. `TurnRequest` names the agent and the
  conversation by the engine's own keys (the platform's ids on a chat
  gateway; on the http gateway the realm-namespaced key `HttpHub.conversation_key`
  spells), a `message_id` of the run's own, `user_id`/`username` for who it
  appears to come from, and two things the application adds: `event`, an
  `EventReport(source, text)` the flow quotes as data in a
  `[context <tag>: event reported by <source>]` block ahead of the speaker
  line (`text` is then the one-phrase summary the line carries), and `turn`,
  a `TurnScope` of attributes and secrets for the tools — only what the
  application still holds: with no fresh credential a gated write ends in an
  unanswered card and a read in a refused secret, so the agent reports and
  asks rather than acts. The dispatcher is the composition root's: `impi`'s
  `SinkTurnDispatcher` puts the request into the agent's sink as a synthetic
  message (`requested_message` builds it) and, for an agent on the http
  gateway, hands it to `HttpHub.run_turn`, which opens a turn the client reads
  like any other (`startedBy: "engine"`). The conversation's record is stamped
  (`last_engine_turn_at`), and `HttpHub(engine_turns=engine_turns_lookup(sessions))`
  answers `GET …/conversations?startedSince=` from it, so a program shows a
  person what moved with one request. **Watching for the event** is the
  scheduler's: `TaskService.watch(agent, runtime_session_id, name=, watch_key=,
  every_s=, until=, prompt=)` (from a tool) or `TaskAdmin.watch_in(...)` (from
  your own code) opens a `watch` task — `watch_key` is an opaque key of yours
  (`"gitlab:pipeline:1234"`) — and `Scheduler(watches=…)` takes your
  `WatchHook` (`crucible.scheduler.ports`), whose `check(agent, watch_key)` answers
  `WatchPending()`, `WatchDone(EventReport, turn=TurnScope | None)` or
  `WatchGone(reason)`. The scheduler asks every `every_s` seconds, writes no
  history while the answer is pending, starts the turn once on done — through
  the same dispatcher, with your event and scope — and ends the task; on
  `until` it starts the turn itself with a report that nothing happened. The
  rows, the lease and the catch-up after a restart are the scheduler's
  already, so a watch survives what a task survives
  ([tasks.md](tasks.md)).
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
from crucible import builtin_tools
from crucible.ports.chat.directory import AgentInfo, StaticDirectory
from crucible.tools import ToolWiring, build_registry
from my_app import my_tools

directory = StaticDirectory([AgentInfo(spec.name, spec.role, spec.description, "", "")])
tools = ToolWiring(settings.tools, registry=build_registry(builtin_tools, my_tools),
                   data_dir=settings.data_dir, interactivity_on=False, confirmations_on=True)
tools.enroll(spec, None)                       # no admin client: no chat
profile = replace(build_pi_profile(spec), env=tools.profile_env(spec) or {})
runtime = PiRuntime(..., extra_extensions=[str(EXTENSION_PATH.resolve())])
tool_server = tools.build_server(
    directory=directory, interaction_svc=None, dotenv_path=settings.dotenv_path,
    conversation_resolver=my_resolver,         # (agent, session id) -> SessionConversation
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
  preview and its `display_name`, and pauses the turn's clock through `clock=runtime` while it waits.
  The gate answers with a `ConfirmOutcome` (`crucible.approvals`): `ALLOWED`
  runs the call; `DENIED`, `UNANSWERED` and `NO_APPROVER` refuse it, and the
  server tells the model which it was — answer `UNANSWERED` for a silence, so
  the model asks the person again later instead of reading it as a no. A gate
  that decides not to ask at all — a budget of questions per turn is spent, a
  policy refuses the call outright — answers `NotAsked(reason)`, and the model
  hears `not asked: <reason>` with a note not to retry: the three outcomes are
  facts about a person, this one is the gate's own words.
- `conversation_resolver` maps the calling agent and the runtime session id a
  tool call carries — the same id you passed to `run_stateful` — to a
  `SessionConversation` (`crucible.ports.conversation`): channel and kind as
  the conversation is addressed on the gateway the last message came through,
  last speaker, files directory, or None for a session that is not that agent's.
  Over crucible's own session store, `conversation_lookup(attachments, store)`
  (`crucible.conversation_files`) is that resolver.
- **Per-turn data for tools — a credential the person sent with the message,
  say — rides in `IncomingMessage.turn` as a `TurnScope`.** Build one in your
  gateway or API handler (`TurnSecrets({"cookie": …})` for anything that must
  never be stored or printed — it shows its names, never its values, and
  refuses pickling), hand `AgentFlow(..., turns=TurnRegistry())`,
  `build_server(..., turns=<the same registry>)` and
  `InteractionWiring(..., turns=<the same registry>)` (and
  `ChatFileService(places=)`); the flow binds the scope to the conversation's
  session for exactly the turn, and a tool reads
  `ctx.turn` or `ctx.require_secret("cookie")` — a `ToolError` that tells the
  model to stop when there is none. `scope.flag("session_expired")` is how a
  tool leaves a note for the turn: when the turn is over — answered or failed —
  the flow posts one notice per flag, with the flag as its `code` and the
  sentence from `AgentFlow(flag_notices={"session_expired": "…"})` as its text
  (the flag itself when none is given). The registry also holds **where the
  turn answers** (`TurnPlace`: the gateway and the thread, bound whether or
  not the message carried a scope): the tool gate, the widgets, the UI bridge
  and the file service post there — in the thread under the person's message
  when a direct message is answered so (`DM_REPLIES=thread`) — and a widget's
  record keeps that thread, so a click resumes the turn in it. Without the
  registry they post at the conversation's standing address. The registry is
  also how the engine tells a running turn what happened to it: the tool gate
  feeds it `CallAwaiting(tool)` when a card is up (`ToolGate(events=turns)`,
  which `InteractionWiring(turns=)` does), and the flow has bound the turn's
  event observer, so the widget's counter and the http journal say "waiting
  for you" on that call. A program reading the journal acts on
  the code; a person reads the sentence. Chat gateways set no scope: nothing on
  Slack or Mattermost belongs to one turn.
- **An HTTP API for your own frontend** is what the `http` gateway is
  (`crucible.gateways.http`, contract in [http-gateway.md](http-gateway.md)):
  `HttpHub(host, port, callers, dispatcher=…, control=runtime,
  turns=HttpTurns(cards=store, traces=store))` plus, per agent,
  `HttpChatClient(hub.turns, name)` as the chat client and `hub.turns` as the
  flow's `tracer`; `GatewayFactory(http_hub=hub)` does this for a
  `GatewayConfig(kind="http")`. `cards` is where a conversation's cards raised
  on another gateway wait (`ports.held_cards.HeldCards`, which the store
  implements) — give it the store, or every replica shows only what its own
  turns raised; `traces` is where a turn's calls are kept once it has ended
  (`ports.traces.Traces`, the store again), read back by the token
  `turn.finished` carries after the journal is gone. An agent reached through
  the http gateway **and** a chat gateway hands the flow one tracer for both:
  `AgentFlow(tracer=TracerByGateway({"http": hub.turns}, default=ToolTrace(…)))`
  (`crucible.flows.tracers`) picks by the gateway the turn came through — the
  journal for an http caller, the widget under the reply in chat — by the
  gateway's name, never by the type of the chat client, so a client you wrap
  is still answered right. The piece that is yours is `callers`, a
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
  shutdown pair, and `HttpHub(readiness_probes=…)` gives your own dependencies a
  say in `/readyz` — see [http-gateway.md](http-gateway.md).
- **One agent, several gateways.** Build one `AgentFlow` and one
  `MessageCoalescer` per agent and hand every gateway's `create_gateway` the
  same coalescer; register the agent's presence as `AgentSink(sink,
  gateways={kind: chat}, default=kind)`. Enrol the agent once per gateway
  (`ToolWiring.enroll(..., gateway=kind)`): the token is the agent's, the
  admin client and the capabilities are the gateway's, and the server checks
  the conversation's own gateway on every call. Log every gateway in and give
  the flow each identity (`add_identity`). When a thread in one chat and a
  dialog in another are one conversation to the person, say so with a
  `ConversationKey` (`ports.chat.keying`) given to the flow, the coalescer and
  the gateways (`GatewayFactory(keying=…)`, `InternalRoutes(keying=…)`); the
  engine then runs them through one runtime session, keeps the conversation's
  address on each gateway and saves what people attach under that key, where a
  tool's files directory is. The same person also carries a
  different id on each gateway; `PersonKey.key(gateway, user_id)` (same module,
  given to `InteractionWiring(people=)` or straight to `ToolGate` and
  `InteractionDispatcher`) says which ids are one person — the one place the
  engine compares people is who may answer a confirmation card, and with it a
  card for a turn started from a web panel is answered by the same person's
  click on its Slack copy. Without one, each platform id is a person of its
  own. `impi` wires all of this from
  `AGENTS_GATEWAY__<AGENT>=mattermost,slack`.
- **A click that carries data.** An `Action` has three strings for three
  readers: `label`, what the person sees on the control and what the message
  says they chose afterwards ("Selected: Approve"); `value`, what the agent
  hears as the click's text; `data`, the application's own — the id of the
  plan being approved — shown to nobody and carried with the click. The
  synthetic message the click becomes has `click: Click(action_id, data,
  interaction_id)` (`crucible.ports.chat.types`) beside its text; a flow or a
  handler of the application's reads `msg.click.data`, the model never sees
  it. Buttons carry `data` on both chat gateways, menus on Mattermost only
  (a Slack menu has nowhere to put it); at most `ACTION_DATA_MAX` (256)
  characters, since a Slack button carries everything in one bounded value —
  and not a secret: on Slack it sits in the message's own blocks.
- **A Slack app of your own.** The Slack gateway runs on a Bolt `AsyncApp`;
  by default the factory builds one from the agent's bot token. An application
  with a Home of its own, commands or views beside the engine's hands the app
  in instead — `GatewayFactory(slack_apps={"<agent>": app})` — with its own
  listeners already registered on it. Bolt gives a request to the first
  listener that matches, in order of registration, so what the application
  registered before handing the app over is its own; the gateway then takes
  the `message` event, the agent panel's events (`app_home_opened`,
  `app_context_changed`, `agent_session_stopped`,
  `agent_session_title_changed`), engine widgets (`cruxw…` actions, the
  `crux_form` view), the prefixed shortcuts (`SLACK_COMMAND_PREFIX`), every
  slash command not taken before it, and `reaction_added`. What the Home
  shows is the application's: give the factory a `SlackHome`
  (`crucible.gateways.slack`) with `slack_home=` — `opened(user_id, tab,
  publish)` is handed `views.publish` for the person who opened it
  (`PublishHome`: a coroutine taking the view as Slack takes it, `type: home`,
  blocks, `private_metadata` …) and publishes what it wants, when it wants,
  and what it wants instead when Slack refuses a view (`invalid_blocks`);
  `context_changed(user_id, context)` hears what the person looks at. Without
  a `SlackHome` the events are acknowledged and nothing is shown. A chat client that has a view of the conversation beside its
  messages implements `ConversationView` (`ports.chat.client`,
  `set_busy(ref, busy)` and `set_waiting(ref, waiting)`): the flow marks the
  conversation busy for the turn and free after it — Slack's agent session
  shows its indicator and Stop button, whose press ends the turn as `/stop`
  does — and the tool gate marks it waiting while a confirmation card is up
  (Slack: the session `suspended`, its own status for "cannot go on until the
  user intervenes") and busy again once it is answered.
- **Tools from MCP servers.** `crucible.mcp` ([mcp.md](mcp.md)) is a source
  of tools for the registry: `mcp = McpServers(McpLibrary(Path(agents) /
  "_mcp"), values)` reads the servers an operator declared and their pinned
  snapshots, `values` being what a `${MCP_…}` in a server's entry is filled
  from (`impi`: `Settings.mcp_values()`, the process environment then `.env`);
  `build_registry(builtin_tools, my_tools, mcp.registry_tools())` puts their
  tools beside yours as `mcp__<server>__<tool>`, and `runtime.tools` may name a
  whole server as `mcp__<server>__*`. At start, before the tool server:
  `report = await mcp.start()` connects every server and compares its tools
  with the snapshot; `tools.withdraw(report.withdrawn_tools)` takes the
  changed untrusted ones out of every allowlist and manifest,
  `tools.restore(report.restored_tools)` hands back what a review pinned
  again, `tools.refresh_manifests()` when `report.refreshed` names a trusted
  server whose definitions moved (`report.connected`, `unreachable`,
  `unpinned`, `skipped` are for the log). The same three calls apply every
  later `CheckReport`: run `mcp.run(interval_s, apply)` as a supervised loop
  (it rechecks on the timer and when a server says its tools changed) and
  call `mcp.recheck(reviewed=True)` on your reload signal, before the profiles
  are re-read — the one check that reads the snapshots from disk again. At
  stop, `await mcp.stop()`. A tool
  whose header is `${connection:<provider>}` needs `connections` on the tool
  server and acts only in a direct message.
- **Telemetry.** Every producer takes a `Telemetry` (`crucible.ports.telemetry`;
  [telemetry.md](telemetry.md)): `AgentFlow(telemetry=)`,
  `tools.build_server(telemetry=)`, `InteractionWiring(telemetry=)`,
  `McpServers(telemetry=)`, `Scheduler(telemetry=)`,
  `OAuthConnections(telemetry=)`, and `tell_engine_state(...)` as a supervised
  loop for the timer. Build one receiver — `impi`'s `LogTelemetry`, or your
  own `record(event)` that feeds an exporter or a table — and hand the same
  instance to all of them. None of them needs one; without it nothing is told.
- **A person's file in an MCP call.** A tool whose argument the server's
  schema declares as content (`contentEncoding: base64`) takes a file the
  person attached ([mcp.md](mcp.md)). Two wires carry it: `build_server(
  received_files_for=attachments.received_files_for)` gives the tool the
  record of what the conversation received, and `AgentFlow(file_keeper=
  attachments)` moves a file a gateway saved under another key into the
  conversation as the flow keys it. Without the first, such a tool answers
  that the engine keeps no files.
- **Several engine processes.** A chat connection the platform pushes events
  into — a bot's websocket, Slack's Socket Mode — must be open in one process
  at a time: the platform hands each event to one of its open connections by
  its own choice, so two replicas would split a conversation between two
  runtime memories. Wrap such a gateway in `LeasedGateway(gateway, store,
  name="gateway:slack:<agent>", owner="<host>:<pid>")` (`crucible.gateways`):
  it runs the gateway only while it holds the named lease in the store
  (`ports.lease.Leases`, implemented by the sqlite and MongoDB stores,
  compare-and-swap), renews it every 10 s for 30 s, closes the connection when
  the lease is lost and queues up again; a replica that stops renewing — a
  crash, a rollout — is replaced within one lease. Sending needs no connection
  and works from every replica. What moves with the lease: the Slack
  conversations' turns run on the holder, whose disk may not have their
  runtime memory — such a turn starts over with the history
  (`HistorySource`), as a rescheduled pod does; a card waiting on the old
  holder expires with it. `impi` wraps its Mattermost and Slack gateways this
  way; on one process with sqlite the lease is always its own.
- **Several engine processes, one conversation.** Beyond the connection, two
  things are one process's and not the store's: the runtime's memory of a
  conversation (its session files) and the questions a turn is waiting on (a
  card's token lives where the card was posted). So a conversation has an
  **owner** (`ports.replica`): the replica whose runtime holds it. Give each
  process a `Replica(id, url)` — a random id, its own address on the http hub's
  port — and wrap each agent's flow in `OwnedFlow(flow, store, replica,
  forwarder, agent=…, forwarded={"mattermost", "slack"})` (`crucible.flows`):
  before a turn it claims the conversation in the store (`claim_conversation`,
  compare-and-swap); free, lapsed or its own, the flow runs here; another live
  replica's, the batch goes to that replica's hub (`HubForwarder`, over
  `InternalRoutes` mounted on its `HttpHub(routes=…)`) and the turn's outcome
  comes back. A message through a gateway that only this process can answer
  from — a request a balancer pinned here, a websocket open here — is not in
  `forwarded`: it takes the conversation over, and the owner's memory being
  older than the conversation is what the catch-up above covers. A replica is
  alive while its `ReplicaHeartbeat` (`crucible.replica_heartbeat`) renews the lease
  `replica:<id>` with its address; stopping releases it, so a clean shutdown
  hands its conversations over at once. Clicks follow the same idea: every
  token the gate and the widget bridge mint carries the replica's id in front
  (`tag_token`), and the dispatcher, finding nothing waiting for a token
  another replica minted, hands the click to that replica (`ClickForwarder`)
  and answers with what it did — and asks it, the same way, whether a person
  may answer a card it holds before the card is shown (`ask_may_answer`). One
  shared secret guards the internal routes,
  on the hub's own port. `impi` turns all of this on with `REPLICA_URL` and
  `REPLICA_TOKEN`; with either unset nothing is owned or forwarded.
- **Testing without a model.** `crucible.runtimes.pi.testing.ScriptedHost`
  replaces the runtime's process and nothing else. Hand it to the runtime as
  `PiRuntime(hosts=HostRouter(ScriptedHost(script)), …)`, where
  `script(request, message)` returns a `ScriptedTurn`: the `text` the model
  answers with, the `tool_calls` it makes first (`ToolCall(name, args)`), or
  an `error` its service failed with. The runtime, its sessions, the spawn
  request it builds and your tool server stay the real ones. A tool call goes
  over HTTP with the token, the session id and the session proof the process
  was given, as the runtime's tool extension sends them, so the allowlist,
  the proof check and the confirmation gate are all exercised. A turn that
  decides on what a tool answered is a coroutine with a third parameter,
  `script(request, message, tools)`: `await tools.call(ToolCall(…))` sends
  the call there and then and returns its `ToolOutcome` (status and body),
  so the script can branch on it before it answers. A `ToolCall(headers=…)`
  replaces the headers the process was given — another session's id, a proof
  of its own making — for the test that the server turns such a call away.
  `host.opened` holds every spawn request, `host.tool_outcomes` what each
  call came back with.
- **Acting as the person, at their own provider.** `crucible.connections`
  connects a person's account at an OAuth provider once and hands a tool
  their token on every turn, refreshed when it is about to end; the model
  never sees it. Yours: the `OAuthProvider` (one per provider), who the owner
  is, where the provider sends the person back (you call
  `finish(state, code, owner=…, facts=…)` from there — `CallbackServer(…,
  start_again=…)` is a ready page, told what its "try again" should say), and
  an `AccountPolicy` whose `refusal(owner, provider, account, facts)` says why
  an account may not be bound to that person, or None; `facts` is what your
  route knew about the person when it finished the connect (an email, a
  realm), passed as plain strings rather than through a context variable
  around the call. The consent URL `begin` returns goes to the
  person privately: whoever holds it can finish the connect. Seal with
  `FernetSealer(keys)` (`crucible[connections]`); the first key seals, every
  key opens. `OAuth2Provider(OAuth2Config(...))` covers a provider that
  speaks the standard (`gitlab(base_url, …)` is GitLab). A tool reaches the
  person's connections as `ctx.require_connections()` — `grant(provider)` for
  the token, `forget(grant)` after a 401 — and declares `CAP_CONNECTIONS`;
  wire `ToolWiring(connections_on=True)` and
  `build_server(connections=connection_access(service, owner_for))`, with
  `owner_for(agent, user_id, turn)` saying whose connections a turn uses — or
  None where nobody's: a gateway whose user ids your calling programs assert,
  a turn no person started (mark yours in its `TurnScope`). What a tool sees
  (`ConnectionAccess`, `TokenGrant`, `ProviderAccount`) is
  `crucible.ports.connections`, so the tool layer never loads the store. Name
  your settings families in `Settings.APP_SECRET_PREFIXES` (and
  `FsProfileStore(app_secret_prefixes=…)`), so no agent can be handed them
  by a pattern.
  To tell the person where they started that it worked, pass
  `begin(owner, provider, return_to=…)` — opaque to the library, kept with
  the link — and `OAuthConnections(on_connected=…)`, which is handed a
  `Connected` carrying it once the connect is stored. A screen that started
  it knows its own message from `ScreenState.post_id` (set on every click)
  and redraws it through `redraw_screen`. A screen that shows a person's own
  things declares `direct_only = True` and is refused outside a direct
  message however it is opened; `InteractionService.open_screen(…, data=…)`
  opens one at a state of the engine's choosing.
- **Keeping secrets out of the log.** `crucible.redaction.ScrubbingFilter`
  on each handler of the root logger scrubs every record. It replaces the
  value and keeps the name for: the tool token and session proof by header
  and by variable, a receiver's secret path, bearer and Slack tokens, OAuth
  values, credentials in a URL, private keys. `scrub_data` does the same
  through an error report, so `before_send=lambda event, hint:
  scrub_data(event)` covers Sentry without crucible depending on it. Your
  own patterns go in `extra=`; a pattern's first group is kept. `impi` and
  `ward` install the filter at startup.

## Why not a `build_engine(config)`?

Because apps diverge: a different app may want different interactivity, different
tool gating, a different runtime backend, or a different agent-selection policy.
The wiring helpers keep the common path short while leaving you free to bypass any
of them and wire the pieces by hand. That composability is the point of the kit.
