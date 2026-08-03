"""GatewayFactory: build an agent's chat client + gateway for a configured
transport. All transport-specific construction — the Mattermost driver, the Slack
AsyncApp, which gateway class — lives here, so an application never branches on
transport inline: it resolves a neutral ``GatewayConfig`` from its own settings and
hands it in.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from mattermostautodriver import AsyncTypedDriver
from slack_bolt.async_app import AsyncApp

from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.mattermost import (
    MattermostChatClient,
    MattermostGateway,
    driver_options,
)
from crucible.gateways.slack import PROMPT_HINT as SLACK_PROMPT_HINT
from crucible.gateways.slack import SlackChatClient, SlackGateway
from crucible.gateways.ws import WsChatClient, WsGateway, WsHub
from crucible.loopguard import LoopGuard
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.gateway import Gateway
from crucible.tools.base import CAP_EPHEMERAL

logger = logging.getLogger(__name__)

# Gateway kinds that deliver interactive callbacks over HTTP, so a deployment with
# any such agent needs the interactions receiver. (Slack drives its socket instead.)
_HTTP_CALLBACK_KINDS = frozenset({"mattermost"})


def needs_http_receiver(kind: str) -> bool:
    """Whether a gateway of this kind delivers callbacks over HTTP — lets the app
    decide up front (before building handles) if the HTTP receiver is required."""
    return kind in _HTTP_CALLBACK_KINDS


@dataclass(frozen=True)
class GatewayConfig:
    """The resolved, transport-neutral inputs the factory needs to build one
    agent's gateway. An application maps its own settings onto this (which agent
    runs on which transport, and with which tokens)."""

    kind: str  # "mattermost" | "slack" | "ws"
    reply_to_agents: bool = True
    max_post_chars: int = 16000
    # Mattermost
    mattermost_url: str = ""
    mm_token: str = ""
    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    # ws carries no per-agent secrets: access is authorized by the client
    # services' tokens on the hub, not by the agent.


@dataclass
class GatewayHandle:
    """One agent's live chat presence: its client, its admin client (when the
    transport supports channel administration), a factory that produces the gateway
    once the message sink is built, the text to append to its system prompt (gateway
    formatting rules), and whether its transport needs the HTTP interactions
    receiver (Mattermost delivers callbacks over HTTP; Slack over its socket)."""

    chat: ChatClient
    admin: ChatAdmin | None
    create_gateway: Callable[[MessageSink], Gateway]
    prompt_hint: str = ""
    needs_http_receiver: bool = False
    # Extra capabilities this gateway kind provides beyond what admin/interactivity
    # imply (e.g. CAP_EPHEMERAL on Mattermost/Slack, absent on ws). Unioned into the
    # agent's cap set in ToolWiring.enroll.
    caps: frozenset[str] = frozenset()


class GatewayFactory:
    def __init__(
        self,
        *,
        directory: AgentDirectory,
        loop_guard: LoopGuard,
        dispatcher: GatewayDispatcher | None,
        ws_hub: WsHub | None = None,
    ) -> None:
        self._directory = directory
        self._loop_guard = loop_guard
        self._dispatcher = dispatcher
        self._ws_hub = ws_hub
        self._builders: dict[str, Callable[[str, GatewayConfig], GatewayHandle | None]] = {
            "mattermost": self._mattermost,
            "slack": self._slack,
            "ws": self._ws,
        }

    def create(self, agent: str, config: GatewayConfig) -> GatewayHandle | None:
        """Build the gateway for ``agent`` from ``config``, or None (with a logged
        reason) if the transport kind is unknown. The application resolves tokens
        before calling, so a runnable ``config`` always yields a handle."""
        builder = self._builders.get(config.kind)
        if builder is None:
            logger.warning(
                "agent %s: unknown gateway %r (use 'mattermost', 'slack' or 'ws') — skipping",
                agent, config.kind,
            )
            return None
        return builder(agent, config)

    def _mattermost(self, agent: str, config: GatewayConfig) -> GatewayHandle:
        driver = AsyncTypedDriver(driver_options(config.mattermost_url, config.mm_token))
        chat = MattermostChatClient(driver, max_post_chars=config.max_post_chars)

        def create_gateway(sink: MessageSink) -> Gateway:
            return MattermostGateway(
                driver, sink, chat,
                directory=self._directory, loop_guard=self._loop_guard,
                reply_to_agents=config.reply_to_agents,
            )

        return GatewayHandle(
            chat=chat, admin=chat, create_gateway=create_gateway,
            prompt_hint="", needs_http_receiver=needs_http_receiver(config.kind),
            caps=frozenset({CAP_EPHEMERAL}),
        )

    def _slack(self, agent: str, config: GatewayConfig) -> GatewayHandle:
        app = AsyncApp(token=config.slack_bot_token)
        chat = SlackChatClient(app.client, max_post_chars=config.max_post_chars)

        def create_gateway(sink: MessageSink) -> Gateway:
            return SlackGateway(
                app, config.slack_app_token, sink, chat,
                poster=chat, dispatcher=self._dispatcher,
                directory=self._directory, loop_guard=self._loop_guard,
                reply_to_agents=config.reply_to_agents,
            )

        return GatewayHandle(
            chat=chat, admin=chat, create_gateway=create_gateway,
            prompt_hint=SLACK_PROMPT_HINT, needs_http_receiver=needs_http_receiver(config.kind),
            caps=frozenset({CAP_EPHEMERAL}),
        )

    def _ws(self, agent: str, config: GatewayConfig) -> GatewayHandle | None:
        hub = self._ws_hub
        if hub is None:
            logger.warning(
                "agent %s: gateway 'ws' but the app built no WsHub — skipping", agent
            )
            return None
        chat = WsChatClient(hub, agent)

        def create_gateway(sink: MessageSink) -> Gateway:
            # The hub owns the shared server; the per-agent gateway only carries
            # the lifecycle contract. Registration here (not at handle build)
            # because the sink exists only now.
            hub.register_agent(agent, sink, chat)
            return WsGateway(agent)

        return GatewayHandle(
            chat=chat, admin=None, create_gateway=create_gateway,
            prompt_hint="", needs_http_receiver=False,
        )
