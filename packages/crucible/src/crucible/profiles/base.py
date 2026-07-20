"""ProfileStore port: what the composition root and reloader need from a profile
source (the user's agents directory, the engine's built-in agents, or a composite)."""

from typing import Protocol

from crucible.ports.agent import AgentSpec


class ProfileStore(Protocol):
    def reload(self) -> None: ...
    def list(self) -> list[AgentSpec]: ...
    def get(self, name: str) -> AgentSpec: ...
