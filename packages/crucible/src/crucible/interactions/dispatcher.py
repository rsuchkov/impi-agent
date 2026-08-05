"""InteractionDispatcher: the transport-neutral brain behind interactive callbacks.

A gateway's callback transport (the HTTP receiver for Mattermost, the socket
handler for Slack) normalizes a raw click/submission and calls these methods. The
dispatcher either resolves a blocking mid-turn request (the paused turn continues
in place) or feeds the choice back into the conversation as a new synthetic
message. It knows nothing about HTTP or any platform's payload shape.
"""

import logging
from dataclasses import dataclass
from enum import Enum, auto
from uuid import uuid4

from crucible.interactions.labels import humanize
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.presence import AgentPresence
from crucible.interactions.screens import ScreenRegistry, ScreenState
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.interactions import form_from_json
from crucible.ports.chat.types import (
    KIND_THREAD,
    STATIC_FIELD_TYPES,
    ConversationRef,
    Form,
    IncomingMessage,
)
from crucible.store.base import FormRecord, FormStore, InteractionStore

logger = logging.getLogger(__name__)

# Replaces the "fill in" message once its form has been answered (engine chrome).
_FORM_SUBMITTED_MESSAGE = "✅ Submitted."


@dataclass(frozen=True)
class AgentSink:
    """Where a resolved click is routed for one agent: its message sink (the
    coalescer) plus the chat client to reply through."""

    sink: MessageSink
    chat: ChatClient


class ActionResult(Enum):
    FED = auto()  # the value was fed back into the conversation as a new turn
    UNKNOWN = auto()  # no live interaction for this token (retire the buttons)
    UNAVAILABLE = auto()  # the agent has no sink to route to


@dataclass(frozen=True)
class FormOpen:
    """What a form-open click needs: which agent owns it and the form to render."""

    agent: str
    form: Form


class InteractionDispatcher:
    def __init__(
        self,
        interactions: InteractionStore,
        presence: AgentPresence,
        pending: PendingUiRequests,
        forms: FormStore,
        *,
        screens: ScreenRegistry | None = None,
        callback_url: str = "",
    ) -> None:
        self._interactions = interactions
        self._presence = presence
        self._pending = pending
        self._forms = forms
        # Screens the engine answers itself (empty = every command is an agent's).
        self._screens = screens
        self._callback_url = callback_url

    def resolve_pending(self, token: str, value: str) -> bool:
        """A blocking mid-turn confirm/select: resolve the Future the paused turn
        is waiting on so it continues in place. False when ``token`` is not a live
        blocking request (the caller then tries the fire-and-forget path)."""
        return bool(token) and self._pending.resolve(token, value)

    async def load_form(self, form_token: str) -> FormOpen | None:
        """Look up a pending form by its token so the transport can open the modal
        (opening is platform-specific). None if unknown / already consumed."""
        record = await self._forms.get_form(form_token) if form_token else None
        if record is None:
            return None
        return FormOpen(agent=record.agent, form=form_from_json(record.spec))

    async def consume_action(
        self, token: str, value: str, user_id: str, *, pick: str = ""
    ) -> ActionResult:
        """Fire-and-forget widget click: consume the one-shot interaction and feed
        the chosen ``value`` back as a synthetic message → a new turn. ``pick``
        ("user"/"channel") marks a picker, whose value is an id to resolve."""
        record = await self._interactions.take_interaction(token) if token else None
        if record is None:
            return ActionResult.UNKNOWN
        target = self._presence.sink(record.agent)
        if target is None:
            return ActionResult.UNAVAILABLE
        if pick:
            value = await humanize(value, pick, target.chat)
        if not value:
            # Surfaces a malformed/unknown callback shape (a codec that failed to
            # find the picked value).
            logger.warning("interaction %s: empty value", record.interaction_id)
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        msg = IncomingMessage(
            ref=ConversationRef(
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                message_id=f"interact-{record.interaction_id}",
                thread_root_id=thread_root,
            ),
            text=value,
            user_id=user_id,
            kind=record.kind,
            synthetic=True,  # engine-generated from a click, not typed by a human
        )
        target.sink.submit(msg, target.chat)
        logger.info("interaction %s resolved: %r by %s", record.interaction_id, value, user_id)
        return ActionResult.FED

    def invoke_command(
        self,
        agent: str,
        *,
        channel_id: str,
        conversation_id: str,
        kind: str,
        text: str,
        user_id: str,
        username: str = "",
    ) -> ActionResult:
        """A slash command / message shortcut: run it as an ordinary turn of the
        agent, in the conversation it was invoked from — the reply is posted
        there like any other answer.

        Unlike a click there is no stored record — the transport supplies the
        agent and the conversation it resolved from its own payload.

        Synchronous like the sink it feeds — submitting is fire-and-forget."""
        target = self._presence.sink(agent)
        if target is None:
            return ActionResult.UNAVAILABLE
        thread_root = conversation_id if kind == KIND_THREAD else ""
        msg = IncomingMessage(
            ref=ConversationRef(
                channel_id=channel_id,
                conversation_id=conversation_id,
                # Unique per invocation: the flow dedups on this id, so reusing
                # the triggering message's id would swallow repeat invocations.
                message_id=f"cmd-{uuid4().hex[:12]}",
                thread_root_id=thread_root,
            ),
            text=text,
            user_id=user_id,
            username=username,
            kind=kind,
            mentioned=True,  # the command WAS addressed to this agent
            synthetic=True,  # engine-generated from a command, not typed
        )
        target.sink.submit(msg, target.chat)
        logger.info("command for %s in %s by %s: %r", agent, conversation_id, user_id, text)
        return ActionResult.FED

    async def open_screen(
        self,
        agent: str,
        command: str,
        *,
        channel_id: str,
        conversation_id: str,
        kind: str,
        user_id: str,
    ) -> bool:
        """A command the ENGINE answers: render its first view and post it as
        ``agent``. False when no screen owns the command (the caller then routes
        it to the agent as usual) or the agent isn't available."""
        screen = self._screens.get(command) if self._screens else None
        target = self._presence.poster(agent)
        if screen is None or target is None:
            return False
        state = ScreenState(screen=screen.command, agent=agent)
        view = await screen.render(state, user_id=user_id)
        thread_root = conversation_id if kind == KIND_THREAD else ""
        ref = ConversationRef(
            channel_id=channel_id,
            conversation_id=conversation_id,
            message_id=conversation_id,
            thread_root_id=thread_root,
        )
        await target.post_cards(ref, list(view.cards), callback_url=self._callback_url)
        logger.info("screen %s opened for %s by %s", screen.command, agent, user_id)
        return True

    async def redraw_screen(
        self, state_raw: str, value: str, *, post_id: str, user_id: str
    ) -> bool:
        """A click on a screen: render the state it carried and rewrite the same
        message. No turn, no new message — this is a UI, not a conversation."""
        state = ScreenState.decode(state_raw)
        screen = self._screens.get(state.screen) if (self._screens and state) else None
        if state is None or screen is None:
            return False
        poster = self._presence.poster(state.agent)
        if poster is None or not post_id:
            return False
        if value:
            # What the control returned (a picked option, a button's value) is the
            # screen's input for this render.
            state = state.with_data(value=value)
        view = await screen.render(state, user_id=user_id)
        await poster.update_cards(post_id, list(view.cards), callback_url=self._callback_url)
        return True

    async def submit_form(
        self, state: str, submission: dict, cancelled: bool, user_id: str
    ) -> bool:
        """Modal-form submission: consume the pending form and feed the rendered
        values back as a synthetic message. Returns whether a message was fed.

        A CANCELLED modal leaves the form pending on purpose — the "fill in"
        button stays live, so closing the dialog by accident costs nothing. Only
        an actual submission consumes the form and retires the button."""
        record = await self._forms.get_form(state) if state else None
        if record is None:
            return False
        if cancelled:
            logger.info("form %s cancelled — its button stays live", state[:8])
            return False
        await self._forms.delete_form(state)  # one-shot: answered
        target = self._presence.sink(record.agent)
        if target is None:
            return False
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        msg = IncomingMessage(
            ref=ConversationRef(
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                message_id=f"form-{state[:12]}",
                thread_root_id=thread_root,
            ),
            text=await _render_submission(record, submission, target.chat),
            user_id=user_id,
            kind=record.kind,
            synthetic=True,
        )
        target.sink.submit(msg, target.chat)
        logger.info("form %s submitted: %d field(s)", state[:8], len(submission))
        await self._retire_button(record, target.chat)
        return True

    @staticmethod
    async def _retire_button(record: FormRecord, chat: ChatClient) -> None:
        """Strike the "fill in" button off its message once the form is answered —
        the platforms don't do it themselves, and a second click would find
        nothing. Best-effort: a failure must not undo the submission."""
        if not record.post_id:  # written before the engine recorded the post id
            return
        try:
            await chat.retract(record.post_id, _FORM_SUBMITTED_MESSAGE)
        except Exception:
            logger.warning("could not retire the form button %s", record.post_id, exc_info=True)


async def _render_submission(record: FormRecord, submission: dict, chat: ChatClient) -> str:
    """A readable block of the submitted values, keyed by the form's field labels,
    for the agent to parse on its next turn. Picked people/channels are resolved
    to names; a static label field carries no value and is skipped."""
    form = form_from_json(record.spec)
    lines = [f"[form: {form.title}]"]
    for field_ in form.fields:
        if field_.type in STATIC_FIELD_TYPES:
            continue
        value = submission.get(field_.name)
        if isinstance(value, bool):
            value = "yes" if value else "no"
        if value not in (None, ""):
            value = await humanize(str(value), field_.type, chat)
        lines.append(f"- {field_.label}: {value if value not in (None, '') else '—'}")
    return "\n".join(lines)
