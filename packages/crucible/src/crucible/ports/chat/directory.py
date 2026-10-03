"""AgentDirectory port: who our agents are, platform-neutrally.

Gateways use it for dispatch decisions (e.g. "is this channel's only resident
agent me?"); later stages expose it to agents themselves via a tool.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentInfo:
    """An agent's platform-neutral identity. ``username``/``user_id`` match the
    vocabulary in chat.types (they are the agent account's login and id on
    whichever gateway); the gateway-specific binding lives in the profile layer."""

    name: str
    role: str
    description: str
    username: str
    user_id: str


class AgentDirectory(Protocol):
    def agent_user_ids(self) -> frozenset[str]:
        """Platform user ids of all enabled agents (sync: served from cache)."""
        ...

    def list_agents(self) -> list[AgentInfo]: ...


class StaticDirectory:
    """An ``AgentDirectory`` for an application whose agents are fixed at
    composition: a single agent behind an HTTP API, a bot with a hard-coded
    roster. Hands back what it was given; the application that keeps a live
    registry (synced from a store, learning platform ids at login) implements the
    port itself."""

    def __init__(self, agents: Sequence[AgentInfo]) -> None:
        self._agents = tuple(agents)

    def agent_user_ids(self) -> frozenset[str]:
        return frozenset(a.user_id for a in self._agents if a.user_id)

    def list_agents(self) -> list[AgentInfo]:
        return list(self._agents)
