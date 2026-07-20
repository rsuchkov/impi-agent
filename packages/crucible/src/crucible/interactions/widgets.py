"""WidgetService concrete: register an interaction, then post the buttons.

The composition root wires it with per-agent posters, the session/interaction
store, and the public callback URL. It implements the ``chat.WidgetService``
port so the tool layer never touches the store or the poster concretes.
"""

import logging
import secrets
from collections.abc import Mapping
from datetime import datetime, timezone

from crucible.ports.chat.types import KIND_THREAD, Action, ConversationRef
from crucible.ports.chat.widgets import WidgetPoster
from crucible.store.base import InteractionRecord, InteractionStore, SessionStore

logger = logging.getLogger(__name__)

# The dropdown's pre-selection label (engine chrome). The prompt shows above it.
_SELECT_PLACEHOLDER = "Select an option"


class WidgetService:
    def __init__(
        self,
        posters: Mapping[str, WidgetPoster],
        sessions: SessionStore,
        interactions: InteractionStore,
        *,
        callback_url: str,
    ) -> None:
        self._posters = posters
        self._sessions = sessions
        self._interactions = interactions
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
        await poster.post_actions(ref, prompt, actions, callback_url=self._callback_url)
        return True
