"""RegistryService: the live agent directory, synced from profiles at boot.

Implements the AgentDirectory port. The SQLite ``agents`` table is a synced
snapshot (exposed to agents via the ``list_agents`` tool); the in-memory cache
serves gateway dispatch decisions synchronously.
"""

import logging

from crucible.ports.agent import AgentSpec
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.gateway import AgentIdentity
from crucible.store.base import AgentStore

logger = logging.getLogger(__name__)


class RegistryService:
    def __init__(self, store: AgentStore) -> None:
        self._store = store
        self._agents: dict[str, AgentInfo] = {}

    async def sync(
        self, specs: list[AgentSpec], identities: dict[str, AgentIdentity]
    ) -> None:
        """Rebuild the registry from neutral profile facts (name/role/description)
        plus each agent's platform identity, learned from its gateway at login.

        No platform-specific field is read here — swap the gateway and this
        service is untouched. Agents without an identity (no token, failed
        login) are skipped.
        """
        agents: dict[str, AgentInfo] = {}
        for spec in specs:
            identity = identities.get(spec.name)
            if identity is None:
                continue
            agents[spec.name] = AgentInfo(
                name=spec.name,
                role=spec.role,
                description=spec.description,
                username=identity.username,
                user_id=identity.user_id,
            )
        self._agents = agents
        for info in agents.values():
            await self._store.upsert_agent(info)
        logger.info("registry synced: %s", ", ".join(agents) or "(no agents)")

    # -- AgentDirectory port --------------------------------------------------

    def agent_user_ids(self) -> frozenset[str]:
        return frozenset(info.user_id for info in self._agents.values())

    def list_agents(self) -> list[AgentInfo]:
        return sorted(self._agents.values(), key=lambda info: info.name)
