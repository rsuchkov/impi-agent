"""WsChatClient: the ChatClient over a WsHub, for one agent.

Replies and notices become frames on the owning service's socket (or its
reconnect buffer). Text goes out as-is — Markdown by default; how it renders
is the client service's business. Everything platform-flavored is absent by
design: no reactions, no history backfill, no widgets (agents on this gateway
simply don't declare the ask_user_*/open_form tools)."""

import logging

from crucible.ports.chat.types import (
    Action,
    ConversationRef,
    Form,
    PostSnippet,
    UserProfile,
)

logger = logging.getLogger(__name__)


class WsChatClient:
    def __init__(self, hub, agent: str) -> None:
        self._hub = hub
        self._agent = agent

    async def post_reply(self, ref: ConversationRef, text: str, *, hop_depth: int = 0) -> None:
        # hop_depth is not propagated: frames carry no engine metadata a peer
        # agent could read; the LoopGuard rate window is the loop bound here.
        await self._hub.send(self._agent, ref.conversation_id, "reply", text)

    async def post_notice(self, ref: ConversationRef, text: str) -> None:
        await self._hub.send(self._agent, ref.conversation_id, "notice", text)

    async def add_reaction(self, ref: ConversationRef, name: str) -> None:
        pass  # no reaction concept on this transport

    async def remove_reaction(self, ref: ConversationRef, name: str) -> None:
        pass

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        return None

    async def get_thread_posts(self, ref: ConversationRef) -> list[PostSnippet]:
        return []  # the engine's own session memory is the only history

    async def get_recent_posts(self, channel_id: str, limit: int = 20) -> list[PostSnippet]:
        return []

    def format_mention(self, username: str) -> str:
        return f"@{username}"

    # -- Interactive widgets: not part of the ws protocol (v1) -----------------

    async def post_actions(
        self, ref: ConversationRef, text: str, actions: list[Action], *, callback_url: str
    ) -> str:
        logger.warning(
            "ws agent %s asked to post a widget — unsupported on this gateway; "
            "sending the prompt as plain text", self._agent,
        )
        await self._hub.send(self._agent, ref.conversation_id, "notice", text)
        return ""

    async def retract(self, post_id: str, text: str) -> None:
        pass

    async def open_dialog(
        self, trigger_id: str, form: Form, *, submit_url: str, state: str
    ) -> None:
        logger.warning(
            "ws agent %s asked to open a form — unsupported on this gateway", self._agent
        )
