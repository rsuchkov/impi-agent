"""FormService concrete: register a pending form, then post the button that
opens it.

A modal needs a click's trigger_id, so open_form is inherently two-step: this
posts a "fill in" button (fire-and-forget, like a widget); the click opens the
dialog (integrations receiver) and the submit comes back as a new message. The
spec is stashed in the form store between those steps.
"""

import logging
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone

from crucible.ports.chat.forms import form_to_json
from crucible.ports.chat.types import KIND_THREAD, Action, ConversationRef, Form
from crucible.ports.chat.widgets import WidgetPoster
from crucible.store.base import FormRecord, FormStore, SessionStore

logger = logging.getLogger(__name__)

_OPEN_LABEL = "📝 Fill in…"


class FormService:
    def __init__(
        self,
        posters: Mapping[str, WidgetPoster],
        sessions: SessionStore,
        forms: FormStore,
        *,
        callback_url: str,
    ) -> None:
        self._posters = posters
        self._sessions = sessions
        self._forms = forms
        self._callback_url = callback_url

    async def open(self, agent: str, runtime_session_id: str, form: Form) -> bool:
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
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        ref = ConversationRef(
            channel_id=record.channel_id,
            conversation_id=record.conversation_id,
            message_id=record.conversation_id,
            thread_root_id=thread_root,
        )
        # context.form marks this as a form-open click (vs a widget choice); the
        # receiver looks the spec up by this token and opens the modal.
        actions = [Action(id="openform", label=_OPEN_LABEL, context={"form": token})]
        await poster.post_actions(
            ref, form.intro or form.title, actions, callback_url=self._callback_url
        )
        return True
