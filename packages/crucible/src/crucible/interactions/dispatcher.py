"""InteractionDispatcher: the transport-neutral brain behind interactive callbacks.

A gateway's callback transport (the HTTP receiver for Mattermost, the socket
handler for Slack) normalizes a raw click/submission and calls these methods. The
dispatcher either resolves a blocking mid-turn request (the paused turn continues
in place) or feeds the choice back into the conversation as a new synthetic
message. It knows nothing about HTTP or any platform's payload shape.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.interactions import form_from_json
from crucible.ports.chat.types import KIND_THREAD, ConversationRef, Form, IncomingMessage
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.store.base import FormRecord, FormStore, InteractionStore

logger = logging.getLogger(__name__)


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
        sinks: Mapping[str, AgentSink],
        pending: PendingUiRequests,
        forms: FormStore,
    ) -> None:
        self._interactions = interactions
        self._sinks = sinks
        self._pending = pending
        self._forms = forms

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

    async def consume_action(self, token: str, value: str, user_id: str) -> ActionResult:
        """Fire-and-forget widget click: consume the one-shot interaction and feed
        the chosen ``value`` back as a synthetic message → a new turn."""
        record = await self._interactions.take_interaction(token) if token else None
        if record is None:
            return ActionResult.UNKNOWN
        target = self._sinks.get(record.agent)
        if target is None:
            return ActionResult.UNAVAILABLE
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

    async def submit_form(
        self, state: str, submission: dict, cancelled: bool, user_id: str
    ) -> bool:
        """Modal-form submission: consume the pending form and, unless cancelled,
        feed the rendered values back as a synthetic message. Returns whether a
        message was fed."""
        record = await self._forms.get_form(state) if state else None
        if record is None:
            return False
        await self._forms.delete_form(state)  # one-shot
        if cancelled:
            return False
        target = self._sinks.get(record.agent)
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
            text=_render_submission(record, submission),
            user_id=user_id,
            kind=record.kind,
            synthetic=True,
        )
        target.sink.submit(msg, target.chat)
        logger.info("form %s submitted: %d field(s)", state[:8], len(submission))
        return True


def _render_submission(record: FormRecord, submission: dict) -> str:
    """A readable block of the submitted values, keyed by the form's field labels,
    for the agent to parse on its next turn."""
    form = form_from_json(record.spec)
    lines = [f"[form: {form.title}]"]
    for field_ in form.fields:
        value = submission.get(field_.name)
        if isinstance(value, bool):
            value = "yes" if value else "no"
        lines.append(f"- {field_.label}: {value if value not in (None, '') else '—'}")
    return "\n".join(lines)
