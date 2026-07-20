"""AgentDirectory port: who our agents are, platform-neutrally.

Gateways use it for dispatch decisions (e.g. "is this channel's only resident
agent me?"); later stages expose it to agents themselves via a tool.
"""

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
