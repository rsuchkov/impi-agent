"""The engine's own door: the chat client of a turn one agent opened for
another on a task (``ports.delegation``).

There is no platform behind it, as there is none behind the http journal: what
the turn posts is collected for the asker — its replies as the answer, the
names of the files it sent (where the application lets such a turn send
any), the code of a notice it ended with — and read by
the delegation service once the turn is over. A card or a widget has nobody
to show it to: the turn answers to no person, and a tool of its that needs
one is refused where it asks. One client per task; the one an agent's sink
holds for the door itself (``keeps=False``) is reached only by what resolves a
chat by the conversation's gateway, and keeps nothing — the task's own client
is the one read. The trusted door's client (``TrustedDelegationChatClient``)
differs in one thing: what the turn puts to the person goes to the asker's
conversation, where the person is — a card, on every gateway that
conversation is live in; a notice addressed to them; the wait on their answer.
"""

import logging
from dataclasses import replace

from crucible.ports.agent.errors import TURN_END_NOTICES
from crucible.ports.chat.client import ChatClient, ConversationView, NobodyToAnswer
from crucible.ports.chat.types import (
    Action,
    ApprovalCard,
    Card,
    ConversationRef,
    Form,
    OutgoingFile,
    PostSnippet,
    UserProfile,
)
from crucible.ports.delegation import TrustedLink, TrustedLinks

logger = logging.getLogger(__name__)

# How much of the asked agent's answer is kept and handed back.
ANSWER_MAX = 20_000


class DelegationChatClient:
    """``DelegatedTurnChat``: a ``ChatClient`` that keeps what the turn says —
    or, with ``keeps`` False, drops it: the sink's door, whose posts nobody reads."""

    def __init__(self, *, keeps: bool = True) -> None:
        self.keeps = keeps
        self.replies: list[str] = []
        self.file_names: list[str] = []
        self.notice_code = ""

    @property
    def answer(self) -> str:
        parts = list(self.replies)
        if self.file_names:
            parts.append("Files the agent produced (kept in its own conversation): " + ", ".join(self.file_names))
        return "\n\n".join(parts)[:ANSWER_MAX]

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        if self.keeps:
            self.replies.append(text)

    async def post_notice(self, ref: ConversationRef, text: str, *, code: str = "") -> None:
        # How the turn ended is what the asker reads; a note about the turn
        # said after it (a tool's flag) does not take its place.
        if self.keeps and (code in TURN_END_NOTICES or not self.notice_code):
            self.notice_code = code

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        return

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        return

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        return None

    async def resolve_channel(self, channel_id: str) -> str:
        return ""

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        return []  # the task is the whole conversation

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return []

    def format_mention(self, username: str, *, user_id: str = "") -> str:
        return f"@{username}"

    async def post_files(
        self, ref: ConversationRef, files: list[OutgoingFile], *, text: str = "", hop_depth: int = 0
    ) -> None:
        if not self.keeps:
            return
        if text:
            self.replies.append(text)
        self.file_names += [file.name for file in files]

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        raise NobodyToAnswer("a task's turn answers to no person who could click")

    async def post_confirmation(
        self, ref: ConversationRef, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        raise NobodyToAnswer("a task's turn answers to no person who could confirm")

    async def update_confirmation(
        self, post_id: str, card: ApprovalCard, actions: list[Action] | None = None, *, callback_url: str = "",
    ) -> None:
        return

    async def retract(self, post_id: str, text: str) -> None:
        return

    async def delete_post(self, post_id: str) -> None:
        return

    async def post_cards(self, ref: ConversationRef, cards: list[Card], *, callback_url: str) -> str:
        return ""  # a view of the turn (its steps) drawn for nobody: nothing to draw on

    async def update_cards(self, post_id: str, cards: list[Card], *, callback_url: str) -> None:
        return

    async def open_dialog(self, trigger_id: str, form: Form, *, submit_url: str, state: str) -> None:
        raise NobodyToAnswer("a task's turn answers to no person who could fill a form")


class TrustedDelegationChatClient(DelegationChatClient):
    """The trusted door's client: what the turn says is kept for the asker's
    turn as before, and what it puts to the person goes to the asker's
    conversation, through the asker's own clients, under the asked agent's
    name (``links`` says whose task a conversation is). A card goes to every
    gateway the asker's conversation is live in, as the asker's own card
    does; a notice goes where the asker's turn answers, unless it says how
    the task's turn ended — that is the asker's to read (``notice_code``)
    and to put in words; and while a card waits, the asker's conversation is
    shown waiting (``ConversationView``), the asker's turn being the one the
    person watches."""

    def __init__(self, links: TrustedLinks, *, keeps: bool = True) -> None:
        super().__init__(keeps=keeps)
        self._links = links
        # A card's copies, by the id the card is known by here (its first
        # copy's): the client and the post id of each, and the asked agent's
        # name — a rewrite or a retraction goes to every one.
        self._posted: dict[str, tuple[list[tuple[ChatClient, str]], str]] = {}
        # Where a wait is shown, by the task's conversation: taken back there
        # even when the task is over by then and its link gone.
        self._waiting: dict[str, tuple[ConversationView, ConversationRef]] = {}

    def _link(self, ref: ConversationRef, what: str) -> TrustedLink:
        link = self._links.link_for(ref.conversation_id)
        if link is None:
            raise NobodyToAnswer(f"a task's turn answers to no person who could {what}")
        return link

    async def post_notice(self, ref: ConversationRef, text: str, *, code: str = "") -> None:
        await super().post_notice(ref, text, code=code)
        if code in TURN_END_NOTICES:
            return
        link = self._links.link_for(ref.conversation_id)
        if link is not None:
            await link.poster.post_notice(link.ref, f"{link.to}: {text}", code=code)

    async def post_confirmation(
        self, ref: ConversationRef, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        link = self._link(ref, "confirm")
        named = _named_for(card, link.to)
        copies: list[tuple[ChatClient, str]] = []
        failure: Exception | None = None
        for poster, where in ((link.poster, link.ref), *link.elsewhere):
            try:
                post_id = await poster.post_confirmation(where, named, actions, callback_url=callback_url)
            except Exception as error:  # noqa: BLE001 - one gateway down must not cost the others their copy
                logger.warning("a trusted task's card could not be posted in %s", where.channel_id or "-", exc_info=True)
                failure = failure or error
                continue
            if post_id:
                copies.append((poster, post_id))
        if not copies:
            if failure is not None:
                raise failure
            return ""
        self._posted[copies[0][1]] = (copies, link.to)
        return copies[0][1]

    async def update_confirmation(
        self, post_id: str, card: ApprovalCard, actions: list[Action] | None = None, *, callback_url: str = "",
    ) -> None:
        posted = self._posted.get(post_id)
        if posted is None:
            return
        copies, to = posted
        named = _named_for(card, to)
        for poster, copy_id in copies:
            try:
                await poster.update_confirmation(copy_id, named, actions, callback_url=callback_url)
            except Exception:  # noqa: BLE001 - the other copies are still rewritten
                logger.warning("a trusted task's card could not be rewritten", exc_info=True)

    async def retract(self, post_id: str, text: str) -> None:
        copies, _ = self._posted.pop(post_id, ([], ""))
        for poster, copy_id in copies:
            await poster.retract(copy_id, text)

    async def delete_post(self, post_id: str) -> None:
        copies, _ = self._posted.pop(post_id, ([], ""))
        for poster, copy_id in copies:
            await poster.delete_post(copy_id)

    async def set_busy(self, ref: ConversationRef, busy: bool) -> None:
        return  # the asker's conversation is busy with the asker's own turn, which says so itself

    async def set_waiting(self, ref: ConversationRef, waiting: bool) -> None:
        if not waiting:
            shown = self._waiting.pop(ref.conversation_id, None)
            if shown is not None:
                await shown[0].set_waiting(shown[1], False)
            return
        link = self._links.link_for(ref.conversation_id)
        if link is not None and isinstance(link.poster, ConversationView):
            self._waiting[ref.conversation_id] = (link.poster, link.ref)
            await link.poster.set_waiting(link.ref, True)


def _named_for(card: ApprovalCard, agent: str) -> ApprovalCard:
    """The card as the asker's conversation shows it: the asked agent's name
    ahead of the title, so a person reads whose tool asks — the card's own
    ``agent`` already says so where the card has no title."""
    return replace(card, title=f"{agent}: {card.title}") if card.title else card
