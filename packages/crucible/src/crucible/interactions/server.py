"""InteractionsServer: the HTTP-callback receiver for interactive widgets.

A gateway that delivers interactions as HTTP callbacks (e.g. Mattermost) points
its widget/form callbacks at this server. It is transport-neutral: an injected
``CallbackCodec`` translates the platform's payload/response shapes, and the
``InteractionDispatcher`` performs the neutral resolve-or-feed-back logic. Bound
on 0.0.0.0 (the platform reaches it from its own network); separate from the
localhost tool server on purpose — only this door is exposed, and only for
callbacks.

The platform is the only party meant to reach it, and a platform's callback
carries no signature — the click says who clicked and the receiver has to
believe it. So the proof that a request came from the platform is the URL
itself: a secret segment on every path, handed to the platform when a card is
posted or a command registered, and checked here before anything is routed.
Off that path the receiver answers 403 and nothing else.
"""

import logging
from collections.abc import Awaitable, Callable

from aiohttp import web
from aiohttp.abc import AbstractAccessLogger

from crucible.approvals import ApprovalOutcome
from crucible.constant_time import matches_any
from crucible.interactions.callbacks import CallbackCodec
from crucible.interactions.dispatcher import InteractionDispatcher
from crucible.interactions.presence import AgentPresence
from crucible.interactions.results import ActionResult
from crucible.ports.chat.types import (
    AGENT_UNAVAILABLE_TEXT,
    BUTTONS_RETIRED_TEXT,
    COMMAND_ACK_TEXT,
    KIND_CHANNEL,
    KIND_THREAD,
    NOT_AN_APPROVER_TEXT,
)
from crucible.receiver_paths import callback_path

logger = logging.getLogger(__name__)

# User-facing chrome for the button message (engine text, not agent persona) —
# English per project convention. The rest of the wording a click or a command
# is answered with is shared with the socket gateways: ports.chat.types.
_CHOSE_PREFIX = "Selected: "

# Which command tokens an agent accepts; empty tuple = commands are off for it.
CommandTokens = Callable[[str], tuple[str, ...]]
# The agents this engine runs. Read when a command arrives, not captured, so the
# app stays the single owner of that list.
LiveAgents = Callable[[], tuple[str, ...]]

# The path segment that means "whichever agent is the default one". Reserved by
# convention only: an agent really called `default` still wins, so no existing
# registration can change meaning.
DEFAULT_AGENT_PATH = "default"


def resolve_command_agent(
    requested: str, *, agents: tuple[str, ...], default: str
) -> str:
    """Which agent a command sent to ``/command/<requested>`` runs as; ``""`` when
    the engine cannot name one.

    A single-agent deployment should not have to spell its agent's name in a URL,
    and neither should one that has a default. Anything other than the reserved
    word is taken at face value — including a real agent named ``default``, which
    keeps this from changing what an existing registration means."""
    if requested != DEFAULT_AGENT_PATH or requested in agents:
        return requested
    if len(agents) == 1:
        return agents[0]
    # With several agents the choice must be configured, and it must be one that
    # actually runs — pointing commands at an agent with no token would only
    # produce "unavailable" on every invocation.
    return default if default in agents else ""


class InteractionsServer:
    def __init__(
        self,
        dispatcher: InteractionDispatcher,
        codec: CallbackCodec,
        presence: AgentPresence,
        *,
        host: str = "0.0.0.0",
        port: int = 8423,
        secret: str = "",
        secret_setting: str = "INTEGRATIONS_SECRET",
        dialog_submit_url: str = "",
        command_tokens: CommandTokens | None = None,
        agents: LiveAgents | None = None,
        default_agent: str = "",
    ) -> None:
        self._dispatcher = dispatcher
        self._codec = codec
        self._presence = presence
        self._host = host
        self._port = port
        self._secret = secret
        # The setting an operator changes to fix a refused callback — the
        # application's own name for it, so the log points at the right file.
        self._secret_setting = secret_setting
        self._dialog_submit_url = dialog_submit_url
        self._command_tokens = command_tokens
        self._agents = agents
        self._default_agent = default_agent
        self._runner: web.AppRunner | None = None

    def _agent_for(self, requested: str) -> str:
        if self._agents is None:
            return requested  # no roster to resolve against: the path is all we have
        return resolve_command_agent(
            requested, agents=self._agents(), default=self._default_agent
        )

    async def start(self) -> None:
        # With a secret, every route lives under it and the guard runs before
        # any handler; without one the bare routes answer, and the log says
        # what that means.
        app = web.Application(middlewares=[self._require_secret_path] if self._secret else [])
        segment = "{secret}" if self._secret else ""
        app.router.add_post(callback_path("interact", secret=segment), self._interact)
        app.router.add_post(callback_path("dialog", secret=segment), self._dialog)
        # Slash commands are per-agent: the platform's command is registered with
        # the agent's own URL, so the path says whom to run.
        app.router.add_post(callback_path("command/{agent}", secret=segment), self._command)
        # aiohttp's access log writes every request's path — the secret, on
        # every click. The receiver's own logger masks it.
        self._runner = web.AppRunner(app, access_log_class=_masking_access_log(self._secret))
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        shape = callback_path("", secret="<secret>" if self._secret else "").rstrip("/")
        logger.info(
            "integrations receiver on http://%s:%d (%s/interact, %s/dialog, "
            "%s/command/{agent}); %s/command/%s -> %s",
            self._host, self._port, shape, shape, shape, shape, DEFAULT_AGENT_PATH,
            self._agent_for(DEFAULT_AGENT_PATH) or "nothing (name one with AGENT_NAME)",
        )
        if not self._secret:
            logger.warning(
                "the receiver has no secret: every callback that reaches port %d is "
                "trusted on arrival — set %s",
                self._port, self._secret_setting,
            )

    @web.middleware
    async def _require_secret_path(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        """The one check that stands in for a signature: the path's secret
        segment. Runs for every request, including those no route matched —
        a bare ``/interact`` is as much \"off the path\" as a wrong secret."""
        # The first segment of the path, whatever route did or did not match:
        # the right secret with a wrong method or endpoint is a 404/405 from
        # aiohttp, not a refusal — and must never be what the log prints.
        _, _, rest = request.path.partition("/")
        presented, _, _ = rest.partition("/")
        if not matches_any([self._secret], presented):
            logger.warning(
                "callback to %s refused: not on the receiver's secret path — a card "
                "posted or a slash command registered before %s was set or changed "
                "still points at the old URL; register it again under the new one",
                # Refused means the first segment is not the secret; the path
                # is logged as it came, with the secret masked should it
                # appear further along.
                request.path.replace(self._secret, "<secret>") if self._secret else request.path,
                self._secret_setting,
            )
            return web.json_response({"error": "forbidden"}, status=403)
        return await handler(request)

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
        # The token is the credential the click carries; eight characters say
        # which card without letting the log replay the click.
        logger.info("interact callback: card %s… form=%s", cb.token[:8], bool(cb.form_token))

        # A request for AUTHORIZATION — a credential, a gated tool call. First,
        # because it is the only click whose answer may depend on WHO sent it;
        # the blocking confirm below takes any click that carries its token and
        # leaves nothing behind. Whatever raised the request rewrites its own
        # card once it has the answer, so nothing is replaced from here.
        if cb.approval:
            outcome = self._dispatcher.resolve_approval(
                cb.approval, cb.value, cb.user_id
            )
            if outcome is ApprovalOutcome.NOT_ALLOWED:
                return web.json_response(self._codec.reply_notice(NOT_AN_APPROVER_TEXT))
            if outcome is ApprovalOutcome.RESOLVED:
                return web.json_response(self._codec.reply_none())
            return web.json_response(self._codec.reply_replace(BUTTONS_RETIRED_TEXT))

        # A screen redraws itself in place: no turn, and the message it came from
        # is rewritten rather than replaced by a response body.
        if cb.screen:
            await self._dispatcher.redraw_screen(
                cb.state, cb.value, post_id=cb.post_id, user_id=cb.user_id
            )
            return web.json_response(self._codec.reply_none())

        # Form-open click: open the modal synchronously — the trigger expires within
        # seconds.
        if cb.form_token:
            return await self._open_form_dialog(cb)

        # Blocking UI request (a mid-turn confirm/select — ask_user_confirm, or
        # the confirmation a tool declares): resolve the Future the paused turn
        # is waiting on. Must come before the fire-and-forget path, which uses a
        # different token store.
        if self._dispatcher.resolve_pending(cb.token, cb.value):
            return web.json_response(self._codec.reply_replace(f"{_CHOSE_PREFIX}{cb.value}"))

        result = await self._dispatcher.consume_action(
            cb.token, cb.value, cb.user_id, pick=cb.pick
        )
        if result is ActionResult.UNKNOWN:
            return web.json_response(self._codec.reply_replace(BUTTONS_RETIRED_TEXT))
        if result is ActionResult.UNAVAILABLE:
            return web.json_response(self._codec.reply_notice(AGENT_UNAVAILABLE_TEXT))
        return web.json_response(self._codec.reply_replace(f"{_CHOSE_PREFIX}{cb.value}"))

    async def _open_form_dialog(self, cb) -> web.Response:
        form = await self._dispatcher.load_form(cb.form_token)
        if form is None:
            return web.json_response(self._codec.reply_replace(BUTTONS_RETIRED_TEXT))
        poster = self._presence.poster(form.agent)
        if poster is None or not cb.trigger:
            return web.json_response(self._codec.reply_notice(AGENT_UNAVAILABLE_TEXT))
        try:
            # state=form_token round-trips to the submit callback; keep the button
            # so a cancelled modal can be re-opened (the token lives until submit).
            await poster.open_dialog(
                cb.trigger, form.form, submit_url=self._dialog_submit_url, state=cb.form_token
            )
        except Exception:
            logger.exception("form %s: failed to open dialog", cb.form_token[:8])
            return web.json_response(self._codec.reply_notice(AGENT_UNAVAILABLE_TEXT))
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

    async def _command(self, request: web.Request) -> web.Response:
        """A slash command for one agent: verify it, start the turn, answer at
        once. The turn takes as long as it takes and posts its own reply into the
        conversation — this response is only the receipt (ephemeral, so the
        receipt itself doesn't clutter the thread)."""
        requested = request.match_info["agent"]
        # Commands arrive form-encoded (Mattermost), unlike the JSON callbacks.
        try:
            body = dict(await request.post())
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        cb = self._codec.parse_command(body)

        agent = self._agent_for(requested)
        if not agent:
            # Only reachable through the reserved word, and only as a
            # misconfiguration: several agents run and none of them is the named
            # default. Say which knob fixes it rather than guessing an owner.
            logger.warning(
                "command %s: /command/%s resolves to no agent — set AGENT_NAME to "
                "one of the running agents", cb.command, requested,
            )
            return web.json_response({"error": "no default agent"}, status=404)

        allowed = self._command_tokens(agent) if self._command_tokens else ()
        if not matches_any(allowed, cb.token):
            # Anything that can reach this port could otherwise run a turn as the
            # agent: an unconfigured agent or a wrong token is a hard stop.
            logger.warning("command %s for %s: rejected (token mismatch)", cb.command, agent)
            return web.json_response({"error": "forbidden"}, status=403)

        # A command typed in a thread belongs to that thread; outside one it runs
        # as the channel's own conversation.
        conversation_id = cb.root_id or cb.channel_id
        kind = KIND_THREAD if cb.root_id else KIND_CHANNEL
        text = f"{cb.command} {cb.text}".strip()

        # Engine-answered commands (the skill library, …) never reach an agent:
        # the answer is a fact, and a model would only slow it down.
        opened = await self._dispatcher.open_screen(
            agent, cb.command,
            channel_id=cb.channel_id, conversation_id=conversation_id,
            kind=kind, user_id=cb.user_id,
        )
        if opened.owned:
            # A screen that refused was not drawn, so the reason has nowhere to
            # be except here — an ephemeral answer to whoever typed the command,
            # which is also the only place it belongs.
            if opened.refused:
                # reply_ack, not reply_notice: this answers the command POST,
                # where the ephemeral shape is the platform's own — a notice is
                # what a CLICK is answered with, and the two are not the same
                # payload.
                return web.json_response(self._codec.reply_ack(opened.refused))
            return web.json_response(self._codec.reply_none())
        result = self._dispatcher.invoke_command(
            agent,
            channel_id=cb.channel_id,
            conversation_id=conversation_id,
            kind=kind,
            text=text,
            user_id=cb.user_id,
            username=cb.user_name,
        )
        if result is ActionResult.UNAVAILABLE:
            logger.warning("command %s: agent %s has no live presence", cb.command, agent)
            return web.json_response(self._codec.reply_ack(AGENT_UNAVAILABLE_TEXT))
        return web.json_response(self._codec.reply_ack(COMMAND_ACK_TEXT))


def _masking_access_log(secret: str) -> type[AbstractAccessLogger]:
    """An access logger that writes the path with the receiver's secret
    segment replaced by ``<secret>`` — the shape aiohttp's default logs, minus
    the one thing in it that is a credential."""

    class _MaskingAccessLog(AbstractAccessLogger):
        def log(self, request: web.BaseRequest, response: web.StreamResponse, time: float) -> None:
            path = request.path
            if secret:
                path = path.replace(f"/{secret}/", "/<secret>/", 1)
            self.logger.info(
                '%s "%s %s" %s %.3fs', request.remote, request.method, path, response.status, time
            )

    return _MaskingAccessLog
