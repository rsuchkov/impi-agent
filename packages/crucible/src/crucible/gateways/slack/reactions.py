"""SlackReactions: an emoji on a message as a command, over the socket.

The third door to a command on Slack, beside the slash command and the message
shortcut: a ``reaction_added`` event whose emoji the agent's map names runs the
mapped command in the conversation of the message under it, through the same
``invoke_command`` a shortcut uses. The event carries neither the message's
text nor its thread, so the message is read back first (one call); without
the scope for that the command still runs, pointed at the thread alone.
"""

import logging
from collections.abc import Callable
from typing import Any

from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.reactions import (
    Reaction,
    ReactionCommands,
    command_text,
    emoji_name,
    reacted_by_an_agent,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.types import KIND_DM, KIND_THREAD

logger = logging.getLogger(__name__)

EVENT_REACTION_ADDED = "reaction_added"


def parse_reaction(event: dict[str, Any]) -> Reaction | None:
    """Normalize one ``reaction_added`` event; None = not a reaction on a
    message (a file, a comment), or one the event does not name fully."""
    item = event.get("item") or {}
    if item.get("type") != "message":
        return None
    emoji = emoji_name(str(event.get("reaction") or ""))
    user_id = str(event.get("user") or "")
    channel = str(item.get("channel") or "")
    ts = str(item.get("ts") or "")
    if not (emoji and user_id and channel and ts):
        return None
    return Reaction(emoji=emoji, user_id=user_id, channel_id=channel, message_id=ts)


class SlackReactions:
    """The reactions of one Slack app that are commands."""

    def __init__(
        self,
        app: AsyncApp,
        dispatcher: GatewayDispatcher,
        chat: ChatClient,
        *,
        agent: str,
        commands: ReactionCommands,
        own_user_id: Callable[[], str],
        directory: AgentDirectory | None = None,
    ) -> None:
        self._app = app
        self._dispatcher = dispatcher
        self._chat = chat
        self._agent = agent
        self._commands = commands
        # Learned at login, after this is built — so asked for, not copied.
        self._own_user_id = own_user_id
        self._directory = directory

    def register(self) -> None:
        self._app.event(EVENT_REACTION_ADDED)(self._on_reaction)

    async def _on_reaction(self, event: dict) -> None:
        try:
            await self.handle_reaction(event)
        except Exception:
            logger.exception("failed to handle Slack reaction")

    async def handle_reaction(self, event: dict[str, Any]) -> None:
        reaction = parse_reaction(event)
        if reaction is None:
            return
        command = self._commands.commands.get(reaction.emoji)
        if command is None:
            return
        if reacted_by_an_agent(reaction.user_id, self._own_user_id(), self._directory):
            return
        message = await self._message(reaction)
        ts = reaction.message_id
        thread_ts = str(message.get("thread_ts") or "")
        # The conversation rule of an inbound message and of a shortcut: the
        # thread wins; a DM without one is the DM-channel session; a top-level
        # channel message gets its answer in a thread under it.
        if thread_ts and thread_ts != ts:
            conversation_id, kind = thread_ts, KIND_THREAD
        elif reaction.channel_id.startswith("D"):
            conversation_id, kind = reaction.channel_id, KIND_DM
        else:
            conversation_id, kind = ts, KIND_THREAD
        profile = await self._chat.get_user_profile(reaction.user_id)
        self._dispatcher.invoke_command(
            self._agent,
            channel_id=reaction.channel_id,
            conversation_id=conversation_id,
            kind=kind,
            text=command_text(command, str(message.get("text") or ""), self._commands.quote_limit),
            user_id=reaction.user_id,
            username=profile.username if profile is not None else "",
        )
        logger.info(
            "reaction :%s: by %s in %s runs /%s for %s",
            reaction.emoji, reaction.user_id, conversation_id, command, self._agent,
        )

    async def _message(self, reaction: Reaction) -> dict[str, Any]:
        """The message the reaction was put on — its text and, when it is a
        reply, its thread — or {} when Slack will not say: the command then
        runs without the quote, in the conversation the event alone names."""
        try:
            response = await self._app.client.conversations_replies(
                channel=reaction.channel_id, ts=reaction.message_id,
                oldest=reaction.message_id, latest=reaction.message_id,
                inclusive=True, limit=1,
            )
        except SlackApiError as exc:
            error = str((exc.response or {}).get("error", "")) if exc.response else ""
            logger.warning(
                "could not read the message under a reaction in %s: %s%s",
                reaction.channel_id, error or exc,
                " — the app is most likely missing a *:history scope" if error == "missing_scope" else "",
            )
            return {}
        messages = response.get("messages") or []
        for message in messages:
            if isinstance(message, dict) and message.get("ts") == reaction.message_id:
                return message
        return messages[0] if messages and isinstance(messages[0], dict) else {}
