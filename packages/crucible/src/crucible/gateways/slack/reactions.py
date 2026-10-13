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
    quoted_message,
    reacted_by_an_agent,
)
from crucible.gateways.slack.events import (
    EVENT_REACTION_ADDED,
    conversation_of,
    is_direct_channel,
    parse_reaction,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory

logger = logging.getLogger(__name__)


class SlackReactions:
    """The reactions of one Slack app that are commands."""

    def __init__(
        self,
        app: AsyncApp,
        dispatcher: GatewayDispatcher,
        chat: ChatClient,
        *,
        agent: str,
        reaction_commands: ReactionCommands,
        own_user_id: Callable[[], str],
        directory: AgentDirectory | None = None,
    ) -> None:
        self._app = app
        self._dispatcher = dispatcher
        self._chat = chat
        self._agent = agent
        self._reaction_commands = reaction_commands
        # Learned at login, after this is built — so asked for, not copied.
        self._own_user_id = own_user_id
        self._directory = directory

    def register(self) -> None:
        self._app.event(EVENT_REACTION_ADDED)(self._on_reaction)

    async def _on_reaction(self, event: dict) -> None:
        try:
            reaction = parse_reaction(event)
            if reaction is not None:
                await self.handle_reaction(reaction)
        except Exception:
            logger.exception("failed to handle Slack reaction")

    async def handle_reaction(self, reaction: Reaction) -> None:
        command = self._reaction_commands.by_emoji.get(reaction.emoji)
        if command is None:
            return
        reactor = await self._chat.get_user_profile(reaction.user_id)
        if reacted_by_an_agent(reaction.user_id, self._own_user_id(), self._directory, reactor):
            return
        message = await self._message(reaction)
        conversation_id, kind, _ = conversation_of(
            reaction.message_id, str(message.get("thread_ts") or ""), reaction.channel_id,
            is_dm=is_direct_channel(reaction.channel_id),
        )
        quoted = None
        text = str(message.get("text") or "")
        if text:
            quoted = quoted_message(
                await self._author(message), text, self._reaction_commands.quote_limit
            )
        self._dispatcher.invoke_command(
            self._agent,
            channel_id=reaction.channel_id,
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

    async def _author(self, message: dict[str, Any]) -> str:
        """Who wrote the reacted message, by username when Slack will say, else
        by the id the message carries — never nobody, since the quote is
        presented as this person's words."""
        user_id = str(message.get("user") or message.get("bot_id") or "")
        profile = await self._chat.get_user_profile(user_id) if user_id else None
        return (profile.username if profile is not None else "") or user_id

    async def _message(self, reaction: Reaction) -> dict[str, Any]:
        """The message the reaction was put on — its text, its author and, when
        it is a reply, its thread — or {} when Slack will not say: the command
        then runs without the quote, in the conversation the event alone names.
        Only the message with the reacted ``ts`` counts: an answer that is the
        thread without it is no quote at all, not the parent's words."""
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
        for message in response.get("messages") or []:
            if isinstance(message, dict) and message.get("ts") == reaction.message_id:
                return message
        return {}
