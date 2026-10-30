"""Tracers for a flow that answers on more than one gateway.

A turn's tool activity is shown where the turn is watched: an HTTP caller
reads it from the turn's journal, a chat turn gets the widget under its reply,
a platform with a view of its own gets that. One flow, one ``tracer`` — so an
agent reached through several gateways hands the flow a ``TracerByGateway``,
which picks the tracer by the gateway the turn came through. By the gateway's
name, never by the type of the chat client: an application that wraps its
client is still answered right.
"""

from __future__ import annotations

from collections.abc import Mapping

from crucible.flows.agent_flow import ActiveTrace, TurnTracer
from crucible.ports.agent.runtime import AgentEvent
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import ConversationRef
from crucible.store.base import SessionRecord


class NoTrace:
    """A trace that draws nothing — what a turn gets on a gateway no tracer
    was named for and with no default."""

    def on_event(self, event: AgentEvent) -> None:
        return None

    async def end(self, *, interrupted: bool = False) -> None:
        return None


class TracerByGateway:
    def __init__(self, by_gateway: Mapping[str, TurnTracer], default: TurnTracer | None = None) -> None:
        self._by_gateway = dict(by_gateway)
        self._default = default

    def begin(
        self, record: SessionRecord, chat: ChatClient, *,
        anchor: ConversationRef | None = None, gateway: str = "",
    ) -> ActiveTrace:
        tracer = self._by_gateway.get(gateway, self._default)
        if tracer is None:
            return NoTrace()
        return tracer.begin(record, chat, anchor=anchor, gateway=gateway)
