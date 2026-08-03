"""ToolWiring: an application's per-agent tool bookkeeping and the tool server,
assembled from a ``ToolSettings`` config.

``enroll`` mints each agent's auth token, records its admin client and capability
set, gates its declared tools, and stores its stable tool env. ``profile_env``
(re)writes an agent's manifest and returns its profile env — called at boot and on
every hot-reload, so ``caps`` and ``envs`` stay live. A no-op shell when tools are
disabled (``registry`` is None).

A convenience for composition roots; an app may wire the registry/server by hand.
"""

import logging
import secrets
from pathlib import Path

from crucible.config import ToolSettings
from crucible.ports.agent import AgentSpec
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.interactions import InteractionService
from crucible.tools.base import CAP_CHAT_ADMIN, CAP_FORMS, CAP_WIDGETS
from crucible.tools.registry import ToolRegistry, build_registry
from crucible.tools.server import SessionResolver, ToolServer

logger = logging.getLogger(__name__)


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


class ToolWiring:
    def __init__(self, tools: ToolSettings, *, data_dir: str, interactivity_on: bool) -> None:
        self._tools = tools
        self.registry = build_registry() if tools.enabled else None
        self.manifests_dir = Path(data_dir) / "tool-manifests"
        self.tokens: dict[str, str] = {}  # token -> agent name (tool server gate)
        self.admins: dict[str, ChatAdmin] = {}  # agent -> its own admin client
        self.allowlists: dict[str, frozenset[str]] = {}  # agent -> tools it may call
        self.envs: dict[str, dict[str, str]] = {}  # agent -> tool env (stable across reloads)
        self.caps: dict[str, frozenset[str]] = {}  # agent -> capability set
        # Widgets/forms exist iff the integrations receiver is on; chat_admin only on
        # gateways that provide it (Mattermost, not Slack) — added per agent in enroll.
        self.base_caps = (
            frozenset({CAP_WIDGETS, CAP_FORMS}) if interactivity_on else frozenset()
        )

    @property
    def enabled(self) -> bool:
        return self.registry is not None

    def enroll(
        self, spec: AgentSpec, admin: ChatAdmin | None,
        *, extra_caps: frozenset[str] = frozenset(),
    ) -> None:
        if self.registry is None:
            return
        token = secrets.token_hex(16)
        self.tokens[token] = spec.name
        if admin is not None:
            self.admins[spec.name] = admin
        # base (widgets/forms) + chat-admin if the agent has an admin client +
        # whatever extra capabilities its gateway kind advertises (e.g. ephemeral).
        caps = (
            self.base_caps
            | (frozenset({CAP_CHAT_ADMIN}) if admin is not None else frozenset())
            | extra_caps
        )
        self.caps[spec.name] = caps
        advertised, dropped = _gate_tools(self.registry, spec.tools, caps)
        for name, missing in dropped.items():
            logger.info(
                "agent %s: tool %r not advertised — gateway lacks %s",
                spec.name, name, ", ".join(sorted(missing)),
            )
        self.allowlists[spec.name] = frozenset(advertised)
        self.envs[spec.name] = {"TOOL_URL": self._tools.server_url, "TOOL_TOKEN": token}

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
        directory: AgentDirectory,
        interaction_svc: InteractionService | None,
        dotenv_path: str,
        session_resolver: SessionResolver | None = None,
    ) -> ToolServer | None:
        if self.registry is None:
            return None
        return ToolServer(
            self.registry,
            directory=directory,
            admins=self.admins,
            tokens=self.tokens,
            allowlists=self.allowlists,
            host=self._tools.server_host,
            port=self._tools.server_port,
            tool_configs=self.registry.load_configs(env_file=dotenv_path),
            interaction_svc=interaction_svc,
            session_resolver=session_resolver,
        )
