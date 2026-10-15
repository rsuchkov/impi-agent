"""A turn the engine starts by itself, and what runs it.

A person's message opens a turn through a gateway; the engine opens one on its
own when a task comes due or an application reports an event — a pipeline
finished, a ticket moved — and wants the agent back in the conversation with
its memory. ``TurnRequest`` names the conversation and what the turn is about,
``TurnDispatcher`` runs it where the agent lives (a chat sink, the http hub)
and answers with how it ended. Neutral: the scheduler and an application both
make requests, and a gateway as well as a composition root may run them.
"""

from dataclasses import dataclass, replace
from typing import Protocol

from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import ConversationRef, EventReport, IncomingMessage
from crucible.ports.turn import ENGINE_TURN_ATTRIBUTE, TurnScope


class DispatchError(Exception):
    """The turn could not be started at all — the agent is not live, or the
    conversation cannot be addressed. Distinct from a turn that ran and failed."""


@dataclass(frozen=True)
class TurnRequest:
    """One engine-started turn, addressed the way a click or a command is.

    ``channel_id`` and ``conversation_id`` are the engine's own keys for the
    conversation (what the session store knows it by: a platform's ids on a
    chat gateway, the realm-namespaced key on the http gateway).
    ``message_id`` is the run's own id, so the flow's replay dedup never
    mistakes two runs for one; ``username`` is who the prompt appears to come
    from, and is never empty — the prompt envelope would render an anonymous
    ``[]:`` line. ``event`` is what the turn is about, quoted as data; ``text``
    then says it in one phrase. ``turn`` is the scope the turn runs under
    (attributes for the tools, secrets the application holds); None lets the
    dispatcher give it one that says nobody is behind this turn."""

    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    text: str
    message_id: str
    user_id: str = ""
    username: str = "scheduler"
    event: EventReport | None = None
    turn: TurnScope | None = None


class TurnDispatcher(Protocol):
    """Runs an engine-started turn in the request's conversation, with the
    agent's memory, and reports how it ended. Raises DispatchError if it never
    started."""

    async def run_turn(self, request: TurnRequest) -> TurnOutcome: ...


def requested_message(request: TurnRequest, ref: ConversationRef, *, turn: TurnScope) -> IncomingMessage:
    """The message a dispatcher hands the agent's sink for a request: addressed
    to the agent by definition and not typed by a human — the two flags a
    command sets — marked as the engine's own turn, and carrying the event.
    ``turn`` is the scope the dispatcher settled on; a request's own scope
    comes through with the dispatcher's turn id and fresh flags, since a flag
    a tool leaves must not outlive the turn it was left in. A request with no
    scope of its own gets the dispatcher's, marked ``ENGINE_TURN_ATTRIBUTE``:
    the promise ``TurnRequest.turn`` makes, kept here for every dispatcher."""
    if request.turn is not None:
        scope = replace(request.turn, turn_id=turn.turn_id, flags=set())
    else:
        scope = replace(turn, attributes={**turn.attributes, ENGINE_TURN_ATTRIBUTE: "1"})
    return IncomingMessage(
        ref=ref,
        text=request.text,
        user_id=request.user_id,
        username=request.username,
        kind=request.kind,
        mentioned=True,
        synthetic=True,
        engine_started=True,
        event=request.event,
        turn=scope,
    )
