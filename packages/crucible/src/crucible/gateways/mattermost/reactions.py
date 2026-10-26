"""MattermostReactions: an emoji on a post as a command, over the WebSocket.

The Mattermost half of what ``gateways/reactions.py`` describes: a
``reaction_added`` frame whose emoji the agent's map names runs the mapped
command in the conversation of the post under it, through the same
``invoke_command`` a slash command uses. The frame names the post by id only,
so the post is read back first — for its text, its author, its thread and, when
it has no thread, whether its channel is a direct message.
"""

import logging
from collections.abc import Callable

from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.mattermost.events import DIRECT_CHANNEL_TYPE, conversation_of
from crucible.gateways.reactions import (
    Reaction,
    ReactionCommands,
    quoted_message,
    reacted_by_an_agent,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.gateway import GATEWAY_MATTERMOST

logger = logging.getLogger(__name__)


class MattermostReactions:
    """The reactions of one Mattermost bot that are commands."""

    def __init__(
        self,
        driver: AsyncTypedDriver,
        dispatcher: GatewayDispatcher,
        chat: ChatClient,
        *,
        agent: str,
        reaction_commands: ReactionCommands,
        own_user_id: Callable[[], str],
        directory: AgentDirectory | None = None,
    ) -> None:
        self._driver = driver
        self._dispatcher = dispatcher
        self._chat = chat
        self._agent = agent
        self._reaction_commands = reaction_commands
        # Learned at login, after this is built — so asked for, not copied.
        self._own_user_id = own_user_id
        self._directory = directory
        # A channel's type never changes, and a reaction storm must not become
        # a lookup storm.
        self._channel_types: dict[str, str] = {}

    async def handle_reaction(self, reaction: Reaction) -> None:
        command = self._reaction_commands.by_emoji.get(reaction.emoji)
        if command is None:
            return
        reactor = await self._chat.get_user_profile(reaction.user_id)
        if reacted_by_an_agent(reaction.user_id, self._own_user_id(), self._directory, reactor):
            return
        try:
            post = await self._driver.posts.get_post(reaction.message_id)
        except Exception:
            logger.warning("could not read post %s under a reaction", reaction.message_id, exc_info=True)
            return
        if not isinstance(post, dict):
            return
        channel_id = str(post.get("channel_id") or reaction.channel_id)
        root_id = str(post.get("root_id") or "")
        # A reply's conversation is its thread whatever the channel; whether
        # the channel is a direct one still says who reads the thread.
        is_dm = await self._channel_type(channel_id) == DIRECT_CHANNEL_TYPE
        conversation_id, kind, _ = conversation_of(reaction.message_id, root_id, channel_id, is_dm=is_dm)
        quoted = None
        text = str(post.get("message") or "")
        if text:
            quoted = quoted_message(
                await self._author(str(post.get("user_id") or "")), text,
                self._reaction_commands.quote_limit,
            )
        self._dispatcher.invoke_command(
            self._agent,
            gateway=GATEWAY_MATTERMOST,
            channel_id=channel_id,
            conversation_id=conversation_id,
            kind=kind,
            text=f"/{command}",
            user_id=reaction.user_id,
            username=reactor.username if reactor is not None else "",
            quoted=quoted,
        )
        logger.info(
            "reaction :%s: by %s in %s runs /%s for %s",
            reaction.emoji, reaction.user_id, conversation_id, command, self._agent,
        )

    async def _author(self, user_id: str) -> str:
        """Who wrote the reacted post, by username when Mattermost will say,
        else by id — never nobody, since the quote is presented as this
        person's words."""
        profile = await self._chat.get_user_profile(user_id) if user_id else None
        return (profile.username if profile is not None else "") or user_id

    async def _channel_type(self, channel_id: str) -> str:
        known = self._channel_types.get(channel_id)
        if known is not None:
            return known
        try:
            channel = await self._driver.channels.get_channel(channel_id)
            kind = str(channel.get("type") or "") if isinstance(channel, dict) else ""
        except Exception:
            logger.warning("get_channel %s failed", channel_id, exc_info=True)
            kind = ""
        if kind:
            self._channel_types[channel_id] = kind
        return kind
