"""InteractionsServer: the HTTP-callback receiver for interactive widgets.

A gateway that delivers interactions as HTTP callbacks (e.g. Mattermost) points
its widget/form callbacks at this server. It is transport-neutral: an injected
``CallbackCodec`` translates the platform's payload/response shapes, and the
``InteractionDispatcher`` performs the neutral resolve-or-feed-back logic. Bound
on 0.0.0.0 (the platform reaches it from its own network); separate from the
localhost tool server on purpose — only this door is exposed, and only for
callbacks.
"""

import logging

from aiohttp import web

from crucible.interactions.callbacks import CallbackCodec
from crucible.interactions.dispatcher import ActionResult, InteractionDispatcher
from crucible.interactions.presence import AgentPresence

logger = logging.getLogger(__name__)

# User-facing chrome for the button message (engine text, not agent persona) —
# English per project convention.
_BUTTONS_RETIRED_MESSAGE = "These buttons are no longer active."
_AGENT_UNAVAILABLE_MESSAGE = "The agent is currently unavailable."
_CHOSE_PREFIX = "Selected: "


class InteractionsServer:
    def __init__(
        self,
        dispatcher: InteractionDispatcher,
        codec: CallbackCodec,
        presence: AgentPresence,
        *,
        host: str = "0.0.0.0",
        port: int = 8423,
        dialog_submit_url: str = "",
    ) -> None:
        self._dispatcher = dispatcher
        self._codec = codec
        self._presence = presence
        self._host = host
        self._port = port
        self._dialog_submit_url = dialog_submit_url
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/interact", self._interact)
        app.router.add_post("/dialog", self._dialog)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        logger.info(
            "integrations receiver on http://%s:%d (/interact, /dialog)", self._host, self._port
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _interact(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        cb = self._codec.parse_action(body)
        # One line per callback — confirms the platform actually reached us (the
        # platform->host hop is the usual failure point for widget callbacks).
        logger.info("interact callback: token=%s form=%s", cb.token, bool(cb.form_token))

        # Form-open click: open the modal synchronously — the trigger expires within
        # seconds.
        if cb.form_token:
            return await self._open_form_dialog(cb)

        # Blocking UI request (a mid-turn confirm/select): resolve the Future the
        # paused turn is waiting on. Must come before the fire-and-forget path,
        # which uses a different token store.
        if self._dispatcher.resolve_pending(cb.token, cb.value):
            return web.json_response(self._codec.reply_replace(f"{_CHOSE_PREFIX}{cb.value}"))

        result = await self._dispatcher.consume_action(cb.token, cb.value, cb.user_id)
        if result is ActionResult.UNKNOWN:
            return web.json_response(self._codec.reply_replace(_BUTTONS_RETIRED_MESSAGE))
        if result is ActionResult.UNAVAILABLE:
            return web.json_response(self._codec.reply_notice(_AGENT_UNAVAILABLE_MESSAGE))
        return web.json_response(self._codec.reply_replace(f"{_CHOSE_PREFIX}{cb.value}"))

    async def _open_form_dialog(self, cb) -> web.Response:
        form = await self._dispatcher.load_form(cb.form_token)
        if form is None:
            return web.json_response(self._codec.reply_replace(_BUTTONS_RETIRED_MESSAGE))
        poster = self._presence.poster(form.agent)
        if poster is None or not cb.trigger:
            return web.json_response(self._codec.reply_notice(_AGENT_UNAVAILABLE_MESSAGE))
        try:
            # state=form_token round-trips to the submit callback; keep the button
            # so a cancelled modal can be re-opened (the token lives until submit).
            await poster.open_dialog(
                cb.trigger, form.form, submit_url=self._dialog_submit_url, state=cb.form_token
            )
        except Exception:
            logger.exception("form %s: failed to open dialog", cb.form_token[:8])
            return web.json_response(self._codec.reply_notice(_AGENT_UNAVAILABLE_MESSAGE))
        logger.info("form %s: dialog opened", cb.form_token[:8])
        return web.json_response(self._codec.reply_none())  # no message change; modal is up

    async def _dialog(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        cb = self._codec.parse_dialog(body)
        await self._dispatcher.submit_form(cb.state, cb.submission, cb.cancelled, cb.user_id)
        return web.json_response(self._codec.reply_none())  # empty 200 → close the dialog
