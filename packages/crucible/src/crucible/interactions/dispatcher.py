"""InteractionDispatcher: the transport-neutral brain behind interactive callbacks.

A gateway's callback transport (the HTTP receiver for Mattermost, the socket
handler for Slack) normalizes a raw click/submission and calls these methods. The
dispatcher either resolves a blocking mid-turn request (the paused turn continues
in place) or feeds the choice back into the conversation as a new synthetic
message. It knows nothing about HTTP or any platform's payload shape.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from uuid import uuid4

from crucible.approvals import ApprovalOutcome, PendingApprovals
from crucible.interactions.labels import humanize
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.ports import FormHandlers
from crucible.interactions.presence import AgentPresence
from crucible.interactions.results import ActionResult
from crucible.interactions.screens import (
    Redrawn,
    ScreenOpened,
    ScreenRegistry,
    ScreenState,
    post_first_view,
    refusal,
)
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.flow import MessageSink
from crucible.ports.chat.interactions import form_from_json
from crucible.ports.chat.types import (
    KIND_THREAD,
    STATIC_FIELD_TYPES,
    ConversationRef,
    Form,
    IncomingMessage,
    QuotedMessage,
)
from crucible.ports.replica import (
    ClickForwarder,
    ClickKind,
    ForwardedClick,
    Replica,
    Replicas,
    replica_of,
)
from crucible.store.base import FormRecord, FormStore, InteractionStore

logger = logging.getLogger(__name__)

# Replaces the "fill in" message once its form has been answered (engine chrome).
_FORM_SUBMITTED_MESSAGE = "✅ Submitted."


@dataclass(frozen=True)
class AgentSink:
    """Where a resolved click is routed for one agent: its message sink (the
    coalescer) plus the chat client of every gateway it is reached through, by
    gateway name, and which of them is the default — the one an engine-started
    turn goes to, and the one a caller gets when it names no gateway."""

    sink: MessageSink
    gateways: Mapping[str, ChatClient]
    default: str

    @property
    def chat(self) -> ChatClient:
        """The default gateway's chat client."""
        return self.gateways[self.default]

    def chat_for(self, gateway: str) -> ChatClient:
        """The chat client of ``gateway``, or the default's when the agent has
        no such gateway (a message the engine made itself names none)."""
        return self.gateways.get(gateway) or self.chat


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
        approvals: PendingApprovals | None = None,
        handlers: FormHandlers | None = None,
        callback_url: str = "",
        replica: Replica | None = None,
        replicas: Replicas | None = None,
        clicks: ClickForwarder | None = None,
    ) -> None:
        self._interactions = interactions
        self._presence = presence
        self._pending = pending
        self._forms = forms
        # Screens the engine answers itself (empty = every command is an agent's).
        self._screens = screens
        # Requests for authorization waiting on a human (None = nothing here
        # asks for any).
        self._approvals = approvals
        # Handlers for forms the application answers itself, keyed by name
        # (None = nothing does, so every form's values go back into the
        # conversation, which is what a tool wants).
        self._handlers = handlers
        self._callback_url = callback_url
        # This replica, the registry of the others and the way to reach one:
        # a click on a question another replica asked (its id is in the token)
        # is handed to it. All three None = one process, every question here.
        self._replica = replica
        self._replicas = replicas
        self._clicks = clicks

    async def resolve_approval(self, token: str, value: str, user_id: str) -> ApprovalOutcome:
        """A click answering a request for authorization — a credential, a
        gated tool call.

        Tried before every other click path, because it is the only one whose
        answer may depend on WHO clicked. Whether it does is per request: a
        credential names its approvers, a tool call the person who asked (or
        TOOL_APPROVERS). A click from somebody outside a named set is reported
        back rather than falling through to a handler that does not check.
        """
        if not token or self._approvals is None:
            return ApprovalOutcome.NOT_MINE
        outcome = self._approvals.resolve(token, value, user_id)
        if outcome is ApprovalOutcome.NOT_MINE:
            forwarded = await self._forward(ClickKind.APPROVAL, token, value, user_id)
            if forwarded is not None:
                return ApprovalOutcome[forwarded.name]
        return outcome

    async def resolve_pending(self, token: str, value: str) -> bool:
        """A blocking mid-turn confirm/select: resolve the Future the paused turn
        is waiting on so it continues in place. False when ``token`` is not a live
        blocking request (the caller then tries the fire-and-forget path)."""
        if not token:
            return False
        if self._pending.resolve(token, value):
            return True
        forwarded = await self._forward(ClickKind.PENDING, token, value, "")
        return forwarded is ForwardedClick.RESOLVED

    async def _forward(
        self, kind: ClickKind, token: str, value: str, user_id: str
    ) -> ForwardedClick | None:
        """What the replica that asked did with the click — None when the token
        is nobody else's (untagged, our own, or its replica is gone)."""
        if self._replica is None or self._replicas is None or self._clicks is None:
            return None
        minted_by = replica_of(token)
        if not minted_by or minted_by == self._replica.id:
            return None
        url = await self._replicas.replica_url(minted_by)
        if not url:
            return None
        try:
            return await self._clicks.forward_click(url, kind, token, value, user_id)
        except Exception:
            logger.warning("click for replica %s at %s could not be forwarded", minted_by, url, exc_info=True)
            return None

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
        chat = target.chat_for(record.gateway)  # the click comes back through the widget's gateway
        if pick:
            value = await humanize(value, pick, chat)
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
            gateway=record.gateway,
        )
        target.sink.submit(msg, chat)
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
        quoted: QuotedMessage | None = None,
        gateway: str = "",
    ) -> ActionResult:
        """A slash command, a message shortcut, a reaction: run it as an
        ordinary turn of the agent, in the conversation it was invoked from —
        the reply is posted there like any other answer. ``quoted`` is the
        message the command points at, carried as somebody else's words.

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
            quoted=quoted,
            gateway=gateway,
        )
        target.sink.submit(msg, target.chat_for(gateway))
        # What was typed is the person's, and a log is kept: the fact of the
        # command at INFO, its text only when someone asked for everything.
        logger.info(
            "command for %s in %s by %s (%d chars)", agent, conversation_id, user_id, len(text)
        )
        logger.debug("command text for %s: %r", agent, text)
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
    ) -> ScreenOpened:
        """A command the ENGINE answers: render its first view and post it as
        ``agent``.

        Three outcomes, and the caller needs all three: no screen owns the
        command (route it to the agent as usual), a screen owns it and its view
        was posted, or a screen owns it and refused — in which case nothing was
        posted and the reason belongs to whoever asked, privately.
        """
        screen = self._screens.get(command) if self._screens else None
        target = self._presence.poster(agent)
        if screen is None or target is None:
            return ScreenOpened(owned=False)
        thread_root = conversation_id if kind == KIND_THREAD else ""
        ref = ConversationRef(
            channel_id=channel_id,
            conversation_id=conversation_id,
            message_id=conversation_id,
            thread_root_id=thread_root,
        )
        # A screen that shows the person's own things declares it, and is
        # refused wherever anybody else could read it.
        denial = await refusal(screen, user_id=user_id, ref=ref, kind=kind)
        if denial:
            # Nothing is posted. Whatever the screen would have drawn, this
            # conversation is not where it may appear.
            logger.info("screen %s refused for %s: %s", screen.command, user_id, denial)
            return ScreenOpened(owned=True, refused=denial)
        await post_first_view(
            screen, target, ref,
            agent=agent, user_id=user_id, callback_url=self._callback_url,
        )
        logger.info("screen %s opened for %s by %s", screen.command, agent, user_id)
        return ScreenOpened(owned=True)

    async def redraw_screen(
        self, state_raw: str, value: str, *, post_id: str, user_id: str
    ) -> Redrawn | None:
        """A click on a screen: render the state it carried and rewrite the same
        message. No turn, no new message — this is a UI, not a conversation.
        None when the click was not a screen's to answer, or was refused; what
        the clicker alone is told travels back in ``Redrawn``."""
        state = ScreenState.decode(state_raw)
        screen = self._screens.get(state.screen) if (self._screens and state) else None
        if state is None or screen is None:
            return None
        poster = self._presence.poster(state.agent)
        if poster is None or not post_id:
            return None
        # Asked again, and with no ref: the message exists, so what is left to
        # decide is whether THIS person may drive it. A click carries whoever
        # pressed it, not whoever opened the card.
        if await refusal(screen, user_id=user_id, ref=None):
            logger.info("screen %s: click refused for %s", screen.command, user_id)
            return None
        state = replace(state, post_id=post_id)
        if value:
            # What the control returned (a picked option, a button's value) is the
            # screen's input for this render.
            state = state.with_data(value=value)
        view = await screen.render(state, user_id=user_id)
        await poster.update_cards(post_id, list(view.cards), callback_url=self._callback_url)
        return Redrawn(view.private_note)

    async def submit_form(
        self, state: str, submission: dict, cancelled: bool, user_id: str
    ) -> bool:
        """Modal-form submission: consume the pending form and hand the values
        on — to the application when it claims them, otherwise back into the
        conversation as a synthetic message. Returns whether anything took them.

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
        if record.handler:
            # Routed by what the form was written with, not by asking handlers
            # whether the token is theirs. That is the difference between a
            # handler failing and the values quietly continuing into somebody's
            # conversation — which for some forms is the one unacceptable
            # outcome.
            return await self._handle_form(record, submission, user_id)
        target = self._presence.sink(record.agent)
        if target is None:
            return False
        chat = target.chat_for(record.gateway)  # the form came back through the gateway it was posted on
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        msg = IncomingMessage(
            ref=ConversationRef(
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                message_id=f"form-{state[:12]}",
                thread_root_id=thread_root,
            ),
            text=await _render_submission(record, submission, chat),
            user_id=user_id,
            kind=record.kind,
            gateway=record.gateway,
            synthetic=True,
        )
        target.sink.submit(msg, chat)
        logger.info("form %s submitted: %d field(s)", state[:8], len(submission))
        await self._retire_button(record, chat)
        return True

    async def _handle_form(
        self, record: FormRecord, submission: dict, user_id: str
    ) -> bool:
        """Give a form's values to the handler it named."""
        handler = self._handlers.get(record.handler) if self._handlers else None
        if handler is None:
            # Named a handler nobody registered: a composition error. The values
            # are dropped rather than sent on — the alternative is guessing, and
            # this is exactly the form whose values must not be guessed about.
            logger.error(
                "form %s names the handler %r, which is not registered — dropped",
                record.token[:8], record.handler,
            )
            return False
        try:
            await handler.handle(record, dict(submission), user_id)
        except Exception:
            logger.exception("form %s: handler %r failed", record.token[:8], record.handler)
            return False
        logger.info("form %s handled by %r", record.token[:8], record.handler)
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
