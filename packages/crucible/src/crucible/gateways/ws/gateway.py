"""WsGateway: the per-agent lifecycle shim for agents living on the WsHub.

Unlike Mattermost/Slack there is no per-agent connection to run — the hub owns
the single server socket for all agents. The gateway's job reduces to the
lifecycle contract: a synthetic identity at login (no platform to ask), and a
run() that parks until stop()."""

import asyncio

from crucible.ports.chat.gateway import AgentIdentity


class WsGateway:
    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._stopped = asyncio.Event()

    async def login(self) -> AgentIdentity:
        # Synthetic: the transport has no account registry. The "ws:" prefix
        # keeps these ids from ever colliding with a real platform user id in
        # the shared directory.
        return AgentIdentity(user_id=f"ws:{self._agent}", username=self._agent)

    async def run(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
