"""Per-agent lifecycle shim for the MCP gateway: the hub serves every agent
registered on it, so an agent's gateway only names itself and waits."""

import asyncio

from crucible.ports.chat.gateway import AgentIdentity


class McpGateway:
    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._stopped = asyncio.Event()

    async def login(self) -> AgentIdentity:
        return AgentIdentity(user_id=f"mcp:{self._agent}", username=self._agent)

    async def run(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
