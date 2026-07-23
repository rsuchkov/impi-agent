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
import random
import secrets
import signal
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from crucible.ports.agent import AgentProfile, AgentRuntime, AgentSpec
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.gateway import Gateway
from crucible.ports.chat.types import IncomingMessage
from crucible.ports.chat.client import ChatClient
from impi.config import ImpiSettings
from crucible.flows.agent_flow import AgentFlow
from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import AgentSink, InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.service import InteractionService
from crucible.interactions.ui_bridge import WidgetUiBridge
from crucible.loopguard import LoopGuard
from crucible.runtimes.pi import EXTENSION_PATH, build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.profiles import CompositeProfileStore, FsProfileStore, ProfileStore
from crucible.gateways import GatewayFactory, GatewayHandle
from impi.gateways import resolve_gateway
from impi.registry import RegistryService
from crucible.reloader import ProfileReloader
from crucible.store.base import SessionStore
from crucible.store.sessions import SqliteSessionStore
from crucible.unit import AgentUnit
from crucible.tools import ToolRegistry, ToolServer, build_registry
from crucible.tools.base import CAP_CHAT_ADMIN, CAP_FORMS, CAP_WIDGETS

# Importing these runs their @tool decorators so build_registry() sees them:
# crucible's generic ask/form tools plus impi's chat-management tools.
import crucible.builtin_tools  # noqa: E402,F401
import impi.chat_tools  # noqa: E402,F401

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


def _gate_tools(
    registry: ToolRegistry, tools: tuple[str, ...], caps: frozenset[str]
) -> tuple[tuple[str, ...], dict[str, frozenset[str]]]:
    """Split an agent's declared tools into (advertised, {dropped: missing caps}).
    A typed tool is dropped when the agent's gateway/config doesn't provide a
    capability it requires (e.g. a channel-admin tool on a Slack agent). Names that
    aren't typed tools (pi builtins like ``read``) are ignored here."""
    kept: list[str] = []
    dropped: dict[str, frozenset[str]] = {}
    for name in tools:
        tool = registry.get(name)
        if tool is None:
            continue
        missing = tool.requires - caps
        if missing:
            dropped[name] = missing
        else:
            kept.append(name)
    return tuple(kept), dropped


class InteractionWiring:
    """Interaction plumbing shared by all agents.

    Owns ``posters`` (agent -> widget poster) and ``sinks`` (agent -> resolved-click
    sink): both are assigned before the UI bridge and dispatcher capture them, so
    the agent loop fills the exact objects those collaborators read lazily at
    request time. ``ui_bridge`` feeds the runtime; ``dispatcher`` feeds the gateway
    factory. ``finalize`` builds the post-loop interaction service and the HTTP
    receiver once ``posters`` is populated.
    """

    def __init__(self, settings: ImpiSettings, sessions: SqliteSessionStore) -> None:
        # The concrete store, not the SessionStore port: the dispatcher/widget/form
        # collaborators need its InteractionStore + FormStore facets too.
        self._settings = settings
        self._sessions = sessions
        ints = settings.integrations
        self.enabled = ints.enabled
        self.posters: dict[str, ChatClient] = {}
        self.sinks: dict[str, AgentSink] = {}
        # Blocking UI bridge: a runtime mid-turn confirm/select becomes a widget the
        # turn waits on. None when interactions are off — UI requests then fall back
        # to the session's auto-reject backstop.
        self.pending_ui = PendingUiRequests() if ints.enabled else None
        self.ui_bridge = (
            WidgetUiBridge(
                self.posters, sessions, self.pending_ui,
                callback_url=ints.interact_url, timeout=ints.ui_timeout,
            )
            if self.pending_ui is not None
            else None
        )
        # Transport-neutral dispatch: resolves a blocking mid-turn request or feeds a
        # click back as a synthetic message. Shared by the HTTP receiver and every
        # socket-driven gateway; reads `sinks` lazily at dispatch time.
        self.dispatcher = (
            InteractionDispatcher(sessions, self.sinks, self.pending_ui, sessions)
            if self.pending_ui is not None
            else None
        )
        self.interaction_svc: InteractionService | None = None
        self.receiver: InteractionsServer | None = None

    def register(self, name: str, *, chat: ChatClient, sink: AgentSink) -> None:
        self.posters[name] = chat
        self.sinks[name] = sink

    def on_arrival_for(self, name: str) -> Callable[[IncomingMessage], object] | None:
        # A real message cancels any blocking UI request outstanding in that
        # conversation (the user typed instead of clicking).
        pending = self.pending_ui
        if pending is None:
            return None
        return lambda m: pending.cancel_for_conversation(name, m.conversation_id)

    def finalize(self, *, needs_receiver: bool) -> None:
        if not self.enabled or self.pending_ui is None:
            return
        ints = self._settings.integrations
        # One service for widgets (ask) and forms (open_form). A form's "fill in"
        # button clicks to /interact like a widget; the modal submission goes to
        # /dialog. The one concrete store backs all three store facets.
        self.interaction_svc = InteractionService(
            self.posters, self._sessions, self._sessions, self._sessions,
            callback_url=ints.interact_url,
        )
        # The HTTP receiver is only for gateways that deliver callbacks over HTTP
        # (Mattermost); socket gateways (Slack) drive the same dispatcher over their
        # socket, so a Slack-only deployment builds no receiver (and binds no port).
        if needs_receiver:
            assert self.dispatcher is not None  # pending_ui is not None here
            self.receiver = InteractionsServer(
                self.dispatcher, MattermostCallbackCodec(), self.posters,
                host=ints.host, port=ints.port, dialog_submit_url=ints.dialog_url,
            )


class ToolWiring:
    """Per-agent tool bookkeeping and the tool server.

    ``enroll`` mints each agent's auth token, records its admin client and
    capability set, gates its declared tools, and stores its stable tool env.
    ``profile_env`` (re)writes an agent's manifest and returns its profile env —
    called at boot and on every hot-reload, so ``caps`` and ``envs`` stay live.
    A no-op shell when tools are disabled (``registry`` is None).
    """

    def __init__(self, settings: ImpiSettings) -> None:
        self._settings = settings
        self.registry = build_registry() if settings.tools.enabled else None
        self.manifests_dir = Path(settings.data_dir) / "tool-manifests"
        self.tokens: dict[str, str] = {}  # token -> agent name (tool server gate)
        self.admins: dict[str, ChatAdmin] = {}  # agent -> its own admin client
        self.allowlists: dict[str, frozenset[str]] = {}  # agent -> tools it may call
        self.envs: dict[str, dict[str, str]] = {}  # agent -> tool env (stable across reloads)
        self.caps: dict[str, frozenset[str]] = {}  # agent -> capability set
        # Widgets/forms exist iff the integrations receiver is on; chat_admin only on
        # gateways that provide it (Mattermost, not Slack) — added per agent in enroll.
        self.base_caps = (
            frozenset({CAP_WIDGETS, CAP_FORMS}) if settings.integrations.enabled else frozenset()
        )

    @property
    def enabled(self) -> bool:
        return self.registry is not None

    def enroll(self, spec: AgentSpec, handle: GatewayHandle) -> None:
        if self.registry is None:
            return
        token = secrets.token_hex(16)
        self.tokens[token] = spec.name
        if handle.admin is not None:
            self.admins[spec.name] = handle.admin
        caps = self.base_caps | (
            frozenset({CAP_CHAT_ADMIN}) if handle.admin is not None else frozenset()
        )
        self.caps[spec.name] = caps
        advertised, dropped = _gate_tools(self.registry, spec.tools, caps)
        for name, missing in dropped.items():
            logger.info(
                "agent %s: tool %r not advertised — gateway lacks %s",
                spec.name, name, ", ".join(sorted(missing)),
            )
        self.allowlists[spec.name] = frozenset(advertised)
        self.envs[spec.name] = {"TOOL_URL": self._settings.tools.server_url, "TOOL_TOKEN": token}

    def add_env(self, name: str, env: dict[str, str]) -> None:
        # Extra tool env that must apply even with tools disabled (engine agents);
        # separate from enroll, which early-returns when the registry is off.
        self.envs.setdefault(name, {}).update(env)

    def profile_env(self, spec: AgentSpec) -> dict[str, str] | None:
        if self.registry is None:
            return None
        # Manifest re-derived here so a hot-reload re-filters the advertised tools.
        advertised, _ = _gate_tools(self.registry, spec.tools, self.caps.get(spec.name, frozenset()))
        manifest_path = self.registry.write_manifest(self.manifests_dir, spec.name, advertised)
        return {**self.envs[spec.name], "TOOL_MANIFEST": str(manifest_path)}

    def build_server(
        self,
        *,
        directory: RegistryService,
        interaction_svc: InteractionService | None,
        dotenv_path: str,
    ) -> ToolServer | None:
        if self.registry is None:
            return None
        tools = self._settings.tools
        return ToolServer(
            self.registry,
            directory=directory,
            admins=self.admins,
            tokens=self.tokens,
            allowlists=self.allowlists,
            host=tools.server_host,
            port=tools.server_port,
            tool_configs=self.registry.load_configs(env_file=dotenv_path),
            interaction_svc=interaction_svc,
        )


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
) -> AgentUnit:
    profile = profiles.build(spec)
    flow = AgentFlow(runtime, profile, sessions, agent_name=spec.name)
    coalescer = MessageCoalescer(flow, on_arrival=interactions.on_arrival_for(spec.name))
    interactions.register(spec.name, chat=handle.chat, sink=AgentSink(sink=coalescer, chat=handle.chat))
    return AgentUnit(spec=spec, flow=flow, gateway=handle.create_gateway(coalescer))


def _build_units(
    specs: list[AgentSpec],
    engine_names: set[str],
    *,
    settings: ImpiSettings,
    runtime: AgentRuntime,
    sessions: SessionStore,
    gateway_factory: GatewayFactory,
    tools: ToolWiring,
    profiles: ProfileBuilder,
    interactions: InteractionWiring,
) -> tuple[list[AgentUnit], bool]:
    units: list[AgentUnit] = []
    needs_receiver = False  # any agent on an HTTP-callback gateway (Mattermost)?
    for spec in specs:
        config = resolve_gateway(settings, spec.name)  # settings -> neutral config
        if config is None:
            continue  # no token for this agent (resolver logged why)
        handle = gateway_factory.create(spec.name, config)
        if handle is None:
            continue  # unknown gateway kind (factory logged why)
        needs_receiver = needs_receiver or handle.needs_http_receiver
        profiles.set_hint(spec.name, handle.prompt_hint)
        tools.enroll(spec, handle)  # must precede profiles.build (reads caps/env)
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
                runtime=runtime, sessions=sessions, profiles=profiles, interactions=interactions,
            )
        )
    return units, needs_receiver


def build_app(settings: ImpiSettings) -> App:
    user_store = FsProfileStore(
        settings.agents_path,
        default_timeout=settings.pi_timeout,
        default_provider=settings.default_provider,
        default_model=settings.default_model,
        skills_override=settings.skills_for,
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
    )
    engine_names = {spec.name for spec in engine_store.list()}
    profiles = CompositeProfileStore([user_store, engine_store])  # rejects duplicate names
    sessions = SqliteSessionStore(settings.resolved_db_path)

    # Interaction plumbing first: its ui_bridge feeds the runtime and its dispatcher
    # feeds the gateway factory.
    interactions = InteractionWiring(settings, sessions)

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
    tools = ToolWiring(settings)
    profile_builder = ProfileBuilder(tools)
    gateway_factory = GatewayFactory(
        directory=registry, loop_guard=loop_guard, dispatcher=interactions.dispatcher
    )

    specs = [*_select_specs(settings, user_store), *engine_store.list()]
    units, needs_receiver = _build_units(
        specs, engine_names,
        settings=settings, runtime=runtime, sessions=sessions,
        gateway_factory=gateway_factory, tools=tools,
        profiles=profile_builder, interactions=interactions,
    )
    if not units:
        raise RuntimeError("No agents with a gateway token — nothing to run")

    interactions.finalize(needs_receiver=needs_receiver)
    tool_server = tools.build_server(
        directory=registry,
        interaction_svc=interactions.interaction_svc,
        dotenv_path=settings.dotenv_path,
    )
    reloader = ProfileReloader(
        profiles=profiles,
        runtime=runtime,
        registry=registry,
        units=units,
        build_profile=profile_builder.build,
    )

    logger.info(
        "app built: agents=[%s], mm=%s, data=%s, tools=%s, widgets=%s",
        ", ".join(u.spec.name for u in units),
        settings.mattermost_url,
        settings.data_dir,
        "on" if tool_server else "off",
        "on" if interactions.receiver else "off",
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
    )


async def _supervise(name: str, gateway: Gateway, *, sleep=asyncio.sleep) -> None:
    """Keep one agent's WS loop alive without letting its failures reach the
    others. A clean return (disconnect) ends it; any crash is logged and retried
    with capped exponential backoff + jitter, so one flaky agent never takes the
    whole engine down. ``sleep`` is injectable for tests."""
    backoff = 1.0
    while True:
        try:
            await gateway.run()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            delay = backoff + random.uniform(0, backoff)
            logger.exception("gateway %s crashed; reconnecting in %.0fs", name, delay)
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
    try:
        identities = {}
        for unit in app.units:
            identities[unit.spec.name] = await unit.gateway.login()
        await app.registry.sync([unit.spec for unit in app.units], identities)
        # SIGHUP → hot-reload agent profiles (`make reload`). Deliberate and
        # human-triggered: the operator reviews/updates the agents directory first,
        # then signals. Installed after login so `identities` is populated.
        asyncio.get_running_loop().add_signal_handler(
            signal.SIGHUP,
            lambda: asyncio.create_task(app.reloader.reload(identities)),
        )
        # N supervised WS loops in one process; each isolated from the others.
        await asyncio.gather(*(_supervise(u.spec.name, u.gateway) for u in app.units))
    finally:
        if app.integrations is not None:
            await app.integrations.stop()
        if app.tool_server is not None:
            await app.tool_server.stop()
        for unit in app.units:
            await unit.gateway.stop()
        await app.runtime.close()
        await app.sessions.close()
