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
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

# Importing these runs their @tool decorators so build_registry() sees them:
# crucible's generic ask/form tools plus impi's chat-management and
# agent-provisioning tools.
import crucible.builtin_tools  # noqa: E402,F401
import impi.agent_tools  # noqa: E402,F401
import impi.chat_tools  # noqa: E402,F401
import impi.skill_tools  # noqa: E402,F401
import impi.task_tools  # noqa: E402,F401
from crucible.attachments import AttachmentStore
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways import (
    GatewayConfig,
    GatewayFactory,
    GatewayHandle,
    needs_http_receiver,
)
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.gateways.ws import WsHub
from crucible.interactions import (
    AgentSink,
    InteractionsServer,
    InteractionWiring,
    MappingPresence,
)
from crucible.interactions.files import ChatFileService, default_roots
from crucible.interactions.screens import ScreenRegistry
from crucible.loopguard import LoopGuard
from crucible.ports.agent import AgentProfile, AgentRuntime, AgentSpec
from crucible.profiles import CompositeProfileStore, FsProfileStore, ProfileStore
from crucible.reloader import ProfileReloader
from crucible.runtimes.pi import EXTENSION_PATH, build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.scheduler.admin import TaskAdmin
from crucible.scheduler.service import Scheduler
from crucible.skills import SkillLibrary
from crucible.store.base import SessionStore
from crucible.store.sessions import SqliteSessionStore
from crucible.tools import ToolServer, ToolWiring
from crucible.unit import AgentUnit
from impi.config import ImpiSettings
from impi.gateways import resolve_gateway
from impi.registry import RegistryService
from impi.scheduling import PresenceNotifier, RuntimePromptRunner, SinkTurnDispatcher
from impi.skill_screen import SkillScreen
from impi.task_screen import TaskScreen

logger = logging.getLogger(__name__)


def build_pi_env(settings: ImpiSettings) -> dict[str, str]:
    """Env forwarded into every pi subprocess.

    Empty when the ChatGPT subscription is used — pi then authenticates via its
    own OAuth store; LLM_* only feed the optional custom provider extension.
    """
    env: dict[str, str] = {}
    if settings.llm_base_url:
        env["LLM_BASE_URL"] = settings.llm_base_url
    if settings.llm_api_key:
        env["LLM_API_KEY"] = settings.llm_api_key
    if settings.llm_model:
        env["LLM_MODEL"] = settings.llm_model
    if not settings.llm_verify_ssl:
        env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
    return env


def build_pi_extensions(settings: ImpiSettings) -> list[str]:
    """pi extensions loaded via -e on every spawn.

    The engine's own tool bridge ships with the pi driver and is always loaded
    first; any extra extensions in the agents directory
    (``_extensions/*/index.ts``) are appended, so the agents directory can add its
    own without touching the engine.
    """
    paths = [str(EXTENSION_PATH.resolve())]
    if settings.agents_path:
        ext_root = Path(settings.agents_path) / "_extensions"
        paths += sorted(
            str(index.resolve()) for index in ext_root.glob("*/index.ts") if index.is_file()
        )
    return paths


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
    scheduler: Scheduler | None = None


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


# Engine-owned agent profiles (e.g. `support`) ship WITH impi, under the package.
BUILTIN_AGENTS_PATH = Path(__file__).parent / "builtin_agents"
# The engine's own checkout root (packages/impi/src/impi/app.py -> repo root):
# forwarded to engine agents so support can read the engine source/docs to diagnose.
IMPI_ROOT = Path(__file__).resolve().parents[4]


def _select_specs(settings: ImpiSettings, profiles: ProfileStore) -> list[AgentSpec]:
    names = settings.enabled_agent_names()
    if names is None:
        return profiles.list()
    return [profiles.get(name) for name in names]


class ProfileBuilder:
    """Maps a spec onto its runtime profile: the backend mapping plus the agent's
    gateway formatting hint and, when tools are on, its tool env + a freshly written
    manifest. The sole place profiles are built — used at boot and, as the reloader's
    ``build_profile``, on every hot-reload (re-reading live hints/caps/env).
    """

    def __init__(self, tools: ToolWiring) -> None:
        self._tools = tools
        self.hints: dict[str, str] = {}  # agent -> its gateway's formatting rules

    def set_hint(self, name: str, hint: str) -> None:
        self.hints[name] = hint

    def build(self, spec: AgentSpec) -> AgentProfile:
        base = build_pi_profile(spec)
        hint = self.hints.get(spec.name, "")
        if hint:
            base = dataclasses.replace(base, append_system_prompt=hint)
        env = self._tools.profile_env(spec)
        if env is None:
            return base
        return dataclasses.replace(base, env=env)


def _build_unit(
    spec: AgentSpec,
    handle: GatewayHandle,
    *,
    runtime: AgentRuntime,
    sessions: SessionStore,
    profiles: ProfileBuilder,
    interactions: InteractionWiring,
    sinks_by_agent: dict[str, AgentSink],
    inline_image_max_bytes: int,
) -> AgentUnit:
    profile = profiles.build(spec)
    flow = AgentFlow(
        runtime, profile, sessions,
        agent_name=spec.name,
        inline_image_max_bytes=inline_image_max_bytes,
    )
    coalescer = MessageCoalescer(flow, on_arrival=interactions.on_arrival_for(spec.name))
    # Record the agent's live presence in the app-owned registry; interactions read
    # it lazily through MappingPresence.
    sinks_by_agent[spec.name] = AgentSink(sink=coalescer, chat=handle.chat)
    return AgentUnit(spec=spec, flow=flow, gateway=handle.create_gateway(coalescer))


def _build_units(
    specs: list[AgentSpec],
    configs: dict[str, GatewayConfig | None],
    engine_names: set[str],
    *,
    settings: ImpiSettings,
    runtime: AgentRuntime,
    sessions: SessionStore,
    gateway_factory: GatewayFactory,
    tools: ToolWiring,
    profiles: ProfileBuilder,
    interactions: InteractionWiring,
    sinks_by_agent: dict[str, AgentSink],
) -> list[AgentUnit]:
    units: list[AgentUnit] = []
    for spec in specs:
        config = configs.get(spec.name)
        if config is None:
            continue  # no token for this agent (resolver logged why)
        handle = gateway_factory.create(spec.name, config)
        if handle is None:
            continue  # unknown gateway kind (factory logged why)
        profiles.set_hint(spec.name, handle.prompt_hint)
        # must precede profiles.build (reads caps/env); handle.caps carries
        # gateway-kind capabilities like ephemeral.
        tools.enroll(spec, handle.admin, extra_caps=handle.caps)
        if spec.name in engine_names:
            # Engine-owned agents (support) get the agents directory path (their
            # editable workspace) and the engine source root (read-only) so their
            # builder tools + SYSTEM.md can locate both.
            tools.add_env(
                spec.name,
                {"AGENTS_PATH": settings.agents_path, "IMPI_ROOT": str(IMPI_ROOT)},
            )
        units.append(
            _build_unit(
                spec, handle,
                runtime=runtime, sessions=sessions, profiles=profiles,
                interactions=interactions, sinks_by_agent=sinks_by_agent,
                inline_image_max_bytes=int(settings.inline_image_max_mb * 1024 * 1024),
            )
        )
    return units


def build_app(settings: ImpiSettings) -> App:
    # The shared skill library: profiles resolve `registry:<name>` through it.
    library = SkillLibrary(settings.resolved_skills_path)
    user_store = FsProfileStore(
        settings.agents_path,
        default_timeout=settings.pi_timeout,
        default_provider=settings.default_provider,
        default_model=settings.default_model,
        skills_override=settings.skills_for,
        library=library.path_if_present,
    )
    # Engine-owned agents (support) are always enumerated (not subject to the user
    # AGENTS_ENABLED list); the token gate still skips them without a token. Their
    # provider/model override the global default so a public checkout still runs.
    engine_store = FsProfileStore(
        str(BUILTIN_AGENTS_PATH),
        default_timeout=settings.pi_timeout,
        default_provider=settings.support_provider or settings.default_provider,
        default_model=settings.support_model or settings.default_model,
        skills_override=settings.skills_for,
        library=library.path_if_present,
    )
    engine_names = {spec.name for spec in engine_store.list()}
    profiles = CompositeProfileStore([user_store, engine_store])  # rejects duplicate names
    sessions = SqliteSessionStore(settings.resolved_db_path)

    # Resolve each agent's gateway config once, up front: it tells us which agents
    # are runnable and whether any needs the HTTP receiver — so the interaction
    # plumbing can be built complete (no post-loop finalize).
    specs = [*_select_specs(settings, user_store), *engine_store.list()]
    configs = {spec.name: resolve_gateway(settings, spec.name) for spec in specs}
    needs_receiver = any(
        cfg is not None and needs_http_receiver(cfg.kind) for cfg in configs.values()
    )

    # The app owns the registry of live agent presences (agent -> AgentSink); the
    # loop fills it, the interaction collaborators read it lazily via MappingPresence.
    sinks_by_agent: dict[str, AgentSink] = {}
    presence = MappingPresence(sinks_by_agent)
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
    interactions = InteractionWiring(
        settings.integrations, sessions, presence,
        codec=MattermostCallbackCodec(), needs_receiver=needs_receiver,
        command_tokens=settings.command_tokens_for,
        screens=screens,
    )

    # TODO(runtime-backend): build_app hardcodes the pi backend. When a second
    # AgentRuntime appears, extract a runtime-builder (selected by a settings key)
    # so this function depends only on the AgentRuntime port. Deferred until needed.
    runtime = PiRuntime(
        pi_bin=settings.pi_bin,
        session_dir=str(settings.resolved_pi_session_dir),
        max_concurrent_sessions=settings.pi_max_concurrent_sessions,
        idle_ttl=settings.pi_session_idle_ttl,
        extra_env=build_pi_env(settings),
        extra_extensions=build_pi_extensions(settings),
        ui_bridge=interactions.ui_bridge,
    )
    registry = RegistryService(sessions)
    # One shared guard makes the rate limit a GLOBAL per-conversation bound (a
    # cascade spans agents, i.e. gateways).
    loop_guard = LoopGuard(
        max_hops=settings.agent_max_hops,
        max_agent_turns=settings.agent_rate_limit_turns,
        window_s=settings.agent_rate_window_s,
    )
    tools = ToolWiring(
        settings.tools, data_dir=settings.data_dir,
        interactivity_on=settings.integrations.enabled,
        files_on=settings.attachments_enabled,
        scheduler_on=settings.scheduler.enabled,
    )
    profile_builder = ProfileBuilder(tools)
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
    # The ws hub exists only when some agent lives on the "ws" gateway; client
    # services authenticate against it with their own tokens (WS_SERVICE_TOKEN__*).
    ws_hub: WsHub | None = None
    if any(cfg is not None and cfg.kind == "ws" for cfg in configs.values()):
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
    gateway_factory = GatewayFactory(
        directory=registry, loop_guard=loop_guard, dispatcher=interactions.dispatcher,
        ws_hub=ws_hub, attachments=attachments,
    )

    units = _build_units(
        specs, configs, engine_names,
        settings=settings, runtime=runtime, sessions=sessions,
        gateway_factory=gateway_factory, tools=tools,
        profiles=profile_builder, interactions=interactions, sinks_by_agent=sinks_by_agent,
    )
    if not units:
        raise RuntimeError("No agents with a gateway token — nothing to run")

    async def _resolve_conversation(runtime_session_id: str) -> tuple[str, str] | None:
        # Gives tools the current turn's channel + triggering user (e.g. for an
        # ephemeral reply). Strings only cross into the tool layer; the store
        # stays here.
        record = await sessions.get_by_runtime_session(runtime_session_id)
        return (record.channel_id, record.last_user_id) if record else None

    # Which directories each agent may send files from: its own profile (the
    # runtime's working directory), its own attachments, and the system temp dir.
    file_svc = None
    if attachments is not None:
        file_svc = ChatFileService(
            presence,
            sessions,
            {
                spec.name: default_roots(spec.profile_dir, attachments.dir_for(spec.name))
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
    tool_server = tools.build_server(
        directory=registry,
        interaction_svc=interactions.interaction_svc,
        file_svc=file_svc,
        task_svc=task_admin,
        dotenv_path=settings.dotenv_path,
        session_resolver=_resolve_conversation,
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
            dispatcher=SinkTurnDispatcher(sinks_by_agent),
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
        "app built: agents=[%s], mm=%s, data=%s, tools=%s, widgets=%s, ws=%s, "
        "scheduler=%s",
        ", ".join(u.spec.name for u in units),
        settings.mattermost_url,
        settings.data_dir,
        "on" if tool_server else "off",
        "on" if interactions.receiver else "off",
        f"on:{settings.ws_port}" if ws_hub else "off",
        f"on:{settings.scheduler.tick_s:.0f}s" if scheduler else "off",
    )
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
        scheduler=scheduler,
    )


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


async def run(settings: ImpiSettings) -> None:
    app = build_app(settings)
    app.runtime.start()
    if app.tool_server is not None:
        # Up before the gateways: a pi turn may call a tool the moment it starts.
        await app.tool_server.start()
    if app.integrations is not None:
        await app.integrations.start()
    if app.ws_hub is not None:
        await app.ws_hub.start()
    try:
        identities = {}
        for unit in app.units:
            identity = await unit.gateway.login()
            identities[unit.spec.name] = identity
            # The gateway is the only authority on who we are on the platform;
            # the flow needs it to keep our own posts out of replayed history.
            unit.flow.set_identity(identity.user_id)
        await app.registry.sync([unit.spec for unit in app.units], identities)
        # SIGHUP → hot-reload agent profiles (`make reload`). Deliberate and
        # human-triggered: the operator reviews/updates the agents directory first,
        # then signals. Installed after login so `identities` is populated.
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(app.reloader.reload(identities)),
        )
        # N supervised WS loops in one process, plus the scheduler's tick — each
        # isolated from the others. The scheduler goes here rather than into a
        # detached task on purpose: a background task whose exception nobody
        # observes is exactly how a timer dies unnoticed.
        loops = [
            _supervise(f"gateway {unit.spec.name}", unit.gateway.run) for unit in app.units
        ]
        if app.scheduler is not None:
            loops.append(_supervise("scheduler", app.scheduler.run))
        await asyncio.gather(*loops)
    finally:
        if app.scheduler is not None:
            # Before the gateways: a run still in flight needs somewhere to post
            # its outcome, and wants its row closed rather than reclaimed later.
            await app.scheduler.stop()
        if app.ws_hub is not None:
            await app.ws_hub.stop()
        if app.integrations is not None:
            await app.integrations.stop()
        if app.tool_server is not None:
            await app.tool_server.stop()
        for unit in app.units:
            await unit.gateway.stop()
        await app.runtime.close()
        await app.sessions.close()
