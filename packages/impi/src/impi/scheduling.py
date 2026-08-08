"""The scheduler's ports, implemented over what the app already has.

The scheduler knows nothing about sinks, runtimes or presence — this is where
those meet it. Each concrete is a few lines; the point of the ports is that the
loop stays testable without any of them.
"""

import logging
from collections.abc import Callable, Mapping

from crucible.flows.coalescer import MessageCoalescer
from crucible.interactions import AgentSink
from crucible.interactions.presence import AgentPresence
from crucible.ports.agent import AgentProfile, AgentResult, AgentRuntime
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import KIND_THREAD, ConversationRef, IncomingMessage
from crucible.scheduler.ports import DispatchError, TurnRequest

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
    """Runs a scheduled turn the way a slash command runs one: a synthetic
    message into the agent's own sink. It goes through the coalescer like
    everything else — and asks for the outcome, which is the whole difference."""

    def __init__(self, sinks: Mapping[str, AgentSink]) -> None:
        # The app's live map; read per call, so an agent that reloads or arrives
        # later is picked up without re-wiring.
        self._sinks = sinks

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        target = self._sinks.get(request.agent)
        if target is None:
            raise DispatchError(
                f"agent {request.agent!r} is not running (no profile, or no token)"
            )
        sink = target.sink
        if not isinstance(sink, MessageCoalescer):
            raise DispatchError(f"agent {request.agent!r} cannot report a turn's outcome")
        message = IncomingMessage(
            ref=conversation_ref(
                channel_id=request.channel_id, conversation_id=request.conversation_id,
                kind=request.kind, message_id=request.message_id,
            ),
            text=request.text,
            user_id=request.user_id,
            username=request.username,
            kind=request.kind,
            # Addressed to this agent by definition, and not typed by a human —
            # the same two flags a command sets.
            mentioned=True,
            synthetic=True,
        )
        return await sink.submit_tracked(message, target.chat)


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
