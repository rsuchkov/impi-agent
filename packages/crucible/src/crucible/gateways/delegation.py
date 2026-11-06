"""The engine's own door: the chat client of a turn one agent opened for
another on a task (``ports.delegation``).

There is no platform behind it, as there is none behind the http journal: what
the turn posts is collected for the asker — its replies as the answer, the
names of the files it sent, the code of a notice it ended with — and read by
the delegation service once the turn is over. A card or a widget has nobody
to show it to: the turn answers to no person, and a tool of its that needs
one is refused where it asks. One client per task.
"""

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
from crucible.ports.delegation import DelegationError

# How much of the asked agent's answer is kept and handed back.
ANSWER_MAX = 20_000


class DelegationChatClient:
    """``TaskTurnChat``: a ``ChatClient`` that keeps what the turn says."""

    def __init__(self) -> None:
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
        self.replies.append(text)

    async def post_notice(self, ref: ConversationRef, text: str, *, code: str = "") -> None:
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
        if text:
            self.replies.append(text)
        self.file_names += [file.name for file in files]

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        raise DelegationError("a task's turn answers to no person who could click")

    async def post_confirmation(
        self, ref: ConversationRef, card: ApprovalCard, actions: list[Action], *, callback_url: str
    ) -> str:
        raise DelegationError("a task's turn answers to no person who could confirm")

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
        raise DelegationError("a task's turn answers to no person who could fill a form")
