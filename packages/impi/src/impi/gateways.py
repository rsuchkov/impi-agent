"""GatewayFactory: build an agent's chat client + gateway for its configured
transport (settings.gateway_for). All transport-specific construction — the
Mattermost driver, the Slack AsyncApp, which gateway class — lives here, so the
composition root never branches on transport inline.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mattermostautodriver import AsyncTypedDriver
from slack_bolt.async_app import AsyncApp

from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.gateway import Gateway
from crucible.ports.chat.widgets import WidgetPoster
from crucible.gateways.mattermost import (
    MattermostChatClient,
    MattermostGateway,
    driver_options,
)
from crucible.gateways.slack import PROMPT_HINT as SLACK_PROMPT_HINT
from crucible.gateways.slack import SlackChatClient, SlackGateway
from crucible.interactions import InteractionDispatcher
from crucible.loopguard import LoopGuard
from impi.config import ImpiSettings

logger = logging.getLogger(__name__)


class ChatClientPoster(ChatClient, WidgetPoster, Protocol):
    """What a gateway's client is: a ChatClient (replies) that also posts
    interactive widgets (WidgetPoster)."""


@dataclass
class GatewayHandle:
    """One agent's live chat presence: its client, its admin client (when the
    transport supports channel administration), a factory that produces the gateway
    once the message sink is built, the text to append to its system prompt (gateway
    formatting rules), and whether its transport needs the HTTP interactions
    receiver (Mattermost delivers callbacks over HTTP; Slack over its socket)."""

    chat: ChatClientPoster
    admin: ChatAdmin | None
    create_gateway: Callable[[MessageSink], Gateway]
    prompt_hint: str = ""
    needs_http_receiver: bool = False


class GatewayFactory:
    def __init__(
        self,
        settings: ImpiSettings,
        *,
        directory: AgentDirectory,
        loop_guard: LoopGuard,
        dispatcher: InteractionDispatcher | None,
    ) -> None:
        self._settings = settings
        self._directory = directory
        self._loop_guard = loop_guard
        self._dispatcher = dispatcher
        self._builders: dict[str, Callable[[str], GatewayHandle | None]] = {
            "mattermost": self._mattermost,
            "slack": self._slack,
        }

    def create(self, agent: str) -> GatewayHandle | None:
        """Build the gateway for ``agent`` per its configured transport, or None
        (with a logged reason) if the transport is unknown or its tokens are unset."""
        kind = self._settings.gateway_for(agent)
        builder = self._builders.get(kind)
        if builder is None:
            logger.warning(
                "agent %s: unknown gateway %r (use 'mattermost' or 'slack') — skipping",
                agent, kind,
            )
            return None
        return builder(agent)

    def _mattermost(self, agent: str) -> GatewayHandle | None:
        token = self._settings.mm_token_for(agent)
        if not token:
            logger.warning(
                "agent %s: gateway=mattermost but no token (set AGENTS_MM_TOKEN__%s) — skipping",
                agent, _key(agent),
            )
            return None
        driver = AsyncTypedDriver(driver_options(self._settings.mattermost_url, token))
        chat = MattermostChatClient(driver, max_post_chars=self._settings.mm_max_post_chars)

        def create_gateway(sink: MessageSink) -> Gateway:
            return MattermostGateway(
                driver, sink, chat,
                directory=self._directory, loop_guard=self._loop_guard,
                reply_to_agents=self._settings.agents_reply_to_agents,
            )

        return GatewayHandle(
            chat=chat, admin=chat, create_gateway=create_gateway,
            prompt_hint="", needs_http_receiver=True,
        )

    def _slack(self, agent: str) -> GatewayHandle | None:
        bot, app_token = self._settings.slack_tokens_for(agent)
        if not (bot and app_token):
            logger.warning(
                "agent %s: gateway=slack but tokens missing "
                "(set AGENTS_SLACK_BOT_TOKEN__%s / AGENTS_SLACK_APP_TOKEN__%s) — skipping",
                agent, _key(agent), _key(agent),
            )
            return None
        app = AsyncApp(token=bot)
        chat = SlackChatClient(app.client, max_post_chars=self._settings.mm_max_post_chars)

        def create_gateway(sink: MessageSink) -> Gateway:
            return SlackGateway(
                app, app_token, sink, chat,
                poster=chat, dispatcher=self._dispatcher,
                directory=self._directory, loop_guard=self._loop_guard,
                reply_to_agents=self._settings.agents_reply_to_agents,
            )

        return GatewayHandle(
            chat=chat, admin=chat, create_gateway=create_gateway,
            prompt_hint=SLACK_PROMPT_HINT, needs_http_receiver=False,
        )


def _key(agent: str) -> str:
    return agent.upper().replace("-", "_")
