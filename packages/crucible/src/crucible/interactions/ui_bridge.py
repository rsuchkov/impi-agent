"""WidgetUiBridge: the concrete UiBridge port.

Turns a mid-turn UI request (the runtime blocking on a confirm/select) into an
interactive widget and BLOCKS until the human answers (or a timeout defaults it).
Unlike the fire-and-forget WidgetService, the click resolves a pending Future the
agent's turn is waiting on — so the turn continues in-place with the answer.

Platform-neutral: it builds neutral ``Action``s and posts through the WidgetPoster
port, so it works over any gateway that implements that port.
"""

import asyncio
import logging
import secrets
from collections.abc import Mapping

from crucible.ports.agent.ui import UiOutcome, UiRequest
from crucible.ports.chat.types import KIND_THREAD, Action, ConversationRef
from crucible.ports.chat.widgets import WidgetPoster
from crucible.interactions.pending_ui import CONFIRM_NO, CONFIRM_YES, PendingUiRequests
from crucible.store.base import SessionStore

logger = logging.getLogger(__name__)

# The dropdown's pre-selection label (engine chrome); the prompt shows above it.
_SELECT_PLACEHOLDER = "Select an option"
# Shown in place of the buttons once a blocking request can no longer be answered,
# so a late click can't hit a stale button (some platforms error on that).
_EXPIRED_MESSAGE = "⌛ This request expired — no answer in time."
_CANCELLED_MESSAGE = "This request was cancelled."


class WidgetUiBridge:
    def __init__(
        self,
        posters: Mapping[str, WidgetPoster],
        sessions: SessionStore,
        pending: PendingUiRequests,
        *,
        callback_url: str,
        timeout: float = 90.0,
    ) -> None:
        self._posters = posters
        self._sessions = sessions
        self._pending = pending
        self._callback_url = callback_url
        self._timeout = timeout

    async def request(self, runtime_session_id: str, req: UiRequest) -> UiOutcome:
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        poster = self._posters.get(record.agent) if record else None
        if record is None or poster is None:
            logger.warning("ui bridge: no session/poster for %s", runtime_session_id)
            return UiOutcome(cancelled=True)

        token = secrets.token_hex(16)
        actions = self._build_actions(req, token)
        if actions is None:
            # A method we don't render as buttons (free-text input/editor): decline
            # safely rather than hang. ask_user_* tools cover the button cases.
            logger.info("ui bridge: unsupported method %r; declining", req.method)
            return UiOutcome(cancelled=True)

        future = self._pending.register(
            token, method=req.method, agent=record.agent, conversation_id=record.conversation_id
        )
        thread_root = record.conversation_id if record.kind == KIND_THREAD else ""
        ref = ConversationRef(
            channel_id=record.channel_id,
            conversation_id=record.conversation_id,
            message_id=record.conversation_id,
            thread_root_id=thread_root,
        )
        try:
            post_id = await poster.post_actions(
                ref, self._prompt_text(req), actions, callback_url=self._callback_url
            )
        except Exception:
            logger.exception("ui bridge: failed to post widget; declining")
            self._pending.discard(token)
            return UiOutcome(cancelled=True)

        try:
            outcome = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            self._pending.discard(token)
            logger.info("ui bridge: timed out after %.0fs; defaulting to cancelled", self._timeout)
            await self._retract(poster, post_id, _EXPIRED_MESSAGE)
            return UiOutcome(cancelled=True)
        # A click updates the message itself (the receiver's response). Only a
        # cancel — the timeout above, or the user typing instead of clicking —
        # leaves stale buttons behind, so retract them.
        if outcome.cancelled:
            await self._retract(poster, post_id, _CANCELLED_MESSAGE)
        return outcome

    # -- internals ----------------------------------------------------------

    @staticmethod
    async def _retract(poster: WidgetPoster, post_id: str, text: str) -> None:
        """Best-effort: drop the widget's buttons. A failure must not break the
        turn (the outcome is already decided)."""
        try:
            await poster.retract(post_id, text)
        except Exception:
            logger.debug("ui bridge: retract failed for post %s", post_id)

    def _build_actions(self, req: UiRequest, token: str) -> list[Action] | None:
        if req.method == "confirm":
            return [
                Action(id="yes", label=CONFIRM_YES, value=CONFIRM_YES, style="primary",
                       context={"token": token}),
                Action(id="no", label=CONFIRM_NO, value=CONFIRM_NO, style="danger",
                       context={"token": token}),
            ]
        if req.method == "select" and req.options:
            return [
                Action(id="sel", label=_SELECT_PLACEHOLDER, kind="select",
                       options=req.options, context={"token": token})
            ]
        return None

    @staticmethod
    def _prompt_text(req: UiRequest) -> str:
        if req.method == "confirm" and req.message:
            return f"{req.title}\n\n{req.message}".strip() if req.title else req.message
        return req.title or req.message
