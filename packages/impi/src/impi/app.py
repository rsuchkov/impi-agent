"""Composition root: the ONLY place where concrete adapters meet each other.

Everything is wired by constructor from one Settings instance — no globals.
Adding a gateway/runtime/store = build a different concrete here, same ports.
The App manifest holds PORTS where a port suffices (runtime, sessions), so
swapping a concrete never touches the run loop.

Multi-agent model: one engine process hosts N gateways (one bot account per
agent) sharing one runtime, one store and one registry.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import random
import secrets
import signal
import socket
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

from crucible.approvals import PendingApprovals
from crucible.attachments import AttachmentStore
from crucible.confinement import landlock_abi
from crucible.conversation_files import (
    conversation_files_dir_lookup,
    conversation_lookup,
    engine_turns_lookup,
)
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.flows.owned import OwnedFlow
from crucible.gateways import (
    GatewayConfig,
    GatewayFactory,
    GatewayHandle,
    LeasedGateway,
)
from crucible.gateways.http import (
    HttpChatClient,
    HttpHub,
    HttpTurns,
    HubForwarder,
    InternalRoutes,
    TokenCallers,
)
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.gateways.ws import WsHub
from crucible.interactions import (
    AgentSink,
    InteractionsServer,
    InteractionWiring,
    MappingPresence,
    ToolTrace,
    TraceScreen,
)
from crucible.interactions.files import ChatFileService, default_roots
from crucible.interactions.screens import ScreenRegistry
from crucible.loopguard import LoopGuard
from crucible.mcp import CheckReport, McpLibrary, McpServers
from crucible.ports.agent import (
    AgentProfile,
    AgentRuntime,
    AgentSpec,
    CallCaptions,
    ChainedCaptions,
    RuntimeControl,
)
from crucible.ports.attachments import FileKeeper
from crucible.ports.chat.flow import Flow
from crucible.ports.chat.gateway import (
    GATEWAY_HTTP,
    GATEWAY_MATTERMOST,
    GATEWAY_SLACK,
    GATEWAY_WS,
    AgentIdentity,
)
from crucible.ports.lease import Leases
from crucible.ports.replica import Replica, Replicas
from crucible.process_scopes import AgentProcessScopes
from crucible.profiles import CompositeProfileStore, ProfileStore
from crucible.profiles.errors import ProfileError
from crucible.reloader import ProfileReloader
from crucible.replica_heartbeat import ReplicaHeartbeat
from crucible.runtime_storage import prepare_runtime_storage
from crucible.runtimes.pi import (
    EXTENSION_PATH,
    HostRouter,
    LocalHost,
    RemoteHost,
    build_pi_profile,
    with_tool_env,
)
from crucible.runtimes.pi.auth_file import configured_providers
from crucible.runtimes.pi.auth_refresh import AuthRefresher
from crucible.runtimes.pi.captions import PiBuiltinCaptions
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.scheduler.admin import TaskAdmin
from crucible.scheduler.service import Scheduler
from crucible.skills import SkillLibrary
from crucible.store import open_store
from crucible.store.base import SessionStore
from crucible.tools import SessionProofBook, ToolServer, ToolWiring
from crucible.turns import TurnRegistry
from crucible.unit import AgentUnit
from impi.config import ImpiSettings
from impi.connections import Connections, RedrawOnConnect, build_connections
from impi.connections_screen import ConnectionsScreen
from impi.deployment_report import (
    agents_dir_extensions,
    confinement_report,
    connections_report,
    fallback_report,
    needs_receiver,
    select_specs,
)
from impi.engine_tools import engine_tool_registry
from impi.gateways import resolve_gateways
from impi.profiles import (
    IMPI_ROOT,
    build_pi_env,
    open_profile_stores,
)
from impi.provisioning import ensure_receiver_secret
from impi.registry import RegistryService
from impi.scheduling import PresenceNotifier, RuntimePromptRunner, SinkTurnDispatcher
from impi.skill_screen import SkillScreen
from impi.task_screen import TaskScreen

logger = logging.getLogger(__name__)


def build_pi_extensions(settings: ImpiSettings) -> list[str]:
    """pi extensions loaded via -e on every spawn.

    The engine's own tool bridge ships with the pi driver and is always loaded
    first; any extra extensions in the agents directory
    (``_extensions/*/index.ts``) are appended, so the agents directory can add its
    own without touching the engine.
    """
    paths = [str(EXTENSION_PATH.resolve())]
    if not settings.agents_path:
        return paths
    ext_root = Path(settings.agents_path) / "_extensions"
    present, enabled = agents_dir_extensions(ext_root, settings.runtime_extension_names())
    for name in present:
        if name not in enabled:
            logger.warning(
                "extension %s is present in %s but not enabled — name it in "
                "RUNTIME_EXTENSIONS to load it into every agent", name, ext_root,
            )
    for name in settings.runtime_extension_names():
        if name not in present:
            logger.warning(
                "RUNTIME_EXTENSIONS names %s, but %s/%s/index.ts does not exist", name, ext_root, name,
            )
    paths += [str((ext_root / name / "index.ts").resolve()) for name in enabled]
    return paths


def build_agent_hosts(
    settings: ImpiSettings,
    specs: Sequence[AgentSpec],
    files_dir_for: Callable[[str], Path] | None = None,
) -> HostRouter:
    """Where each agent's runtime runs.

    The default is a child process of this one — what a deployment that has not
    turned the axis on keeps doing, and what the engine falls back to for any
    agent whose own host is not configured yet. An agent gets a host of its own
    the moment there is a token for it, which is what `agent sync` writes when
    it creates the container.
    """
    router = HostRouter(LocalHost(program=settings.pi_bin))
    if not settings.agent_hosts_enabled:
        return router
    library = settings.resolved_skills_path
    for spec in specs:
        url, token = settings.agent_host_for(spec.name)
        if not url:
            logger.warning(
                "agent %s has no container of its own yet — it runs in the engine "
                "(run `impi agent sync` to give it one)",
                spec.name,
            )
            continue
        router.add(
            spec.name,
            RemoteHost(
                url=url,
                token=token,
                library_root=library if library.is_dir() else None,
                files_root=files_dir_for(spec.name) if files_dir_for else None,
                connect_timeout=settings.agent_host_timeout,
            ),
        )
    if router.remote_agents:
        logger.info(
            "Agents running in containers of their own: %s",
            ", ".join(router.remote_agents),
        )
    return router


# The variable naming the directory an agent's process writes files to send:
# its agent's, or its conversation's when it is confined to one.
FILES_DIR_ENV = "AGENT_FILES_DIR"

def _spec_or_none(profiles: ProfileStore, name: str) -> AgentSpec | None:
    try:
        return profiles.get(name)
    except ProfileError:
        return None


@dataclass
class App:
    settings: ImpiSettings
    runtime: AgentRuntime
    sessions: SessionStore
    registry: RegistryService
    units: list[AgentUnit]
    tool_server: ToolServer | None
    integrations: InteractionsServer | None
    reloader: ProfileReloader
    # The commands the engine answers itself. Which words are bound is a wiring
    # decision worth being able to assert on: an unbound one silently becomes an
    # ordinary agent turn.
    screens: ScreenRegistry
    ws_hub: WsHub | None = None
    http_hub: HttpHub | None = None
    scheduler: Scheduler | None = None
    # The tool widget under a reply. None when interactivity or the widget is
    # off; the screen that answers its clicks is registered either way.
    tracer: ToolTrace | None = None
    # Keeps the runtime's OAuth login fresh, so a confined process — which may
    # only read the credential — never has to refresh it itself.
    auth_refresher: AuthRefresher | None = None
    # People's own accounts at OAuth providers, and the page a provider sends
    # them back to. None when no provider is configured.
    connections: Connections | None = None
    # Each agent's own user id on its platform, filled as the gateways log in:
    # what tells a command typed in the agent's direct message from one typed
    # in somebody else's.
    agent_user_ids: dict[str, str] = dataclasses.field(default_factory=dict)
    # The tool wiring (allowlists, per-gateway capabilities) and the live presence
    # map (agent -> its sink and chat clients), for whoever operates the app.
    tools: ToolWiring | None = None
    sinks_by_agent: dict[str, AgentSink] = dataclasses.field(default_factory=dict)
    # This replica among the others sharing the store, when there are others:
    # its heartbeat (alive while it beats) and its client for their hubs.
    heartbeat: ReplicaHeartbeat | None = None
    forwarder: HubForwarder | None = None
    # The deployment's MCP servers, connected at start and closed at stop.
    mcp_servers: McpServers | None = None


def _engine_version() -> str:
    """The running build, recorded in the scheduler's heartbeat so a stale row
    names the version that wrote it."""
    try:
        return metadata.version("impi")
    except metadata.PackageNotFoundError:  # a source checkout without an install
        return ""


def _signal_reload() -> None:
    """Re-read the agent profiles in place — the same thing SIGHUP from `impi
    reload` does, raised from inside the process that installs the handler."""
    os.kill(os.getpid(), signal.SIGHUP)



class ProfileBuilder:
    """Maps a spec onto its runtime profile: the backend mapping plus the agent's
    gateway formatting hint and, when tools are on, its tool env + a freshly written
    manifest. The sole place profiles are built — used at boot and, as the reloader's
    ``build_profile``, on every hot-reload (re-reading live hints/caps/env).
    """

    def __init__(self, tools: ToolWiring, *, engine_owned: frozenset[str] = frozenset()) -> None:
        self._tools = tools
        # Agents that are part of the engine (support): their processes inherit
        # the engine's whole environment, as the CLI they run does. Everyone
        # else inherits the base set plus what their profile names.
        self._engine_owned = engine_owned
        self.hints: dict[str, str] = {}  # agent -> its gateway's formatting rules

    def set_hint(self, name: str, hint: str) -> None:
        self.hints[name] = hint

    def build(self, spec: AgentSpec) -> AgentProfile:
        base = build_pi_profile(spec)
        # The runtime tells every agent how its prompt is framed; what is
        # added here is its gateway's formatting rules.
        base = dataclasses.replace(
            base,
            append_system_prompt=self.hints.get(spec.name, ""),
            inherit_all=spec.name in self._engine_owned,
        )
        return with_tool_env(base, self._tools.profile_env(spec))


# Gateway kinds that hold a connection the platform pushes events into (a bot's
# websocket, Socket Mode) — the ones that must run in one process at a time. The
# http and ws gateways are servers behind whatever balances them; nothing to lease.
CONNECTION_GATEWAYS = frozenset({GATEWAY_MATTERMOST, GATEWAY_SLACK})


class _TracerByChat:
    """One tracer per kind of chat: an http caller's turn writes its tool
    events into the caller's journal, a chat turn draws the widget. Chosen per
    turn by the chat the message came with, so an agent reached through both
    is traced right on each."""

    def __init__(self, journal: HttpTurns | None, widget: ToolTrace | None) -> None:
        self._journal = journal
        self._widget = widget

    def begin(self, record, chat, *, anchor=None):
        if isinstance(chat, HttpChatClient) and self._journal is not None:
            return self._journal.begin(record, chat, anchor=anchor)
        if self._widget is not None:
            return self._widget.begin(record, chat, anchor=anchor)
        return _NoTrace()


class _NoTrace:
    def on_event(self, event) -> None:
        return None

    async def end(self, *, interrupted: bool = False) -> None:
        return None


def _posting_tools(tools: ToolWiring) -> frozenset[str]:
    """The registry's tools whose call puts something in front of a person
    (``speaks_to_user`` or ``posts_to_user``): a turn that called one and wrote
    nothing has reached them."""
    registry = tools.registry
    if registry is None:
        return frozenset()
    return frozenset(
        name for name in registry.names()
        if (tool := registry.get(name)) is not None and (tool.speaks_to_user or tool.posts_to_user)
    )


def _build_unit(
    spec: AgentSpec,
    handles: dict[str, GatewayHandle],
    *,
    runtime: AgentRuntime,
    control: RuntimeControl,
    sessions: SessionStore,
    profiles: ProfileBuilder,
    interactions: InteractionWiring,
    sinks_by_agent: dict[str, AgentSink],
    inline_image_max_bytes: int,
    tracer: ToolTrace | HttpTurns | _TracerByChat | None,
    turns: TurnRegistry,
    captions: CallCaptions,
    posting_tools: Collection[str] = (),
    file_keeper: FileKeeper | None = None,
    replica: Replica | None = None,
    replicas: Replicas | None = None,
    forwarder: HubForwarder | None = None,
) -> AgentUnit:
    profile = profiles.build(spec)
    flow = AgentFlow(
        runtime, profile, sessions,
        agent_name=spec.name, posting_tools=posting_tools, file_keeper=file_keeper,
        inline_image_max_bytes=inline_image_max_bytes,
        tracer=tracer,
        turns=turns,
        captions=captions,
        # A conversation the runtime has forgotten (sessions wiped, an operator's
        # reset) gets its thread replayed again instead of starting mid-air.
        control=control,
    )
    served: Flow = flow
    if replica is not None and replicas is not None and forwarder is not None:
        # A conversation runs where it lives: another replica's, alive, gets
        # the batch over its hub; a message through a gateway nothing else can
        # answer from takes the conversation here.
        served = OwnedFlow(
            flow, replicas, replica, forwarder, agent=spec.name, forwarded=CONNECTION_GATEWAYS,
        )
    coalescer = MessageCoalescer(served, on_arrival=interactions.on_arrival_for(spec.name), agent=spec.name)
    # Record the agent's live presence in the app-owned registry; interactions read
    # it lazily through MappingPresence. One sink, a chat client per gateway, the
    # first gateway the default.
    kinds = list(handles)
    sinks_by_agent[spec.name] = AgentSink(
        sink=coalescer, gateways={kind: handle.chat for kind, handle in handles.items()}, default=kinds[0],
    )
    return AgentUnit(
        spec=spec, flow=flow,
        gateways={kind: handle.create_gateway(coalescer) for kind, handle in handles.items()},
    )


def _build_units(
    specs: list[AgentSpec],
    configs: dict[str, list[GatewayConfig]],
    engine_names: set[str],
    *,
    settings: ImpiSettings,
    runtime: AgentRuntime,
    control: RuntimeControl,
    sessions: SessionStore,
    gateway_factory: GatewayFactory,
    tools: ToolWiring,
    captions: CallCaptions,
    file_keeper: FileKeeper | None = None,
    profiles: ProfileBuilder,
    interactions: InteractionWiring,
    sinks_by_agent: dict[str, AgentSink],
    tracer: ToolTrace | None,
    http_turns: HttpTurns | None,
    turns: TurnRegistry,
    leases: Leases,
    instance: str,
    replica: Replica | None = None,
    replicas: Replicas | None = None,
    forwarder: HubForwarder | None = None,
) -> list[AgentUnit]:
    units: list[AgentUnit] = []
    for spec in specs:
        handles: dict[str, GatewayHandle] = {}
        for config in configs.get(spec.name) or []:
            handle = gateway_factory.create(spec.name, config)
            if handle is None:
                continue  # unknown gateway kind (factory logged why)
            handles[config.kind] = handle
        if not handles:
            continue  # no token for any of its gateways (resolver logged why)
        # Every gateway's formatting rules, once each — the agent writes for
        # all of them at once.
        hints = [h.prompt_hint for h in handles.values() if h.prompt_hint]
        profiles.set_hint(spec.name, "\n\n".join(dict.fromkeys(hints)))
        # must precede profiles.build (reads caps/env); each gateway's caps are
        # kept by its name, the agent's allowlist is what any of them can serve.
        for kind, handle in handles.items():
            tools.enroll(
                spec, handle.admin, extra_caps=handle.caps, denied_caps=handle.denied_caps, gateway=kind,
            )
        if spec.name in engine_names:
            # Engine-owned agents (support) get the agents directory path (their
            # editable workspace) and the engine source root (read-only) so their
            # builder tools + SYSTEM.md can locate both.
            tools.add_env(
                spec.name,
                {"AGENTS_PATH": settings.agents_path, "IMPI_ROOT": str(IMPI_ROOT)},
            )
        unit = _build_unit(
            spec, handles,
            runtime=runtime, control=control, sessions=sessions, profiles=profiles,
            interactions=interactions, sinks_by_agent=sinks_by_agent,
            inline_image_max_bytes=int(settings.inline_image_max_mb * 1024 * 1024),
            # An http caller's tool activity goes to the caller's journal, a
            # chat turn's to a widget under the reply — chosen per turn by the
            # chat the message came with.
            tracer=(
                http_turns if set(handles) == {GATEWAY_HTTP}
                else _TracerByChat(http_turns, tracer) if GATEWAY_HTTP in handles
                else tracer
            ),
            turns=turns,
            captions=captions,
            posting_tools=_posting_tools(tools),
            file_keeper=file_keeper,
            replica=replica, replicas=replicas, forwarder=forwarder,
        )
        for kind in handles:
            if kind in CONNECTION_GATEWAYS:
                # A connection the platform pushes events into is opened by one
                # engine process at a time: two replicas with one bot token would
                # each get half of every conversation. The store's lease says
                # which; with one process and a sqlite store it is always ours.
                unit.gateways[kind] = LeasedGateway(
                    unit.gateways[kind], leases, name=f"gateway:{kind}:{spec.name}", owner=instance
                )
        units.append(unit)
    return units


def build_app(settings: ImpiSettings) -> App:
    # The shared skill library: profiles resolve `registry:<name>` through it.
    library = SkillLibrary(settings.resolved_skills_path)
    user_store, engine_store = open_profile_stores(settings, library)
    engine_names = {spec.name for spec in engine_store.list()}
    profiles = CompositeProfileStore([user_store, engine_store])  # rejects duplicate names
    sessions = open_store(
        settings.store_backend, name=settings.resolved_db_name, url=settings.db_url,
        prefix=settings.db_collection_prefix,
    )

    # Resolve each agent's gateway config once, up front: it tells us which agents
    # are runnable and whether any needs the HTTP receiver — so the interaction
    # plumbing can be built complete (no post-loop finalize).
    specs = select_specs(settings, user_store, engine_store)
    configs = {spec.name: resolve_gateways(settings, spec.name) for spec in specs}
    # The default gateway's config per agent, for what asks about one gateway.
    defaults = {name: (cfgs[0] if cfgs else None) for name, cfgs in configs.items()}
    # A receiver with no secret trusts every callback on arrival; a deployment
    # that needs one gets a secret minted on its first start.
    receiver_needed = needs_receiver(cfg for cfgs in configs.values() for cfg in cfgs)
    settings = ensure_receiver_secret(settings, needs_receiver=receiver_needed)

    # The app owns the registry of live agent presences (agent -> AgentSink); the
    # loop fills it, the interaction collaborators read it lazily via MappingPresence.
    sinks_by_agent: dict[str, AgentSink] = {}
    presence = MappingPresence(sinks_by_agent)
    agent_user_ids: dict[str, str] = {}
    # A person's own accounts at OAuth providers. The chat client an email is
    # checked through is read when a connect finishes, by then the units are up;
    # the message a connect started from is redrawn through the dispatcher,
    # handed over below once interactivity is built.
    redraw_on_connect = RedrawOnConnect()
    connections = build_connections(
        settings, sessions, defaults,
        lambda agent: sink.chat if (sink := sinks_by_agent.get(agent)) is not None else None,
        on_connected=redraw_on_connect,
    )
    if connections is not None:
        report = connections_report(
            settings, connections.providers, defaults,
            tools_on=settings.tools.enabled, widgets_on=settings.integrations.enabled,
        )
        logger.info(report.line)
        for warning in report.warnings:
            logger.warning(warning)
    # Interaction plumbing: its ui_bridge feeds the runtime and its dispatcher feeds
    # the gateway factory. Holds no per-agent state.
    # Commands the engine answers itself. Each screen declares the trigger word
    # it binds to (SkillScreen.command == "skills", i.e. /skills), and a matching
    # command never reaches an agent: browsing the library and editing a profile
    # are facts and edits. SIGHUP after an edit is how the agent picks it up (the
    # same path as `impi reload`).
    screens = ScreenRegistry()
    screens.register(
        SkillScreen(
            library, settings.agents_path,
            command=settings.skills_command, reload=_signal_reload,
        )
    )
    # Registered even when scheduling is off: the screen then says so itself.
    # Leaving it unregistered would hand /tasks to an agent, which answers about
    # a list it has no way to read.
    screens.register(
        TaskScreen(
            sessions,
            TaskAdmin(
                sessions, sessions,
                default_timezone=settings.scheduler.timezone,
                max_per_agent=settings.scheduler.max_tasks_per_agent,
            ),
            heartbeat=sessions,
            command=settings.tasks_command,
            scheduler_enabled=settings.scheduler.enabled,
        )
    )
    # The panel under a reply that lists the turn's tool calls. Registered
    # whenever interactivity is on, whether or not new ones are being drawn:
    # a button posted before the widget was turned off must still open.
    screens.register(TraceScreen(sessions))
    if connections is not None:
        screens.register(ConnectionsScreen(
            connections.service, owner_for=connections.owner_for,
            providers_for=connections.providers_for, command=settings.connections_command,
        ))
    # Which agents this engine actually runs — a profile with no token is not one
    # of them. It answers /command/default, so a deployment can register a slash
    # command without naming an agent in its URL.
    runnable = tuple(name for name, cfgs in configs.items() if cfgs)
    # Requests waiting on a human — a credential, a tool call. Built here
    # because the click that answers one arrives through the interaction
    # receiver, while the things that raise one are assembled further down.
    approvals = PendingApprovals() if settings.integrations.enabled else None
    # This process among the replicas sharing the store, when it is one of
    # several: a random id (in front of every click token it mints), its
    # address for the others, and the client that reaches theirs.
    replica: Replica | None = None
    forwarder: HubForwarder | None = None
    if settings.replicated:
        replica = Replica(id=secrets.token_hex(4), url=settings.replica_url)
        forwarder = HubForwarder(settings.replica_token.strip())
    elif settings.replica_url or settings.replica_token.strip():
        logger.warning(
            "REPLICA_URL and REPLICA_TOKEN must both be set; conversations are not owned or forwarded"
        )
    interactions = InteractionWiring(
        settings.integrations, sessions, presence,
        codec=MattermostCallbackCodec(), needs_receiver=receiver_needed,
        command_tokens=settings.command_tokens_for,
        agents=lambda: runnable,
        default_agent=settings.agent_name,
        agent_user_id=lambda agent: agent_user_ids.get(agent, ""),
        screens=screens,
        approvals=approvals,
        tool_max_grant_s=settings.tools.max_grant_s,
        tool_approvers=settings.tools.approvers,
        replica=replica, clicks=forwarder,
    )
    redraw_on_connect.dispatcher = interactions.dispatcher

    # TODO(runtime-backend): build_app hardcodes the pi backend. When a second
    # AgentRuntime appears, extract a runtime-builder (selected by a settings key)
    # so this function depends only on the AgentRuntime port. Deferred until needed.
    # One secret per conversation's process, issued here and checked by the
    # tool server: a call's session id is then a fact, not a claim the shared
    # agent token lets any process of that agent make.
    session_proofs = SessionProofBook()
    # Where files people attach land. Swept once at startup so a long-running
    # deployment doesn't accumulate them forever.
    attachments: AttachmentStore | None = None
    if settings.attachments_enabled:
        attachments = AttachmentStore(
            settings.resolved_attachments_dir,
            max_bytes=int(settings.attachment_max_mb * 1024 * 1024),
            retention_days=settings.attachment_retention_days,
        )
        attachments.sweep()
    # What each agent's process may touch: its profile and the skill library to
    # read, its files and its own scratch space to write. support is part of
    # the engine (it reads the engine's own configuration) and runs
    # unconfined unless its profile says otherwise.
    # The same lookup as the tools' (crucible.conversation_files), so the
    # confinement and a tool name one directory for a conversation.
    conversation_files_dir = conversation_files_dir_lookup(attachments, sessions)
    process_scopes = AgentProcessScopes(
        lambda name: _spec_or_none(profiles, name),
        default_level=settings.runtime_confinement,
        files_dir_for=attachments.dir_for if attachments else (lambda name: None),
        conversation_files_dir_for=conversation_files_dir,
        files_dir_env=FILES_DIR_ENV,
        skills_root=settings.resolved_skills_path,
        unconfined_by_default=frozenset(engine_names),
    )
    hosts = build_agent_hosts(settings, specs, attachments.dir_for if attachments else None)
    abi = landlock_abi()
    line, shared_warning = confinement_report(
        specs, defaults, engine_names, level_for=process_scopes.level_for, abi=abi,
        in_process=lambda name: name not in hosts.remote_agents,
    )
    (logger.info if abi else logger.warning)(line)
    if shared_warning:
        logger.warning(shared_warning)
    # An agent in its own container is handed the credential file alone.
    here, own_container = configured_providers(), configured_providers(definitions=False)
    fallback_line, fallback_warning = fallback_report(
        specs, defaults,
        providers_for=lambda name: own_container if name in hosts.remote_agents else here,
    )
    if fallback_line:
        logger.info(fallback_line)
    if fallback_warning:
        logger.warning(fallback_warning)
    runtime = PiRuntime(
        pi_bin=settings.pi_bin,
        session_dir=str(settings.resolved_pi_session_dir),
        max_concurrent_sessions=settings.pi_max_concurrent_sessions,
        max_sessions_per_agent=settings.pi_max_sessions_per_agent,
        idle_ttl=settings.pi_session_idle_ttl,
        acquire_timeout=settings.pi_acquire_timeout_s,
        evict_idle_on_pressure=settings.pi_evict_idle_on_pressure,
        session_proofs=session_proofs,
        extra_env=build_pi_env(settings),
        inherit=settings.runtime_env_names(),
        extra_extensions=build_pi_extensions(settings),
        ui_bridge=interactions.ui_bridge,
        hosts=hosts,
        scopes=process_scopes,
    )
    registry = RegistryService(sessions)
    # One shared guard makes the rate limit a GLOBAL per-conversation bound (a
    # cascade spans agents, i.e. gateways).
    loop_guard = LoopGuard(
        max_hops=settings.agent_max_hops,
        max_agent_turns=settings.agent_rate_limit_turns,
        window_s=settings.agent_rate_window_s,
    )
    # The MCP servers the deployment declares: their pinned tools stand in the
    # registry beside the engine's own, and are called through them. The
    # servers themselves are connected when the app starts (``run``).
    mcp = McpServers(McpLibrary(settings.resolved_mcp_path), settings.mcp_values())
    tools = ToolWiring(
        settings.tools, registry=engine_tool_registry(mcp.registry_tools()), data_dir=settings.data_dir,
        interactivity_on=settings.integrations.enabled,
        files_on=settings.attachments_enabled,
        scheduler_on=settings.scheduler.enabled,
        # A tool that must be confirmed is advertised only where the gate that
        # asks exists; with interactivity off it is left out of the agent's
        # tool list rather than offered and refused on every call.
        confirmations_on=interactions.tool_gate is not None,
        connections_on=connections is not None,
    )
    profile_builder = ProfileBuilder(tools, engine_owned=frozenset(engine_names))
    if attachments is not None:
        for spec in specs:
            # Each agent's own directory, named in its environment. It is the one
            # place an agent can both write and send from wherever its runtime
            # runs — the same path on both sides when that is another container —
            # so a skill that produces a file has somewhere to put it.
            files_dir = attachments.dir_for(spec.name)
            files_dir.mkdir(parents=True, exist_ok=True)
            tools.add_env(spec.name, {FILES_DIR_ENV: str(files_dir)})
    # The ws hub exists only when some agent lives on the "ws" gateway; client
    # services authenticate against it with their own tokens (WS_SERVICE_TOKEN__*).
    ws_hub: WsHub | None = None
    if any(cfg.kind == GATEWAY_WS for cfgs in configs.values() for cfg in cfgs):
        ws_services = settings.ws_services()
        if not ws_services:
            logger.warning(
                "ws agents configured but no client services "
                "(set WS_SERVICE_TOKEN__<NAME>) — nothing can connect to the hub"
            )
        ws_hub = WsHub(
            settings.ws_host, settings.ws_port, ws_services,
            directory=registry, attachments=attachments,
        )
    # The http hub likewise: a request/turn API for programs that cannot hold a
    # socket; callers authenticate with their own tokens (HTTP_CALLER_TOKEN__*).
    # It also carries the replicas' own routes, so it runs whenever this is one
    # of several even with no http agent.
    http_hub: HttpHub | None = None
    http_agents = any(cfg.kind == GATEWAY_HTTP for cfgs in configs.values() for cfg in cfgs)
    if http_agents or replica is not None:
        http_callers = settings.http_callers()
        if http_agents and not http_callers:
            logger.warning(
                "http agents configured but no callers "
                "(set HTTP_CALLER_TOKEN__<NAME>) — nothing can reach the hub"
            )
        internal = (
            InternalRoutes(
                settings.replica_token.strip(), presence,
                dispatcher=interactions.dispatcher, attachments=attachments,
            )
            if replica is not None
            else None
        )
        http_hub = HttpHub(
            settings.http_host, settings.http_port, TokenCallers(http_callers),
            # Cards a conversation holds outside a turn of this gateway's go in
            # the store: every replica shows them, not only the one whose turn
            # raised them.
            turns=HttpTurns(cards=sessions),
            directory=registry, attachments=attachments,
            dispatcher=interactions.dispatcher, control=runtime,
            max_wait_s=settings.http_max_wait_s,
            # `startedSince`: which of a person's conversations the engine
            # started a turn in — a scheduled task, an event — from the inventory.
            engine_turns=engine_turns_lookup(sessions),
            routes=internal.routes if internal is not None else (),
        )
    # What belongs to one turn (a credential a caller sent along), bound by the
    # flow for the turn and read by the tool server. One registry for the app.
    turns = TurnRegistry()
    gateway_factory = GatewayFactory(
        directory=registry, loop_guard=loop_guard, dispatcher=interactions.dispatcher,
        ws_hub=ws_hub, http_hub=http_hub, attachments=attachments,
    )

    # Only with a dispatcher to route the click to: with interactivity off the
    # button would be dead, and a dead control is worse than no widget.
    tracer = (
        ToolTrace(
            sessions,
            callback_url=settings.integrations.interact_url,
            retention_days=settings.tool_trace_retention_days,
        )
        if settings.integrations.enabled and settings.tool_trace_enabled
        else None
    )
    units = _build_units(
        specs, configs, engine_names,
        settings=settings, runtime=runtime, control=runtime, sessions=sessions,
        gateway_factory=gateway_factory, tools=tools, file_keeper=attachments,
        leases=sessions, instance=f"{socket.gethostname()}:{os.getpid()}",
        # Who captions a call for the person watching: the deployment's tools
        # themselves, then the runtime's own built-ins.
        captions=ChainedCaptions(*([tools.registry] if tools.registry else []), PiBuiltinCaptions()),
        profiles=profile_builder, interactions=interactions, sinks_by_agent=sinks_by_agent,
        tracer=tracer, http_turns=http_hub.turns if http_hub else None, turns=turns,
        replica=replica, replicas=sessions, forwarder=forwarder,
    )
    if not units:
        raise RuntimeError("No agents with a gateway token — nothing to run")

    # Which directories each agent may send files from: its own profile (the
    # runtime's working directory), its own attachments, and the system temp dir.
    file_svc = None
    if attachments is not None:
        file_svc = ChatFileService(
            presence,
            sessions,
            {
                spec.name: default_roots(
                    spec.profile_dir,
                    attachments.dir_for(spec.name),
                    include_temp=not settings.agent_hosts_enabled,
                )
                for spec in specs
            },
            max_bytes=int(settings.attachment_max_mb * 1024 * 1024),
        )
    # Creating tasks needs only the store, so it is ready before the tool
    # server; the ticker that fires them needs the units and comes after.
    task_admin: TaskAdmin | None = None
    if settings.scheduler.enabled:
        task_admin = TaskAdmin(
            sessions, sessions,
            default_timezone=settings.scheduler.timezone,
            max_per_agent=settings.scheduler.max_tasks_per_agent,
        )
    if settings.agent_hosts_enabled and settings.tools.enabled:
        if settings.tool_server_host in ("127.0.0.1", "localhost", "::1"):
            logger.warning(
                "agents run in containers of their own but the tool server binds "
                "%s — they cannot reach it; set TOOL_SERVER_HOST=0.0.0.0 and "
                "TOOL_PUBLIC_URL",
                settings.tool_server_host,
            )
        elif not settings.tool_public_url:
            logger.warning(
                "TOOL_PUBLIC_URL is not set — agents in their own containers will "
                "be told to call %s, which is this container's address, not one "
                "they can resolve",
                settings.tools.server_url,
            )
    tool_server = tools.build_server(
        directory=registry,
        interaction_svc=interactions.interaction_svc,
        file_svc=file_svc,
        task_svc=task_admin,
        dotenv_path=settings.dotenv_path,
        # Tools get the turn's channel and last speaker (an ephemeral reply,
        # say) and its files directory, in one read of the session record.
        conversation_resolver=conversation_lookup(attachments, sessions),
        received_files_for=attachments.received_files_for if attachments is not None else None,
        connections=connections.tool_access() if connections is not None else None,
        tool_gate=interactions.tool_gate,
        # While the gate waits for a person, the turn's timeout waits too.
        clock=runtime,
        session_proofs=session_proofs,
        turns=turns,
    )
    # Scheduled work. Built after the units, because its dispatcher reads the
    # live {agent: AgentSink} map and its prompt runner the units' profiles.
    scheduler: Scheduler | None = None
    if settings.scheduler.enabled:
        units_by_name = {unit.spec.name: unit for unit in units}

        def _profile_of(agent: str) -> AgentProfile | None:
            unit = units_by_name.get(agent)
            # The flow's own profile, so a hot-reload applies to the next
            # memoryless run too.
            return unit.flow.profile if unit else None

        scheduler = Scheduler(
            sessions,
            dispatcher=SinkTurnDispatcher(sinks_by_agent, http=http_hub, sessions=sessions),
            prompts=RuntimePromptRunner(runtime, _profile_of),
            notifier=PresenceNotifier(presence),
            tick_s=settings.scheduler.tick_s,
            startup_grace_s=settings.scheduler.startup_grace_s,
            run_deadline_s=settings.scheduler.run_deadline_s,
            max_concurrent=settings.scheduler.max_concurrent,
            max_failures=settings.scheduler.max_failures,
            version=_engine_version(),
        )

    reloader = ProfileReloader(
        profiles=profiles,
        runtime=runtime,
        registry=registry,
        units=units,
        build_profile=profile_builder.build,
    )

    logger.info(
        "app built: agents=[%s], mm=%s, data=%s, tools=%s, widgets=%s, ws=%s, http=%s, "
        "scheduler=%s",
        ", ".join(u.spec.name for u in units),
        settings.mattermost_url,
        settings.data_dir,
        "on" if tool_server else "off",
        "on" if interactions.receiver else "off",
        f"on:{settings.ws_port}" if ws_hub else "off",
        f"on:{settings.http_port}" if http_hub else "off",
        f"on:{settings.scheduler.tick_s:.0f}s" if scheduler else "off",
    )
    if replica is not None:
        logger.info("replica %s at %s: conversations are owned and forwarded", replica.id, replica.url)
    return App(
        settings=settings,
        runtime=runtime,
        sessions=sessions,
        registry=registry,
        units=units,
        tool_server=tool_server,
        integrations=interactions.receiver,
        reloader=reloader,
        screens=screens,
        ws_hub=ws_hub,
        http_hub=http_hub,
        scheduler=scheduler,
        tracer=tracer,
        auth_refresher=AuthRefresher(program=settings.pi_bin),
        connections=connections,
        agent_user_ids=agent_user_ids,
        tools=tools,
        sinks_by_agent=sinks_by_agent,
        heartbeat=ReplicaHeartbeat(sessions, replica) if replica is not None else None,
        forwarder=forwarder,
        mcp_servers=mcp if mcp.servers else None,
    )


def _apply_mcp_check(tools: ToolWiring, report: CheckReport) -> None:
    """What a check of the MCP servers found, applied to what the agents are
    shown: withdrawn tools go from every allowlist and manifest, tools a review
    handed back return, moved definitions reach the manifests."""
    tools.withdraw(report.withdrawn_tools)
    tools.restore(report.restored_tools)
    if report.refreshed:
        tools.refresh_manifests()


async def _reload(app: App, identities: dict[str, AgentIdentity]) -> None:
    """``impi reload``: the MCP servers are checked against their snapshots as
    the operator left them on disk — a refreshed snapshot is adopted, a server
    reviewed back in is handed back — and then every profile is re-read."""
    if app.mcp_servers is not None and app.tools is not None:
        _apply_mcp_check(app.tools, await app.mcp_servers.recheck(reviewed=True))
    await app.reloader.reload(identities)


async def _supervise(
    name: str, run_loop: Callable[[], Awaitable[None]], *, sleep=asyncio.sleep
) -> None:
    """Keep one long-running loop alive without letting its failures reach the
    others — an agent's WS connection, or the scheduler's tick. A clean return
    (a disconnect) ends it; any crash is logged and retried with capped
    exponential backoff + jitter, so one flaky loop never takes the whole engine
    down. ``sleep`` is injectable for tests."""
    backoff = 1.0
    while True:
        try:
            await run_loop()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = backoff + random.uniform(0, backoff)
            logger.exception("%s crashed; restarting in %.0fs", name, delay)
            await sleep(delay)
            backoff = min(backoff * 2, 60.0)


def stop_on_sigterm(task: asyncio.Task | None = None) -> None:
    """Make SIGTERM cancel ``task`` (the current one by default) and nothing
    else. Raising KeyboardInterrupt from a signal handler — the old way — made
    ``asyncio.run`` cancel every task at once on the way out, so a shutdown that
    meant to wait for running turns found none left to wait for."""
    target = task or asyncio.current_task()
    if target is None:
        raise RuntimeError("stop_on_sigterm needs a running task")
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, target.cancel)


async def run(settings: ImpiSettings) -> None:
    app = build_app(settings)
    if app.tracer is not None:
        # Like the attachment sweep: old traces go at boot, not on a timer.
        await app.tracer.sweep()
    if settings.processed_posts_retention_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings.processed_posts_retention_days)
        forgotten = await app.sessions.prune_processed(before=cutoff.isoformat(timespec="seconds"))
        if forgotten:
            logger.info("forgot %d answered message(s) older than %d days", forgotten,
                        settings.processed_posts_retention_days)
    await prepare_runtime_storage(settings, app.sessions)
    app.runtime.start()
    if app.mcp_servers is not None and app.tools is not None:
        # Before the tool server and the gateways: a tool whose definition is
        # no longer the one reviewed is withdrawn before any process reads a
        # manifest or a model is shown it; a trusted server's moved definitions
        # reach the manifests the same way.
        report = await app.mcp_servers.start()
        _apply_mcp_check(app.tools, report)
        logger.info(
            "mcp: %s",
            ", ".join(
                f"{s.name}({len([t for t in app.mcp_servers.tools() if t.server.name == s.name])} tools, {s.trust}"
                f"{', unreachable' if s.name in report.unreachable else ''}"
                f"{', withdrawn' if s.name in report.withdrawn else ''})"
                for s in app.mcp_servers.servers
            ),
        )
    if app.tool_server is not None:
        # Up before the gateways: a pi turn may call a tool the moment it starts.
        await app.tool_server.start()
    if app.integrations is not None:
        await app.integrations.start()
    if app.ws_hub is not None:
        await app.ws_hub.start()
    if app.http_hub is not None:
        await app.http_hub.start()
    if app.connections is not None:
        await app.connections.callback.start()
    if app.heartbeat is not None:
        # Alive before anything reaches us: a forwarded turn finds our lease.
        await app.heartbeat.beat()
    try:
        identities = {}
        for unit in app.units:
            # Each gateway is the authority on who we are on its platform; the
            # flow needs every one to keep our own posts out of replayed
            # history. The default gateway's identity is the agent's for the
            # directory.
            for kind, gateway in unit.gateways.items():
                identity = await gateway.login()
                unit.flow.add_identity(identity.user_id)
                if unit.spec.name not in identities:
                    identities[unit.spec.name] = identity
                    app.agent_user_ids[unit.spec.name] = identity.user_id
        await app.registry.sync([unit.spec for unit in app.units], identities)
        # SIGHUP → hot-reload agent profiles (`make reload`). Deliberate and
        # human-triggered: the operator reviews/updates the agents directory first,
        # then signals. Installed after login so `identities` is populated.
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(_reload(app, identities)),
        )
        # SIGTERM (`make stop`, a container runtime, Kubernetes) ends THIS task
        # only, so the finally below runs while every other task — the turns in
        # flight — is still alive, which is what lets the http hub drain. The
        # same shutdown Ctrl+C gets, since asyncio.run cancels the main task on
        # SIGINT too.
        stop_on_sigterm()
        # N supervised WS loops in one process, plus the scheduler's tick — each
        # isolated from the others. The scheduler goes here rather than into a
        # detached task on purpose: a background task whose exception nobody
        # observes is exactly how a timer dies unnoticed.
        loops = [
            _supervise(f"gateway {unit.spec.name}/{kind}", gateway.run)
            for unit in app.units for kind, gateway in unit.gateways.items()
        ]
        if app.scheduler is not None:
            loops.append(_supervise("scheduler", app.scheduler.run))
        if app.auth_refresher is not None:
            loops.append(_supervise("runtime login refresh", app.auth_refresher.run))
        if app.heartbeat is not None:
            loops.append(_supervise("replica heartbeat", app.heartbeat.run))
        if app.mcp_servers is not None and app.tools is not None:
            mcp, tools = app.mcp_servers, app.tools
            loops.append(_supervise(
                "mcp recheck", lambda: mcp.run(settings.mcp_recheck_s, lambda report: _apply_mcp_check(tools, report))
            ))
        await asyncio.gather(*loops)
    finally:
        if app.heartbeat is not None:
            # Its loop was cancelled with us and let the lease go: the others
            # take our conversations from here on. Stopped so it never beats again.
            app.heartbeat.stop()
        if app.forwarder is not None:
            await app.forwarder.aclose()
        if app.scheduler is not None:
            # Before the gateways: a run still in flight needs somewhere to post
            # its outcome, and wants its row closed rather than reclaimed later.
            await app.scheduler.stop()
        if app.ws_hub is not None:
            await app.ws_hub.stop()
        if app.http_hub is not None:
            # Stop taking new turns, let the running ones finish, then close.
            app.http_hub.drain()
            if not await app.http_hub.idle(settings.http_drain_s):
                logger.warning("http hub: turns still running after %.0fs; closing anyway",
                               settings.http_drain_s)
            await app.http_hub.stop()
        if app.connections is not None:
            await app.connections.callback.stop()
        if app.integrations is not None:
            await app.integrations.stop()
        if app.tool_server is not None:
            await app.tool_server.stop()
        if app.mcp_servers is not None:
            await app.mcp_servers.stop()
        for unit in app.units:
            for gateway in unit.gateways.values():
                await gateway.stop()
        await app.runtime.close()
        await app.sessions.close()
