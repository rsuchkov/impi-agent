"""AgentPresence: look up an agent's live outbound client and inbound sink by name.

The interaction collaborators (UI bridge, dispatcher, service, receiver) resolve an
agent's ``ChatClient`` / ``AgentSink`` through this at request time, so they hold no
per-agent state. The application owns the registry of live agents and implements
this — or wraps its ``{agent: AgentSink}`` map in ``MappingPresence``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from crucible.ports.chat.client import ChatClient

if TYPE_CHECKING:
    from crucible.interactions.dispatcher import AgentSink


class AgentPresence(Protocol):
    def poster(self, agent: str) -> ChatClient | None:
        """The agent's outbound chat client, or None if it has no live presence."""
        ...

    def sink(self, agent: str) -> AgentSink | None:
        """Where a resolved click is routed for the agent, or None."""
        ...


class MappingPresence:
    """AgentPresence backed by a live ``{agent: AgentSink}`` map. The map may be
    empty at construction and filled later — reads are lazy (per request)."""

    def __init__(self, sinks: Mapping[str, AgentSink]) -> None:
        self._sinks = sinks

    def poster(self, agent: str) -> ChatClient | None:
        target = self._sinks.get(agent)
        return target.chat if target is not None else None

    def sink(self, agent: str) -> AgentSink | None:
        return self._sinks.get(agent)
