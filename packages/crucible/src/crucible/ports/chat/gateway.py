"""Gateway port: one agent's connection to one chat platform."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentIdentity:
    """The agent account's identity on the platform, learned at login. The
    gateway is the sole authority on this — the neutral layers never derive it
    from a platform-specific profile field."""

    user_id: str
    username: str


class Gateway(Protocol):
    """Lifecycle: login() once, then run() until stop()."""

    async def login(self) -> AgentIdentity:
        """Authenticate and return the agent account's platform identity."""
        ...

    async def run(self) -> None:
        """Consume platform events until stop(); returns when disconnected."""
        ...

    async def stop(self) -> None: ...
