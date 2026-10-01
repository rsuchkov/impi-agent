"""The interactive half of a Slack app: clicks, modal submissions, message
shortcuts and slash commands, delivered over Socket Mode and routed to the
neutral InteractionDispatcher — the same brain the Mattermost HTTP receiver
feeds, here over the WebSocket the app already holds.

On its own, deliberately. A gateway composes this beside its message handling;
an application that runs no agents (the secret broker) uses this alone, and
must not be handed a sink, a directory or a loop guard it has no use for.
"""

import logging
import re
from collections.abc import Awaitable, Callable

from slack_bolt.async_app import AsyncApp

from crucible.approvals import ApprovalOutcome
from crucible.gateways.dispatch import GatewayDispatcher
from crucible.gateways.slack.client import MESSAGE_ID_SEP
from crucible.gateways.slack.rendering import (
    FORM_CALLBACK,
    WIDGET_ACTION_PREFIX,
    decode_action,
    decode_approval,
    decode_screen,
    extract_submission,
    picked_kind,
)
from crucible.interactions.results import ActionResult
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.types import (
    AGENT_UNAVAILABLE_TEXT,
    BUTTONS_RETIRED_TEXT,
    COMMAND_ACK_TEXT,
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    NOT_AN_APPROVER_TEXT,
)

logger = logging.getLogger(__name__)

# Shown on the clicked message once its buttons are stripped. Slack doesn't retire
# interactive elements via the ack (unlike Mattermost's callback response), so the
# gateway updates the message itself.
_CHOSE_PREFIX = "Selected: "

# Default prefix for message shortcuts an agent answers as commands: the callback
# id starts with it and the rest is the command name (crux_summarize ->
# "summarize"). Slack forbids custom slash commands inside threads, so a shortcut
# is the thread-aware entry. Configurable per deployment (SLACK_COMMAND_PREFIX) —
# a workspace may already have its own naming convention.
DEFAULT_COMMAND_SHORTCUT_PREFIX = "crux_"

# What a slash command's `respond` looks like from here: text in, an ephemeral
# answer to whoever typed the command.
Respond = Callable[[str], Awaitable[object]]


class SlackInteractions:
    """Clicks, submits, shortcuts and slash commands of one Slack app."""

    def __init__(
        self,
        app: AsyncApp,
        dispatcher: GatewayDispatcher,
        poster: ChatClient,
        *,
        agent: str,
        command_prefix: str = DEFAULT_COMMAND_SHORTCUT_PREFIX,
    ) -> None:
        self._app = app
        self._dispatcher = dispatcher
        self._poster = poster  # opens modals on a form-open click (same account)
        self._agent = agent  # our own name; a command names the agent to run
        # Which message shortcuts are commands, and where the command name starts.
        # Empty = every shortcut is a command and its callback id IS the name.
        self._command_prefix = command_prefix

    def register(self) -> None:
        # One handler for every engine widget (action ids share a prefix).
        self._app.action(re.compile(f"^{WIDGET_ACTION_PREFIX}"))(self._on_action)
        self._app.view(FORM_CALLBACK)(self._on_view)
        # Message shortcuts are the thread-aware command entry (Slack forbids
        # custom slash commands in threads); one handler for the whole family.
        # escape(): the prefix is configuration, not a pattern.
        self._app.shortcut(re.compile(f"^{re.escape(self._command_prefix)}"))(self._on_shortcut)
        # Slash commands are the entry everywhere ELSE: a channel, a direct
        # message. Every command the app declares lands here; which ones exist
        # is the app's configuration, not ours.
        self._app.command(re.compile(r"^/"))(self._on_command)

    # -- bolt entry points: ack within 3s, then do the work ---------------------

    async def _on_action(self, ack, body: dict) -> None:
        await ack()
        try:
            await self.handle_action(body)
        except Exception:
            logger.exception("failed to handle Slack block action")

    async def _on_view(self, ack, body: dict) -> None:
        await ack()
        try:
            await self.handle_view(body)
        except Exception:
            logger.exception("failed to handle Slack view submission")

    async def _on_shortcut(self, ack, body: dict) -> None:
        await ack()  # Slack demands an ack within 3s; the turn runs after it
        try:
            self.handle_shortcut(body)
        except Exception:
            logger.exception("failed to handle Slack shortcut")

    async def _on_command(self, ack, body: dict, respond) -> None:
        await ack()
        try:
            await self.handle_command(body, respond)
        except Exception:
            logger.exception("failed to handle Slack slash command")

    # -- clicks ---------------------------------------------------------------------

    async def handle_action(self, body: dict) -> None:
        actions = body.get("actions") or []
        if not actions:
            return
        token, form_token, value = decode_action(actions[0])
        user_id = (body.get("user") or {}).get("id", "")
        approval = decode_approval(actions[0])
        if approval:
            # A request for a CREDENTIAL. Not to be confused with the Allow/Block
            # further down: that one approves a tool call mid-turn and is
            # answered by whoever is in the conversation, while this one is
            # addressed to a named person and refuses everybody else.
            #
            # The broker rewrites its own card once it has the answer, so the
            # buttons are stripped from here only when the click landed on a
            # request that no longer exists.
            outcome = self._dispatcher.resolve_approval(approval, value, user_id)
            if outcome is ApprovalOutcome.NOT_MINE:
                await self._strip_buttons(body, BUTTONS_RETIRED_TEXT)
            elif outcome is ApprovalOutcome.NOT_ALLOWED:
                # Told off privately; the card stays live for the person it is
                # addressed to — the same answer the HTTP receiver gives.
                await self._whisper(body, NOT_AN_APPROVER_TEXT)
            return
        screen, state = decode_screen(actions[0])
        if screen:
            # An engine screen: redraw the message it came from, no turn.
            await self._dispatcher.redraw_screen(
                state, value, post_id=self._message_id(body), user_id=user_id
            )
            return
        if form_token:
            # The button deliberately SURVIVES the open: a modal closed without
            # submitting can then be reopened. It is retired when the form is
            # answered (InteractionDispatcher.submit_form) or when its click finds
            # nothing left to open.
            if not await self._open_modal(body, form_token):
                await self._strip_buttons(body, BUTTONS_RETIRED_TEXT)
            return
        # A blocking mid-turn request: ask_user_confirm, or the confirmation
        # gate in front of a tool call. Addressed to the conversation, so any
        # click that carries the token answers it.
        if not self._dispatcher.resolve_pending(token, value):
            # Slack names the element that fired, so a picker's id is resolvable.
            await self._dispatcher.consume_action(
                token, value, user_id, pick=picked_kind(actions[0])
            )
        # Slack won't retire the buttons on its own — strip them off the message so a
        # fire-and-forget widget can't be clicked twice.
        await self._strip_buttons(body, f"{_CHOSE_PREFIX}{value}")

    # -- commands -------------------------------------------------------------------

    def handle_shortcut(self, body: dict) -> None:
        """A message shortcut runs a command in the conversation of the message it
        was invoked on — the thread if there is one, else the message itself (which
        is what a reply would start). The callback id names the command."""
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

    async def handle_command(self, body: dict, respond: Respond) -> None:
        """A slash command: the mirror of the HTTP receiver's `/command` route.
        A screen that owns the command is drawn by the engine, no model
        involved; anything else is a turn of the agent. Either way the only
        answer that belongs to the command itself is a private one — a refusal,
        or the receipt — and `respond` is exactly that."""
        command = str(body.get("command", "")).lstrip("/")
        if not command:
            return
        channel_id = str(body.get("channel_id", ""))
        # A command carries no thread (Slack forbids them there), so it runs as
        # the channel's own conversation — or the direct message's.
        kind = KIND_DM if body.get("channel_name") == "directmessage" else KIND_CHANNEL
        user_id = str(body.get("user_id", ""))
        opened = await self._dispatcher.open_screen(
            self._agent, command,
            channel_id=channel_id, conversation_id=channel_id,
            kind=kind, user_id=user_id,
        )
        if opened.owned:
            if opened.refused:
                await respond(opened.refused)
            return
        text = f"/{command} {body.get('text', '')}".strip()
        result = self._dispatcher.invoke_command(
            self._agent,
            channel_id=channel_id,
            conversation_id=channel_id,
            kind=kind,
            text=text,
            user_id=user_id,
            username=str(body.get("user_name", "")),
        )
        if result is ActionResult.UNAVAILABLE:
            logger.warning("command %s: agent %s has no live presence", command, self._agent)
            await respond(AGENT_UNAVAILABLE_TEXT)
            return
        await respond(COMMAND_ACK_TEXT)

    # -- modals and message surgery -------------------------------------------------

    async def _open_modal(self, body: dict, form_token: str) -> bool:
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

    async def handle_view(self, body: dict) -> None:
        view = body.get("view") or {}
        state = view.get("private_metadata", "")
        submission = extract_submission(view.get("state") or {})
        user_id = (body.get("user") or {}).get("id", "")
        await self._dispatcher.submit_form(state, submission, cancelled=False, user_id=user_id)

    @staticmethod
    def _message_id(body: dict) -> str:
        """The clicked message in the composite form SlackChatClient uses, so a
        screen redraw goes through the neutral update verb."""
        channel = (body.get("channel") or {}).get("id", "")
        ts = (body.get("message") or {}).get("ts", "")
        return f"{channel}{MESSAGE_ID_SEP}{ts}" if channel and ts else ""

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

    async def _whisper(self, body: dict, text: str) -> None:
        """A word only the clicker sees, in the channel they clicked in."""
        channel = (body.get("channel") or {}).get("id", "")
        user = (body.get("user") or {}).get("id", "")
        if not (channel and user):
            return
        try:
            await self._app.client.chat_postEphemeral(channel=channel, user=user, text=text)
        except Exception:
            logger.debug("could not whisper to %s in %s", user, channel, exc_info=True)
