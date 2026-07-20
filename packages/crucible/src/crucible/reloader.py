"""ProfileReloader: re-read agent profiles on demand so each agent's next turn
picks up the edited profile, without dropping conversation memory.

Runtime-agnostic: it depends only on ports (``AgentRuntime``) and an injected
``build_profile`` factory, so all backend specifics (how a profile is built,
how sessions are reset) stay in the composition root.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from crucible.ports.agent import AgentProfile, AgentRuntime, AgentSpec
from crucible.ports.chat.gateway import AgentIdentity
from crucible.profiles import ProfileError, ProfileStore
from crucible.unit import AgentUnit

logger = logging.getLogger(__name__)


class RegistrySync(Protocol):
    """The reload's view of the agent registry: rebuild it from the reloaded
    specs plus each agent's platform identity. A concrete directory implements
    this; the reloader stays runtime- and app-agnostic."""

    async def sync(
        self, specs: list[AgentSpec], identities: dict[str, AgentIdentity]
    ) -> None: ...


class ProfileReloader:
    """Re-reads every agent profile on demand (SIGHUP → ``make reload``) so each
    agent's NEXT turn runs with the new config, without losing memory.

    A running agent was configured when its session started, so a reload must
    BOTH swap the flow's profile AND reset the agent's live idle sessions: an
    existing conversation keeps its original configuration until its session is
    reset (the runtime resumes it, so memory survives). A bad edit
    (``ProfileError``) leaves the running config untouched.
    """

    def __init__(
        self,
        *,
        profiles: ProfileStore,
        runtime: AgentRuntime,
        registry: RegistrySync,
        units: list[AgentUnit],
        build_profile: Callable[[AgentSpec], AgentProfile],
    ) -> None:
        self._profiles = profiles
        self._runtime = runtime
        self._registry = registry
        self._units = units
        # Maps a freshly loaded spec onto the agent's runtime profile (the
        # backend mapping plus its stable per-agent env). Owned by the app.
        self._build_profile = build_profile
        self._lock = asyncio.Lock()

    async def reload(self, identities: dict[str, AgentIdentity]) -> None:
        async with self._lock:
            try:
                self._profiles.reload()
            except ProfileError:
                logger.exception("profile reload failed; keeping running config")
                return
            reloaded = 0
            dropped = 0
            for unit in self._units:
                name = unit.spec.name
                try:
                    spec = self._profiles.get(name)
                    unit.flow.set_profile(self._build_profile(spec))
                    unit.spec = spec
                    dropped += await self._runtime.drop_agent_sessions(name)
                    reloaded += 1
                except Exception:
                    # One bad agent must not abort the rest — keep its old config.
                    logger.exception("failed to reload agent %s; keeping its config", name)
            # Registry rebuild so changed role/description/tools reach list_agents.
            await self._registry.sync([u.spec for u in self._units], identities)
            logger.info("reloaded %d agents, dropped %d sessions", reloaded, dropped)
