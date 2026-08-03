"""ChatAdmin port: platform-neutral channel administration used by tools.

Separate from ChatClient (posting) on purpose — a flow posts, a tool
administers. A gateway adapter implements both; tools depend only on this port.
Each agent gets its OWN ChatAdmin (its own account/token), so an action taken
by a tool is attributed to the agent that called it.
"""

from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.types import PostSnippet


@dataclass(frozen=True)
class ChannelMember:
    user_id: str
    username: str


class ChatAdmin(Protocol):
    async def create_channel(
        self, name: str, display_name: str, *, private: bool = True, purpose: str = ""
    ) -> str:
        """Create a channel and return its id. ``name`` is sanitized to the
        platform's slug rules by the adapter."""
        ...

    async def invite_to_channel(self, channel_id: str, user_id: str) -> None: ...

    async def get_channel_members(self, channel_id: str) -> list[ChannelMember]: ...

    async def resolve_username(self, username: str) -> str | None:
        """Platform username -> user id, or None if unknown."""
        ...

    async def post_message(self, channel_id: str, message: str, *, hop_depth: int = 0) -> str:
        """Post a message to a channel as this agent (proactive, top-level — not a
        reply to a specific post) and return the new post id. ``hop_depth`` stamps
        the agent-cascade counter so loop protection keeps counting a chain this
        message starts."""
        ...

    async def get_channel_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        """The channel's most recent posts, chronological (oldest first)."""
        ...

    async def post_ephemeral(self, channel_id: str, user_id: str, message: str) -> None:
        """Post a message in ``channel_id`` visible ONLY to ``user_id`` (an
        ephemeral post). Platform-gated: only gateways that advertise the
        ephemeral capability offer it (Mattermost, Slack; not every platform)."""
        ...
