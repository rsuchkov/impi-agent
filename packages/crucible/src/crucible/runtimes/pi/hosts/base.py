"""What a runtime host IS, and which agent gets which one.

The port and the wiring only. Each implementation lives beside this file, named
for where it runs the process: :mod:`local` starts it as a child of this
process, :mod:`remote` asks a host in the agent's own container. Nothing here
knows how either does it.
"""

import logging
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.runtimes.pi.transport import PiTransport

logger = logging.getLogger(__name__)


@runtime_checkable
class RuntimeHost(Protocol):
    """Somewhere a runtime process can be started and spoken to."""

    async def open(self, request: SpawnRequest) -> PiTransport:
        """Start the process and return the channel to it."""
        ...

    async def aclose(self) -> None:
        """Release whatever the host holds between spawns. Idempotent."""


class HostRouter:
    """Which host runs which agent: a default, plus per-agent overrides.

    Overrides are what makes the move gradual — an agent can be given a host of
    its own while every other agent still runs here, and a deployment that has
    moved nobody holds a router with no overrides at all.
    """

    def __init__(
        self, default: RuntimeHost, overrides: Mapping[str, RuntimeHost] | None = None
    ) -> None:
        self._default = default
        self._overrides = dict(overrides or {})

    def for_agent(self, agent: str) -> RuntimeHost:
        return self._overrides.get(agent, self._default)

    def add(self, agent: str, host: RuntimeHost) -> None:
        self._overrides[agent] = host

    @property
    def remote_agents(self) -> tuple[str, ...]:
        """The agents that do NOT run here. Named for logs and health checks —
        "which agents am I not hosting" is the first question when one is quiet."""
        return tuple(sorted(self._overrides))

    async def aclose(self) -> None:
        # Distinct hosts only: several agents may share one, and closing it twice
        # is not the same as closing it once for a host that holds a connection.
        seen: list[RuntimeHost] = []
        for host in [self._default, *self._overrides.values()]:
            if any(host is other for other in seen):
                continue
            seen.append(host)
            try:
                await host.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not be derailed
                logger.warning("Runtime host did not close cleanly", exc_info=True)
