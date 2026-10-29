"""SlackGateway: owns the Socket Mode connection, normalizes, decides, dispatches.

One gateway = one bot account (its own AsyncApp). Inbound messages become neutral
IncomingMessages fed to the agent's sink; interactive callbacks (button clicks,
modal submits, shortcuts, slash commands) are SlackInteractions' — the same
class an application without agents drives on its own — and reach the
transport-neutral InteractionDispatcher over the WebSocket instead of HTTP.
"""

import logging
from dataclasses import replace

from aiohttp import ClientSession
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from crucible.attachments import AttachmentStore, IncomingFile
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.reactions import ReactionCommands
from crucible.gateways.slack.events import FileHandle, event_to_incoming, parse_files
from crucible.gateways.slack.home import SlackHome
from crucible.gateways.slack.interactions import (
    DEFAULT_COMMAND_SHORTCUT_PREFIX,
    SlackInteractions,
)
from crucible.gateways.slack.reactions import SlackReactions
from crucible.loopguard import LoopGuard
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.gateway import GATEWAY_SLACK, AgentIdentity
from crucible.ports.chat.keying import ConversationKey, PlatformKey
from crucible.ports.chat.types import IncomingMessage

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_COMMAND_SHORTCUT_PREFIX", "SlackGateway"]


class SlackGateway:
    def __init__(
        self,
        app: AsyncApp,
        app_token: str,
        sink: MessageSink,
        chat: ChatClient,
        *,
        agent: str = "",
        poster: ChatClient | None = None,
        dispatcher: GatewayDispatcher | None = None,
        directory: AgentDirectory | None = None,
        loop_guard: LoopGuard | None = None,
        reply_to_agents: bool = True,
        command_prefix: str = DEFAULT_COMMAND_SHORTCUT_PREFIX,
        attachments: AttachmentStore | None = None,
        keying: ConversationKey | None = None,
        reaction_commands: ReactionCommands | None = None,
        home: SlackHome | None = None,
    ) -> None:
        self._app = app
        self._app_token = app_token
        # The application's Home and context handling (None: the events are
        # taken and nothing is shown).
        self._home = home
        self._dispatcher = dispatcher
        # The socket handler lives one connection: slack_sdk's client stays
        # closed once closed, so ``run`` opens a fresh one each time it is
        # asked to — after ``stop`` (a lost lease) the gateway runs again.
        self._handler: AsyncSocketModeHandler | None = None
        self._agent = agent  # our own name; a command names the agent to run
        self._sink = sink
        self._chat = chat
        self._directory = directory
        self._loop_guard = loop_guard
        self._reply_to_agents = reply_to_agents
        self._attachments = attachments
        self._keying: ConversationKey = keying or PlatformKey()  # the conversation a file belongs to
        self._own_user_id = ""
        self._own_bot_id = ""
        # The interactive half, kept apart: clicks, modals, shortcuts and slash
        # commands go to the dispatcher and never near the sink. Without a
        # dispatcher there is nobody to route them to, and they stay unhandled.
        self.interactions: SlackInteractions | None = None
        if dispatcher is not None:
            self.interactions = SlackInteractions(
                app, dispatcher, poster or chat, agent=agent, command_prefix=command_prefix
            )
        # Reactions that are commands: the third door beside the slash command
        # and the shortcut, and like them nothing without a dispatcher.
        self.reactions: SlackReactions | None = None
        if dispatcher is not None and reaction_commands is not None and reaction_commands.by_emoji:
            self.reactions = SlackReactions(
                app, dispatcher, chat, agent=agent, reaction_commands=reaction_commands,
                own_user_id=lambda: self._own_user_id, directory=directory,
            )
        self._register()

    async def login(self) -> AgentIdentity:
        auth = await self._app.client.auth_test()
        self._own_user_id = auth.get("user_id", "")
        self._own_bot_id = auth.get("bot_id", "")
        username = auth.get("user", "")
        logger.info("Slack gateway up: @%s (%s)", username, self._own_user_id)
        return AgentIdentity(user_id=self._own_user_id, username=username)

    async def run(self) -> None:
        self._handler = self._new_handler()
        await self._handler.start_async()  # opens the socket; returns on disconnect

    async def stop(self) -> None:
        handler, self._handler = self._handler, None
        if handler is not None:
            await handler.close_async()

    def _new_handler(self) -> AsyncSocketModeHandler:
        return AsyncSocketModeHandler(self._app, self._app_token)

    # -- registration -------------------------------------------------------

    def _register(self) -> None:
        """What this gateway takes from the app: the ``message`` event, the
        agent panel's events, and — through ``interactions`` and ``reactions``
        — engine widgets (``cruxw…`` actions, the ``crux_form`` view), the
        prefixed shortcuts, every slash command and ``reaction_added``. Bolt
        gives a request to the first listener that matches, in order of
        registration: an application that registers its own listeners on the
        app before handing it over keeps them."""
        self._app.event("message")(self._on_message)
        self._app.event("app_home_opened")(self._on_home_opened)
        self._app.event("app_context_changed")(self._on_context_changed)
        self._app.event("agent_session_stopped")(self._on_session_stopped)
        self._app.event("agent_session_title_changed")(self._on_session_title_changed)
        if self.interactions is not None:
            self.interactions.register()
        if self.reactions is not None:
            self.reactions.register()

    # -- the agent panel -------------------------------------------------------------

    async def _on_home_opened(self, event: dict, client: AsyncWebClient) -> None:
        user_id, tab = str(event.get("user") or ""), str(event.get("tab") or "")
        if self._home is None or not user_id:
            return
        try:
            blocks = await self._home.opened(user_id, tab)
            if blocks:
                await client.views_publish(user_id=user_id, view={"type": "home", "blocks": blocks})
        except Exception:
            logger.exception("Slack Home for %s failed", user_id)

    async def _on_context_changed(self, event: dict) -> None:
        if self._home is None:
            return
        try:
            await self._home.context_changed(str(event.get("user") or ""), dict(event.get("app_context") or {}))
        except Exception:
            logger.exception("Slack context change failed")

    async def _on_session_stopped(self, event: dict) -> None:
        """The person pressed Stop on the agent session: the turn in that
        thread ends as a /stop would end it. Nothing to route it to without a
        dispatcher."""
        thread_ts = str(event.get("thread_ts") or "")
        if self._dispatcher is None or not thread_ts:
            return
        stop = await self._dispatcher.stop_turn(self._agent, thread_ts, gateway=GATEWAY_SLACK)
        logger.info("Slack agent session %s stopped by %s: %s", thread_ts, event.get("user", "?"), stop.name.lower())

    async def _on_session_title_changed(self, event: dict) -> None:
        logger.debug("Slack agent session %s renamed by %s", event.get("thread_ts", "?"), event.get("user", "?"))

    async def _on_message(self, event: dict) -> None:
        try:
            await self._handle_message(event)
        except Exception:
            logger.exception("failed to handle Slack message event")

    # -- inbound messages ---------------------------------------------------

    async def _handle_message(self, event: dict) -> None:
        msg = event_to_incoming(event, self._own_user_id, self._own_bot_id)
        if msg is None:
            return
        decided = self._decide(msg)
        if decided is None:
            return
        decided = await self._with_attachments(decided)
        self._sink.submit(decided, self._chat)

    async def _with_attachments(self, msg: IncomingMessage) -> IncomingMessage:
        """Download whatever the sender attached, so the message carries local
        paths. Only for messages we are actually going to answer."""
        if self._attachments is None:
            return msg
        handles = parse_files(msg.raw)
        if not handles:
            return msg
        fetched: list[IncomingFile] = []
        for handle in handles:
            data = await self._download(handle)
            if data is not None:
                fetched.append(
                    IncomingFile(
                        name=handle.name,
                        data=data,
                        mime=handle.mime,
                        key=handle.file_id,
                    )
                )
        stored = await self._attachments.save_many(
            self._agent, self._keying.key(self._agent, msg), fetched
        )
        return replace(msg, attachments=stored) if stored else msg

    async def _download(self, handle: FileHandle) -> bytes | None:
        """Fetch a private Slack file with the bot token. Slack answers an
        unauthorized request with its HTML sign-in page rather than an error, so
        an HTML body is reported as the missing scope it almost always is."""
        headers = {"Authorization": f"Bearer {self._app.client.token}"}
        try:
            async with ClientSession() as session:
                async with session.get(handle.url, headers=headers) as response:
                    if response.status != 200:
                        logger.warning(
                            "downloading %s failed: HTTP %s", handle.name, response.status
                        )
                        return None
                    if "text/html" in response.headers.get("Content-Type", ""):
                        logger.warning(
                            "downloading %s returned a login page — the app is most "
                            "likely missing the files:read scope",
                            handle.name,
                        )
                        return None
                    return await response.read()
        except Exception:
            logger.warning("could not download %s", handle.name, exc_info=True)
            return None

    def _decide(self, msg: IncomingMessage) -> IncomingMessage | None:
        if msg.is_from_bot:
            return self._decide_agent(msg)
        return msg if (msg.is_dm or msg.mentioned) else None

    def _decide_agent(self, msg: IncomingMessage) -> IncomingMessage | None:
        """Answer ANOTHER of our agents only when explicitly mentioned and the loop
        guard allows it. (Slack messages carry no hop-depth, so cascades are bounded
        by the rate-limit window only.)"""
        if not self._reply_to_agents or self._directory is None:
            return None
        if msg.user_id not in self._directory.agent_user_ids():
            return None
        if not msg.mentioned:
            return None
        if self._loop_guard is not None:
            decision = self._loop_guard.check(
                conversation_id=msg.conversation_id, hop_depth=msg.hop_depth
            )
            if not decision.allow:
                logger.info("loop guard dropped agent turn in %s: %s", msg.conversation_id, decision.reason)
                return None
        return msg
