"""InteractionService concrete: resolve the conversation, register the pending
interaction/form, then post via the agent's ChatClient.

The composition root wires it with per-agent chat clients, the session /
interaction / form store, and the public callback URL. It implements the
``chat.InteractionService`` port so the tool layer never touches the store or the
posting concretes. This is the OUTBOUND half of the widget/form round-trip; the
INBOUND half (matching the click/submit) is the InteractionDispatcher.
"""

import logging
import secrets
from datetime import datetime, timezone

from crucible.interactions.presence import AgentPresence
from crucible.ports.chat.interactions import (
    ASK_CHANNELS,
    ASK_SELECT,
    ASK_USERS,
    form_to_json,
)
from crucible.ports.chat.types import (
    ACTION_CHANNEL_SELECT,
    ACTION_SELECT,
    ACTION_USER_SELECT,
    KIND_THREAD,
    PICK_FIELD_BY_KIND,
    Action,
    Choice,
    ConversationRef,
    Form,
)
from crucible.store.base import (
    FormRecord,
    FormStore,
    InteractionRecord,
    InteractionStore,
    SessionStore,
)

logger = logging.getLogger(__name__)

# The dropdown's pre-selection label (engine chrome). The prompt shows above it.
_SELECT_PLACEHOLDER = "Select an option"
_OPEN_LABEL = "📝 Fill in…"  # default wording; a form may bring its own
# ask(style=…) for the workspace pickers -> the Action kind and its placeholder.
# The pick marker itself comes from the ports table, so it can't drift.
_PICKERS = {
    ASK_USERS: (ACTION_USER_SELECT, "Select a person"),
    ASK_CHANNELS: (ACTION_CHANNEL_SELECT, "Select a channel"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InteractionService:
    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        interactions: InteractionStore,
        forms: FormStore,
        *,
        callback_url: str,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._interactions = interactions
        self._forms = forms
        self._callback_url = callback_url

    async def ask(
        self,
        agent: str,
        runtime_session_id: str,
        prompt: str,
        options: list[str],
        *,
        style: str = "buttons",
    ) -> bool:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent)
        if record is None or poster is None:
            logger.warning("widget ask: no session/poster for %s / %s", agent, runtime_session_id)
            return False

        interaction_id = secrets.token_hex(8)
        token = secrets.token_hex(16)
        # Register BEFORE posting so an instant click always finds the interaction.
        await self._interactions.create_interaction(
            InteractionRecord(
                interaction_id=interaction_id,
                token=token,
                agent=agent,
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                kind=record.kind,
                created_at=_now(),
            )
        )
        ctx: dict[str, str] = {"interaction_id": interaction_id, "token": token}
        if style in _PICKERS:
            # A picker returns a platform id; ``pick`` travels in the context so the
            # click can be resolved to a name (the callback alone wouldn't say).
            kind, placeholder = _PICKERS[style]
            ctx["pick"] = PICK_FIELD_BY_KIND[kind]
            actions = [Action(id="sel", label=placeholder, kind=kind, context=ctx)]
        elif style == ASK_SELECT:
            # One dropdown action; the gateway's codec maps the pick back on click.
            actions = [
                Action(id="sel", label=_SELECT_PLACEHOLDER, kind=ACTION_SELECT,
                       options=Choice.of(*options), context=ctx)
            ]
        else:
            actions = [
                Action(id=f"opt{i}", label=option, value=option, context=ctx)
                for i, option in enumerate(options)
            ]
        await poster.post_actions(self._ref(record), prompt, actions, callback_url=self._callback_url)
        return True

    async def open_form(self, agent: str, runtime_session_id: str, form: Form) -> bool:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._presence.poster(agent)
        if record is None or poster is None:
            logger.warning("form open: no session/poster for %s / %s", agent, runtime_session_id)
            return False

        token = secrets.token_hex(16)
        # context.form marks this as a form-open click (vs a widget choice); the
        # receiver looks the spec up by this token and opens the modal.
        actions = [
            Action(id="openform", label=form.open_label or _OPEN_LABEL, context={"form": token})
        ]
        post_id = await poster.post_actions(
            self._ref(record), form.intro or form.title, actions, callback_url=self._callback_url
        )
        # Registered AFTER posting, unlike a widget: the record carries the button's
        # own post id so submitting can retire it, and that id exists only now. A
        # click can't outrace this — it travels through the platform first.
        await self._forms.create_form(
            FormRecord(
                token=token,
                agent=agent,
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                kind=record.kind,
                spec=form_to_json(form),
                created_at=_now(),
                post_id=post_id,
            )
        )
        return True

    @staticmethod
    def _ref(record) -> ConversationRef:
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        return ConversationRef(
            channel_id=record.channel_id,
            conversation_id=record.conversation_id,
            message_id=record.conversation_id,
            thread_root_id=thread_root,
        )
