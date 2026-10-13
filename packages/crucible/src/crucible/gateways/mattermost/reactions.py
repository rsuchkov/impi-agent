"""MattermostReactions: an emoji on a post as a command, over the WebSocket.

The Mattermost half of what ``gateways/reactions.py`` describes: a
``reaction_added`` frame whose emoji the agent's map names runs the mapped
command in the conversation of the post under it, through the same
``invoke_command`` a slash command uses. The frame names the post by id only,
so the post is read back first — for its text, its thread and, when it has no
thread, whether its channel is a direct message.
"""

import logging

from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.reactions import (
    Reaction,
    ReactionCommands,
    command_text,
    reacted_by_an_agent,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.types import KIND_DM, KIND_THREAD

logger = logging.getLogger(__name__)

_DIRECT_CHANNEL_TYPE = "D"


class MattermostReactions:
    """The reactions of one Mattermost bot that are commands."""

    def __init__(
        self,
        driver: AsyncTypedDriver,
        dispatcher: GatewayDispatcher,
        chat: ChatClient,
        *,
        agent: str,
        commands: ReactionCommands,
        directory: AgentDirectory | None = None,
    ) -> None:
        self._driver = driver
        self._dispatcher = dispatcher
        self._chat = chat
        self._agent = agent
        self._commands = commands
        self._directory = directory
        # A channel's type never changes, and a reaction storm must not become
        # a lookup storm.
        self._channel_types: dict[str, str] = {}

    async def handle_reaction(self, reaction: Reaction, own_user_id: str) -> None:
        command = self._commands.commands.get(reaction.emoji)
        if command is None:
            return
        if reacted_by_an_agent(reaction.user_id, own_user_id, self._directory):
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
        # The conversation rule of an inbound post: the thread wins; a
        # top-level post in a direct message is the DM-channel session; a
        # top-level channel post gets its answer in a thread under it.
        if root_id:
            conversation_id, kind = root_id, KIND_THREAD
        elif await self._channel_type(channel_id) == _DIRECT_CHANNEL_TYPE:
            conversation_id, kind = channel_id, KIND_DM
        else:
            conversation_id, kind = reaction.message_id, KIND_THREAD
        profile = await self._chat.get_user_profile(reaction.user_id)
        self._dispatcher.invoke_command(
            self._agent,
            channel_id=channel_id,
            conversation_id=conversation_id,
            kind=kind,
            text=command_text(command, str(post.get("message") or ""), self._commands.quote_limit),
            user_id=reaction.user_id,
            username=profile.username if profile is not None else "",
        )
        logger.info(
            "reaction :%s: by %s in %s runs /%s for %s",
            reaction.emoji, reaction.user_id, conversation_id, command, self._agent,
        )

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
