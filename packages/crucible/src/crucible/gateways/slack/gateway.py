"""SlackGateway: owns the Socket Mode connection, normalizes, decides, dispatches.

One gateway = one bot account (its own AsyncApp). Inbound messages become neutral
IncomingMessages fed to the agent's sink; interactive callbacks (button clicks,
modal submits) are routed to the transport-neutral InteractionDispatcher — the
same brain the Mattermost HTTP receiver uses, here over the WebSocket instead.
"""

import logging
import re

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.slack.events import event_to_incoming
from crucible.gateways.slack.rendering import (
    FORM_CALLBACK,
    WIDGET_ACTION_PREFIX,
    decode_action,
    extract_submission,
    picked_kind,
)
from crucible.loopguard import LoopGuard
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.gateway import AgentIdentity
from crucible.ports.chat.types import KIND_DM, KIND_THREAD, IncomingMessage

logger = logging.getLogger(__name__)

# Shown on the clicked message once its buttons are stripped. Slack doesn't retire
# interactive elements via the ack (unlike Mattermost's callback response), so the
# gateway updates the message itself.
_CHOSE_PREFIX = "Selected: "
_FORM_OPENED = "📝 Opening form…"

# Default prefix for message shortcuts an agent answers as commands: the callback
# id starts with it and the rest is the command name (crux_summarize ->
# "summarize"). Slack forbids custom slash commands inside threads, so a shortcut
# is the thread-aware entry. Configurable per deployment (SLACK_COMMAND_PREFIX) —
# a workspace may already have its own naming convention.
DEFAULT_COMMAND_SHORTCUT_PREFIX = "crux_"


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
    ) -> None:
        self._app = app
        self._handler = AsyncSocketModeHandler(app, app_token)
        self._agent = agent  # our own name; a command names the agent to run
        # Which message shortcuts are commands, and where the command name starts.
        # Empty = every shortcut is a command and its callback id IS the name.
        self._command_prefix = command_prefix
        self._sink = sink
        self._chat = chat
        self._poster = poster  # opens modals on a form-open click (same agent)
        self._dispatcher = dispatcher
        self._directory = directory
        self._loop_guard = loop_guard
        self._reply_to_agents = reply_to_agents
        self._own_user_id = ""
        self._own_bot_id = ""
        self._register()

    async def login(self) -> AgentIdentity:
        auth = await self._app.client.auth_test()
        self._own_user_id = auth.get("user_id", "")
        self._own_bot_id = auth.get("bot_id", "")
        username = auth.get("user", "")
        logger.info("Slack gateway up: @%s (%s)", username, self._own_user_id)
        return AgentIdentity(user_id=self._own_user_id, username=username)

    async def run(self) -> None:
        await self._handler.start_async()  # opens the socket; returns on disconnect

    async def stop(self) -> None:
        await self._handler.close_async()

    # -- registration -------------------------------------------------------

    def _register(self) -> None:
        self._app.event("message")(self._on_message)
        # One handler for every engine widget (action ids share a prefix).
        self._app.action(re.compile(f"^{WIDGET_ACTION_PREFIX}"))(self._on_action)
        self._app.view(FORM_CALLBACK)(self._on_view)
        # Message shortcuts are the thread-aware command entry (Slack forbids
        # custom slash commands in threads); one handler for the whole family.
        # escape(): the prefix is configuration, not a pattern.
        self._app.shortcut(re.compile(f"^{re.escape(self._command_prefix)}"))(self._on_shortcut)

    async def _on_message(self, event: dict) -> None:
        try:
            await self._handle_message(event)
        except Exception:
            logger.exception("failed to handle Slack message event")

    async def _on_action(self, ack, body: dict) -> None:
        await ack()
        try:
            await self._handle_action(body)
        except Exception:
            logger.exception("failed to handle Slack block action")

    async def _on_view(self, ack, body: dict) -> None:
        await ack()
        try:
            await self._handle_view(body)
        except Exception:
            logger.exception("failed to handle Slack view submission")

    async def _on_shortcut(self, ack, body: dict) -> None:
        await ack()  # Slack demands an ack within 3s; the turn runs after it
        try:
            self._handle_shortcut(body)
        except Exception:
            logger.exception("failed to handle Slack shortcut")

    # -- inbound messages ---------------------------------------------------

    async def _handle_message(self, event: dict) -> None:
        msg = event_to_incoming(event, self._own_user_id, self._own_bot_id)
        if msg is None:
            return
        decided = self._decide(msg)
        if decided is None:
            return
        self._sink.submit(decided, self._chat)

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

    # -- interactive callbacks (routed to the neutral dispatcher) -----------

    async def _handle_action(self, body: dict) -> None:
        if self._dispatcher is None:
            return
        actions = body.get("actions") or []
        if not actions:
            return
        token, form_token, value = decode_action(actions[0])
        user_id = (body.get("user") or {}).get("id", "")
        if form_token:
            if await self._open_modal(body, form_token):
                await self._strip_buttons(body, _FORM_OPENED)
            return
        if not self._dispatcher.resolve_pending(token, value):
            # Slack names the element that fired, so a picker's id is resolvable.
            await self._dispatcher.consume_action(
                token, value, user_id, pick=picked_kind(actions[0])
            )
        # Slack won't retire the buttons on its own — strip them off the message so a
        # fire-and-forget widget can't be clicked twice.
        await self._strip_buttons(body, f"{_CHOSE_PREFIX}{value}")

    def _handle_shortcut(self, body: dict) -> None:
        """A message shortcut runs a command in the conversation of the message it
        was invoked on — the thread if there is one, else the message itself (which
        is what a reply would start). The callback id names the command."""
        if self._dispatcher is None:
            return
        command = str(body.get("callback_id", "")).removeprefix(self._command_prefix)
        if not command:
            return
        message = body.get("message") or {}
        channel_id = (body.get("channel") or {}).get("id", "")
        user = body.get("user") or {}
        ts = str(message.get("ts") or "")
        thread_ts = str(message.get("thread_ts") or "")
        # Same conversation rule as an inbound message (slack/events.py): the
        # thread wins; a DM without a thread is the DM-channel session.
        if thread_ts and thread_ts != ts:
            conversation_id, kind = thread_ts, KIND_THREAD
        elif channel_id.startswith("D"):  # the shortcut payload carries no channel type
            conversation_id, kind = channel_id, KIND_DM
        else:
            conversation_id, kind = ts, KIND_THREAD
        if not conversation_id:
            logger.warning("shortcut %s: no conversation in the payload", command)
            return
        self._dispatcher.invoke_command(
            self._agent,
            channel_id=channel_id,
            conversation_id=conversation_id,
            kind=kind,
            text=f"/{command}",
            user_id=user.get("id", ""),
            username=user.get("username", "") or user.get("name", ""),
        )

    async def _open_modal(self, body: dict, form_token: str) -> bool:
        if self._dispatcher is None or self._poster is None:
            return False
        form = await self._dispatcher.load_form(form_token)
        if form is None:
            return False
        trigger = body.get("trigger_id", "")
        if not trigger:
            return False
        try:
            await self._poster.open_dialog(trigger, form.form, submit_url="", state=form_token)
        except Exception:
            logger.exception("failed to open Slack modal for form %s", form_token[:8])
            return False
        return True

    async def _strip_buttons(self, body: dict, text: str) -> None:
        """Best-effort: replace the clicked message's text and drop its interactive
        blocks, so a widget can't be clicked again."""
        channel = (body.get("channel") or {}).get("id", "")
        ts = (body.get("message") or {}).get("ts", "")
        if not (channel and ts):
            return
        try:
            await self._app.client.chat_update(channel=channel, ts=ts, text=text, blocks=[])
        except Exception:
            logger.debug("could not strip buttons off %s/%s", channel, ts, exc_info=True)

    async def _handle_view(self, body: dict) -> None:
        if self._dispatcher is None:
            return
        view = body.get("view") or {}
        state = view.get("private_metadata", "")
        submission = extract_submission(view.get("state") or {})
        user_id = (body.get("user") or {}).get("id", "")
        await self._dispatcher.submit_form(state, submission, cancelled=False, user_id=user_id)
