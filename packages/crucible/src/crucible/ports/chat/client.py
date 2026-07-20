"""Outbound port: how a flow talks back to the user, regardless of platform."""

from typing import Protocol

from crucible.ports.chat.types import ConversationRef, PostSnippet, UserProfile


class ChatClient(Protocol):
    """Two posting verbs by design:
    - ``post_reply``  — a real answer from the agent (rich rendering applies);
    - ``post_notice`` — verbatim status/system text (fallback message), as-is.

    Reactions are an optional capability — adapters on platforms without them
    may no-op. ``format_mention`` builds platform mention markup (sync, pure).
    """

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        """Post an agent reply. ``hop_depth`` travels with the message (agent-to-
        agent loop accounting); adapters stamp it where other agents can read it."""
        ...

    async def post_notice(self, ref: ConversationRef, text: str) -> None: ...

    async def add_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def get_user_profile(self, user_id: str) -> UserProfile | None: ...

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        """The whole thread ``ref`` belongs to, oldest first (context backfill).
        Takes the full ref so adapters that address a thread by (channel, root)
        — not by a globally-unique root id — can fetch it. [] if unavailable."""
        ...

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        """Recent channel messages, oldest first (channel-session backfill)."""
        ...

    def format_mention(self, username: str) -> str: ...
