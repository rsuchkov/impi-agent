"""In-process fake of the ChatClient port."""

from crucible.ports.chat.types import ConversationRef, PostSnippet, UserProfile


class FakeChat:
    def __init__(self) -> None:
        self.replies: list[tuple[ConversationRef, str]] = []
        self.reply_hops: list[int] = []  # hop_depth of each reply, positionally
        self.notices: list[tuple[ConversationRef, str]] = []
        self.reactions: list[tuple[str, str]] = []  # ("+eyes"/"-eyes", message_id)
        self.thread_posts: dict[str, list[PostSnippet]] = {}  # root_id -> thread
        self.recent_posts: dict[str, list[PostSnippet]] = {}  # channel_id -> history

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

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        return self.thread_posts.get(ref.thread_root_id or ref.conversation_id, [])

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return self.recent_posts.get(channel_id, [])[-limit:]

    def format_mention(self, username: str) -> str:
        return f"@{username}"
