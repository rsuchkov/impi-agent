"""The chat client of a turn an MCP call opened.

What the turn says goes into its journal, as on the http gateway — the
gateway reads the answer out of it when the turn ends, and a client following
a task reads the turn's status from it. What the turn puts to the person — a
confirmation card — cannot go to the MCP client: the program on the other end
speaks for the person but is not them, and a yes it typed would be the model's
yes. The card goes where the application can put it in front of the person
(``ApprovalCards``: a direct message on a chat platform, a page of its own);
the application answers it through its own gateway, and the person is the same
one by the application's keying of people. A gateway with no ``ApprovalCards``
has nobody to ask: a tool that needs a yes is refused where it asks.
"""

import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from crucible.gateways.http.client import HttpChatClient
from crucible.gateways.http.turns import HttpTurns
from crucible.gateways.mcp.auth import McpCaller, handle_of
from crucible.ports.chat.client import NobodyToAnswer
from crucible.ports.chat.gateway import MCP_SCOPE_ACT
from crucible.ports.chat.types import Action, ApprovalCard, ConversationRef, Form

logger = logging.getLogger(__name__)

# How many forwarded cards are remembered by their post ids: a verdict rewrites
# a card minutes after it was put up, not days.
_FORWARDED_MAX = 1000


@dataclass(frozen=True)
class CardOrigin:
    """Where the card comes from, for the application to put it in context:
    the conversation's key (``mcp:<subject>:<handle>``) and handle, and the
    turn that asks — what the application keyed its own record of the call
    by, if it kept one."""

    conversation: str
    handle: str
    turn_id: str


class ApprovalCards(Protocol):
    """Where the application puts a confirmation an MCP turn needs in front
    of the person. ``post_confirmation`` is given the caller — the person
    the card is for and the client that asked — and where the card comes from
    (``CardOrigin``), and answers with the post's id as the application knows
    it; the other verbs name that id. The card's controls carry the engine's
    approval token: the application's gateway hands a click to the dispatcher
    as it does its own cards'."""

    async def post_confirmation(
        self, asker: McpCaller, card: ApprovalCard, actions: list[Action], *, origin: CardOrigin, callback_url: str
    ) -> str: ...

    async def update_confirmation(
        self, post_id: str, card: ApprovalCard, actions: list[Action] | None = None, *, callback_url: str = ""
    ) -> None: ...

    async def retract(self, post_id: str, text: str) -> None: ...


class McpChatClient(HttpChatClient):
    """``HttpChatClient`` over the gateway's turns, with cards forwarded to
    the application. ``asker_of`` names the caller whose call runs in a
    conversation, or None when none does."""

    def __init__(
        self,
        turns: HttpTurns,
        agent: str,
        *,
        cards: ApprovalCards | None,
        asker_of: Callable[[str], McpCaller | None],
    ) -> None:
        super().__init__(turns, agent)
        self._delivery = cards
        self._asker_of = asker_of
        # The journal's post id -> the application's, for a rewrite or a retraction.
        self._forwarded: OrderedDict[str, str] = OrderedDict()

    async def post_confirmation(
        self, ref: ConversationRef, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        asker = self._asker_of(ref.conversation_id)
        if self._delivery is None or asker is None:
            raise NobodyToAnswer("an MCP client speaks for the person and cannot answer for them")
        if not asker.allows(MCP_SCOPE_ACT):
            # The token may not act: a card is a step past what the person let
            # the client do, whichever agent's turn raised it — the client's
            # own, or a specialist's it asked, whose card comes back through here.
            raise NobodyToAnswer("the client's token may only read; nothing it asks for gets a yes")
        card = _asked_through(card, asker)
        turn = self._turns.active(ref.conversation_id)
        origin = CardOrigin(
            conversation=ref.conversation_id, handle=handle_of(ref.conversation_id),
            turn_id=turn.id if turn is not None else "",
        )
        theirs = await self._delivery.post_confirmation(asker, card, actions, origin=origin, callback_url=callback_url)
        post_id = await super().post_confirmation(ref, card, actions, callback_url=callback_url)
        if post_id and theirs:
            self._forwarded[post_id] = theirs
            while len(self._forwarded) > _FORWARDED_MAX:
                self._forwarded.popitem(last=False)
        return post_id

    async def update_confirmation(
        self, post_id: str, card: ApprovalCard, actions: list[Action] | None = None, *, callback_url: str = ""
    ) -> None:
        await super().update_confirmation(post_id, card, actions, callback_url=callback_url)
        theirs = self._forwarded.get(post_id)
        if theirs is not None and self._delivery is not None:
            asker = self._asker_of(self._conversation_of(post_id)) if actions else None
            await self._delivery.update_confirmation(
                theirs, _asked_through(card, asker) if asker is not None else card, actions, callback_url=callback_url,
            )

    async def retract(self, post_id: str, text: str) -> None:
        await super().retract(post_id, text)
        theirs = self._forwarded.pop(post_id, None)
        if theirs is not None and self._delivery is not None:
            await self._delivery.retract(theirs, text)

    def _conversation_of(self, post_id: str) -> str:
        turn = self._turn_of_post(post_id)
        return turn.conversation if turn is not None else ""

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        raise NobodyToAnswer("an MCP client speaks for the person and cannot click for them")

    async def open_dialog(self, trigger_id: str, form: Form, *, submit_url: str, state: str) -> None:
        raise NobodyToAnswer("an MCP client speaks for the person and cannot fill a form for them")


def _asked_through(card: ApprovalCard, asker: McpCaller) -> ApprovalCard:
    """The card says the turn was asked through a program: the person reading
    it in their chat did not type the request they are asked to approve."""
    return replace(card, asked_through=asker.client_words)
