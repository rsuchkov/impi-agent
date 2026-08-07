"""In-process fake of the ChatClient port."""

from crucible.ports.chat.types import (
    Action,
    Card,
    ConversationRef,
    Form,
    OutgoingFile,
    PostSnippet,
    UserProfile,
)


class FakeChat:
    def __init__(self) -> None:
        self.replies: list[tuple[ConversationRef, str]] = []
        self.reply_hops: list[int] = []  # hop_depth of each reply, positionally
        self.notices: list[tuple[ConversationRef, str]] = []
        self.reactions: list[tuple[str, str]] = []  # ("+eyes"/"-eyes", message_id)
        self.thread_posts: dict[str, list[PostSnippet]] = {}  # root_id -> thread
        self.recent_posts: dict[str, list[PostSnippet]] = {}  # channel_id -> history
        self.posted_actions: list[tuple] = []  # (ref, text, actions, callback_url)
        self.retracted: list[tuple[str, str]] = []  # (post_id, text)
        self.posted_cards: list[tuple] = []  # (ref, cards, callback_url)
        self.updated: list[tuple] = []  # (post_id, cards) — screen redraws
        self.dialogs: list[tuple] = []  # (trigger_id, form, submit_url, state)
        self.channel_names: dict[str, str] = {}  # channel_id -> display name
        self.posted_files: list[tuple] = []  # (ref, files, text)

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        self.replies.append((ref, text))
        self.reply_hops.append(hop_depth)

    async def post_notice(self, ref: ConversationRef, text: str) -> None:
        self.notices.append((ref, text))

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        self.reactions.append((f"+{name}", ref.message_id))

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        self.reactions.append((f"-{name}", ref.message_id))

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        return UserProfile(username="roman", display_name="Roman")

    async def resolve_channel(self, channel_id: str) -> str:
        return self.channel_names.get(channel_id, "")

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        return self.thread_posts.get(ref.thread_root_id or ref.conversation_id, [])

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return self.recent_posts.get(channel_id, [])[-limit:]

    def format_mention(self, username: str) -> str:
        return f"@{username}"

    async def post_files(
        self, ref: ConversationRef, files: list[OutgoingFile], *, text: str = ""
    ) -> None:
        self.posted_files.append((ref, files, text))

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        self.posted_actions.append((ref, text, actions, callback_url))
        return "widget-post-id"

    async def retract(self, post_id: str, text: str) -> None:
        self.retracted.append((post_id, text))

    async def post_cards(
        self, ref: ConversationRef, cards: list[Card], *, callback_url: str
    ) -> str:
        self.posted_cards.append((ref, cards, callback_url))
        return "widget-post-id"

    async def update_cards(
        self, post_id: str, cards: list[Card], *, callback_url: str
    ) -> None:
        self.updated.append((post_id, cards))

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        self.dialogs.append((trigger_id, form, submit_url, state))
