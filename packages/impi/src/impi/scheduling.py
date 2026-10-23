"""The scheduler's ports, implemented over what the app already has.

The scheduler knows nothing about sinks, runtimes or presence — this is where
those meet it. Each concrete is a few lines; the point of the ports is that the
loop stays testable without any of them.
"""

import logging
from collections.abc import Callable, Mapping

from crucible.flows.coalescer import MessageCoalescer
from crucible.gateways.http import HttpHub
from crucible.interactions import AgentSink
from crucible.interactions.presence import AgentPresence
from crucible.ports.agent import AgentProfile, AgentResult, AgentRuntime
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.gateway import GATEWAY_HTTP
from crucible.ports.chat.types import KIND_THREAD, ConversationRef
from crucible.ports.engine_turn import DispatchError, TurnRequest, requested_message
from crucible.ports.turn import TurnScope
from crucible.store.base import (
    GatewayAddress,
    SessionStore,
    derive_runtime_session_id,
    gateway_addresses,
)

logger = logging.getLogger(__name__)

def conversation_ref(
    *, channel_id: str, conversation_id: str, kind: str, message_id: str = ""
) -> ConversationRef:
    """Where a scheduled message goes. A thread task replies inside its thread;
    a DM or channel task posts at the top level, same rule the click and command
    paths use."""
    return ConversationRef(
        channel_id=channel_id,
        conversation_id=conversation_id,
        message_id=message_id or conversation_id,
        thread_root_id=conversation_id if kind == KIND_THREAD else "",
    )


class SinkTurnDispatcher:
    """Runs an engine-started turn the way a slash command runs one: a
    synthetic message into the agent's own sink. It goes through the coalescer
    like everything else — and asks for the outcome, which is the whole
    difference. An agent on the http gateway has no chat to answer in: its
    turn goes through the hub, which opens a journal for the client to read."""

    def __init__(
        self, sinks: Mapping[str, AgentSink], *, http: HttpHub | None = None,
        sessions: SessionStore | None = None,
    ) -> None:
        # The app's live map; read per call, so an agent that reloads or arrives
        # later is picked up without re-wiring.
        self._sinks = sinks
        self._http = http
        # Where a conversation is live, when the agent has several gateways.
        self._sessions = sessions

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        target = self._sinks.get(request.agent)
        served_by_http = self._http is not None and self._http.serves(request.agent)
        if target is None:
            if served_by_http:
                return await self._http.run_turn(request)  # type: ignore[union-attr]
            raise DispatchError(
                f"agent {request.agent!r} is not running (no profile, or no token)"
            )
        gateway, address = await self._where(request, target)
        if gateway == GATEWAY_HTTP and served_by_http:
            return await self._http.run_turn(request)  # type: ignore[union-attr]
        sink = target.sink
        if not isinstance(sink, MessageCoalescer):
            raise DispatchError(f"agent {request.agent!r} cannot report a turn's outcome")
        message = requested_message(
            request,
            conversation_ref(
                channel_id=address.channel_id, conversation_id=address.conversation_id,
                kind=address.kind, message_id=request.message_id,
            ),
            # The marker that nobody is behind this turn is the port's to add
            # (ENGINE_TURN_ATTRIBUTE), the same for every dispatcher.
            turn=TurnScope(turn_id=request.message_id),
            # Marked with the gateway it goes through, so the flow that owns the
            # conversation elsewhere is handed it the way a person's message is.
            gateway=gateway,
        )
        return await sink.submit_tracked(message, target.chat_for(gateway))

    async def _where(self, request: TurnRequest, target: AgentSink) -> tuple[str, GatewayAddress]:
        """Which gateway a turn the engine starts goes to, and the conversation's
        address there: the agent's default gateway when the conversation is live
        in it (or nowhere yet), else the one it last spoke through. The default
        is the operator's choice (the first in ``AGENTS_GATEWAY__<AGENT>``)."""
        own = GatewayAddress(request.channel_id, request.conversation_id, request.kind)
        record = (
            await self._sessions.get_by_runtime_session(
                derive_runtime_session_id(request.agent, request.conversation_id)
            ) if self._sessions is not None else None
        )
        addresses = gateway_addresses(record) if record is not None else {}
        if not addresses or target.default in addresses:
            return target.default, addresses.get(target.default, own)
        if record is not None and record.last_gateway in addresses:
            return record.last_gateway, addresses[record.last_gateway]
        return target.default, own


class RuntimePromptRunner:
    """Runs a task's prompt with no session and no memory. The first production
    caller of run_stateless: cheap, repeatable, and its result is ours to post."""

    def __init__(
        self, runtime: AgentRuntime, profile_for: Callable[[str], AgentProfile | None]
    ) -> None:
        self._runtime = runtime
        # Resolved per call rather than captured, so a hot-reloaded profile
        # applies to the next run.
        self._profile_for = profile_for

    async def run_prompt(self, agent: str, text: str) -> AgentResult:
        profile = self._profile_for(agent)
        if profile is None:
            raise DispatchError(f"agent {agent!r} is not running (no profile, or no token)")
        return await self._runtime.run_stateless(profile, text)


class PresenceNotifier:
    """The scheduler's voice in a conversation, through the agent's own client."""

    def __init__(self, presence: AgentPresence) -> None:
        self._presence = presence

    async def deliver(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str, text: str
    ) -> None:
        poster = self._require(agent)
        await poster.post_reply(
            conversation_ref(channel_id=channel_id, conversation_id=conversation_id, kind=kind),
            text,
        )

    async def announce(
        self, agent: str, *, channel_id: str, conversation_id: str, kind: str, text: str
    ) -> None:
        poster = self._require(agent)
        await poster.post_notice(
            conversation_ref(channel_id=channel_id, conversation_id=conversation_id, kind=kind),
            text,
        )

    def _require(self, agent: str):
        poster = self._presence.poster(agent)
        if poster is None:
            raise DispatchError(f"agent {agent!r} has nowhere to post (not running)")
        return poster
