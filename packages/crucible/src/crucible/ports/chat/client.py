"""Outbound port: how an agent talks to the platform (replies, reactions, and
interactive widgets), regardless of platform."""

from typing import Protocol

from crucible.ports.chat.types import (
    Action,
    ConversationRef,
    Form,
    PostSnippet,
    UserProfile,
)


class ChatClient(Protocol):
    """The agent's outbound platform surface. Posting verbs:
    - ``post_reply``  — a real answer from the agent (rich rendering applies);
    - ``post_notice`` — verbatim status/system text (fallback message), as-is.

    Reactions are an optional capability — adapters on platforms without them
    may no-op. ``format_mention`` builds platform mention markup (sync, pure).
    The widget verbs (``post_actions``/``retract``/``open_dialog``) post clickable
    affordances whose clicks call back to the engine.
    """

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        """Post an agent reply. ``hop_depth`` travels with the message (agent-to-
        agent loop accounting); adapters stamp it where other agents can read it."""
        ...

    async def post_notice(self, ref: ConversationRef, text: str) -> None: ...

    async def add_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None: ...

    async def get_user_profile(self, user_id: str) -> UserProfile | None: ...

    async def resolve_channel(self, channel_id: str) -> str:
        """Channel id -> its display name, "" if unknown. The counterpart of
        ``get_user_profile`` for channels: a channel picker returns an id, and the
        engine shows the agent a name. Adapters without channels return ""."""
        ...

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        """The whole thread ``ref`` belongs to, oldest first (context backfill).
        Takes the full ref so adapters that address a thread by (channel, root)
        — not by a globally-unique root id — can fetch it. [] if unavailable."""
        ...

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        """Recent channel messages, oldest first (channel-session backfill)."""
        ...

    def format_mention(self, username: str) -> str: ...

    # -- Interactive widgets ------------------------------------------------

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        """Post ``text`` with clickable ``actions`` as the agent. Each action, when
        clicked, makes the platform POST to ``callback_url`` with the action's
        ``context``. Returns the posted message id."""
        ...

    async def retract(self, post_id: str, text: str) -> None:
        """Replace a previously-posted widget's text and drop its buttons — used
        when a blocking request expires or is cancelled, so no stale button can be
        clicked afterwards. Best-effort (a failure must not break the turn)."""
        ...

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        """Open ``form`` as a modal for the user whose click produced ``trigger_id``
        (short-lived — call synchronously in the click handler). On submit the
        platform POSTs to ``submit_url`` with ``state`` echoed back."""
        ...
