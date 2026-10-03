"""HttpChatClient: the ChatClient over HttpTurns, for one agent.

Whatever the agent posts into a conversation becomes an event in the journal of
the turn running there: a reply, a notice with its code, a card with its
controls, a file. There is no platform on the other side — a program reads the
journal and draws what it likes. The widget verbs are real here, unlike on the
ws gateway: a confirmation card is an ``actions`` event, and the client answers
it through the hub's actions endpoint.
"""

import base64
import logging
import uuid

from crucible.gateways.http.journal import (
    EV_ACTIONS,
    EV_ACTIONS_RETIRED,
    EV_CARDS,
    EV_FILE,
    EV_MESSAGE,
    EV_NOTICE,
    STATUS_AWAITING_INPUT,
    STATUS_RUNNING,
)
from crucible.gateways.http.turns import HttpTurns, Turn
from crucible.ports.chat.types import (
    Action,
    Card,
    ConversationRef,
    Form,
    OutgoingFile,
    PostSnippet,
    UserProfile,
)

logger = logging.getLogger(__name__)


def _wire_action(action: Action) -> dict:
    return {
        "id": action.id,
        "label": action.label,
        "value": action.value,
        "style": action.style,
        "kind": action.kind,
        "options": [{"label": c.label, "value": c.value} for c in action.options],
    }


class HttpChatClient:
    def __init__(self, turns: HttpTurns, agent: str) -> None:
        self._turns = turns
        self._agent = agent

    def _turn(self, ref: ConversationRef, what: str) -> Turn | None:
        turn = self._turns.active(ref.conversation_id)
        if turn is None:
            # Posted after the turn ended (a late notice) or outside any turn:
            # nobody is reading, and there is no journal to append to.
            logger.info("%s for %s arrived with no turn running; dropped", what, ref.conversation_id)
        return turn

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        turn = self._turn(ref, "a reply")
        if turn is not None:
            turn.journal.emit(EV_MESSAGE, messageId=uuid.uuid4().hex, format="markdown", text=text)

    async def post_notice(self, ref: ConversationRef, text: str, *, code: str = "") -> None:
        turn = self._turn(ref, "a notice")
        if turn is not None:
            turn.journal.emit(EV_NOTICE, code=code, text=text)

    async def post_files(
        self, ref: ConversationRef, files: list[OutgoingFile], *, text: str = ""
    ) -> None:
        turn = self._turn(ref, "a file")
        if turn is None:
            return
        for index, file in enumerate(files):
            turn.journal.emit(
                EV_FILE, name=file.name, mime=file.mime,
                data=base64.b64encode(file.data).decode("ascii"),
                text=text if index == 0 else "",
            )

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        return  # the journal's status is the "working on it" mark

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        return

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        return None

    async def resolve_channel(self, channel_id: str) -> str:
        return ""

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        return []  # the runtime's own memory is the only history

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return []

    def format_mention(self, username: str) -> str:
        return f"@{username}"

    # -- interactive: real here, answered through the hub's actions endpoint -----

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        turn = self._turn(ref, "a card")
        if turn is None:
            return ""
        post_id = uuid.uuid4().hex
        turn.posted[post_id] = tuple(actions)
        turn.blocking.add(post_id)
        turn.journal.emit(
            EV_ACTIONS, postId=post_id, text=text, actions=[_wire_action(a) for a in actions]
        )
        turn.journal.set_status(STATUS_AWAITING_INPUT)
        return post_id

    async def retract(self, post_id: str, text: str) -> None:
        turn = self._turn_of_post(post_id)
        if turn is None:
            return
        turn.posted.pop(post_id, None)
        turn.blocking.discard(post_id)
        turn.journal.emit(EV_ACTIONS_RETIRED, postId=post_id, text=text)
        if not turn.blocking:
            turn.journal.set_status(STATUS_RUNNING)

    async def post_cards(
        self, ref: ConversationRef, cards: list[Card], *, callback_url: str
    ) -> str:
        turn = self._turn(ref, "cards")
        if turn is None:
            return ""
        post_id = uuid.uuid4().hex
        self._cards(turn, post_id, cards)
        return post_id

    async def update_cards(
        self, post_id: str, cards: list[Card], *, callback_url: str
    ) -> None:
        turn = self._turn_of_post(post_id)
        if turn is not None:
            self._cards(turn, post_id, cards)

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        # Not reached in practice: forms are not advertised to an http agent
        # (the gateway denies CAP_FORMS). Kept for the port's sake.
        logger.warning(
            "agent %s asked to open a form — not available on the HTTP gateway", self._agent
        )

    def _cards(self, turn: Turn, post_id: str, cards: list[Card]) -> None:
        turn.posted[post_id] = tuple(a for card in cards for a in card.actions)
        turn.journal.emit(
            EV_CARDS, postId=post_id,
            cards=[
                {"text": c.text, "accent": c.accent, "actions": [_wire_action(a) for a in c.actions]}
                for c in cards
            ],
        )

    def _turn_of_post(self, post_id: str) -> Turn | None:
        for turn in self._turns.posted_in():
            if post_id in turn.posted:
                return turn
        return None
