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
from collections.abc import Mapping
from datetime import datetime, timezone

from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.interactions import form_to_json
from crucible.ports.chat.types import KIND_THREAD, Action, ConversationRef, Form
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
_OPEN_LABEL = "📝 Fill in…"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InteractionService:
    def __init__(
        self,
        posters: Mapping[str, ChatClient],
        sessions: SessionStore,
        interactions: InteractionStore,
        forms: FormStore,
        *,
        callback_url: str,
    ) -> None:
        self._posters = posters
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
        poster = self._posters.get(agent)
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
        ctx = {"interaction_id": interaction_id, "token": token}
        if style == "select":
            # One dropdown action; the gateway's codec maps the pick back on click.
            actions = [
                Action(id="sel", label=_SELECT_PLACEHOLDER, kind="select",
                       options=tuple(options), context=ctx)
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
        poster = self._posters.get(agent)
        if record is None or poster is None:
            logger.warning("form open: no session/poster for %s / %s", agent, runtime_session_id)
            return False

        token = secrets.token_hex(16)
        await self._forms.create_form(
            FormRecord(
                token=token,
                agent=agent,
                channel_id=record.channel_id,
                conversation_id=record.conversation_id,
                kind=record.kind,
                spec=form_to_json(form),
                created_at=_now(),
            )
        )
        # context.form marks this as a form-open click (vs a widget choice); the
        # receiver looks the spec up by this token and opens the modal.
        actions = [Action(id="openform", label=_OPEN_LABEL, context={"form": token})]
        await poster.post_actions(
            self._ref(record), form.intro or form.title, actions, callback_url=self._callback_url
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
