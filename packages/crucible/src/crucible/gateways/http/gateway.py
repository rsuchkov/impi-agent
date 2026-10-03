"""HttpGateway: the per-agent lifecycle shim for agents living on the HttpHub.

As with the ws hub, there is no per-agent connection: the hub owns the one
server for every agent, so the gateway's job is the lifecycle contract — a
synthetic identity at login (no platform to ask), and a run() that parks."""

import asyncio

from crucible.ports.chat.gateway import AgentIdentity


class HttpGateway:
    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._stopped = asyncio.Event()

    async def login(self) -> AgentIdentity:
        # The prefix keeps these ids from colliding with a real platform user
        # id in the shared directory.
        return AgentIdentity(user_id=f"http:{self._agent}", username=self._agent)

    async def run(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
