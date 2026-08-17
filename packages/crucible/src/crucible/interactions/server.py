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
from collections.abc import Callable

from aiohttp import web

from crucible.interactions.callbacks import CallbackCodec
from crucible.interactions.dispatcher import ActionResult, InteractionDispatcher
from crucible.interactions.presence import AgentPresence
from crucible.ports.chat.types import KIND_CHANNEL, KIND_THREAD
from crucible.secrets.approvals import SecretApprovalOutcome

logger = logging.getLogger(__name__)

# User-facing chrome for the button message (engine text, not agent persona) —
# English per project convention.
_BUTTONS_RETIRED_MESSAGE = "These buttons are no longer active."
_AGENT_UNAVAILABLE_MESSAGE = "The agent is currently unavailable."
_CHOSE_PREFIX = "Selected: "
# Shown to whoever clicked a credential request they are not an approver for.
# Ephemeral: the card stays live for the person it was actually addressed to.
_NOT_AN_APPROVER_MESSAGE = "Only an approver can answer that request."
_COMMAND_ACK_MESSAGE = "Working on it — the answer will appear in this conversation."

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
        app = web.Application()
        app.router.add_post("/interact", self._interact)
        app.router.add_post("/dialog", self._dialog)
        # Slash commands are per-agent: the platform's command is registered with
        # the agent's own URL, so the path says whom to run.
        app.router.add_post("/command/{agent}", self._command)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        logger.info(
            "integrations receiver on http://%s:%d (/interact, /dialog, /command/{agent}); "
            "/command/%s -> %s",
            self._host, self._port, DEFAULT_AGENT_PATH,
            self._agent_for(DEFAULT_AGENT_PATH) or "nothing (name one with AGENT_NAME)",
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

        # A request for a CREDENTIAL, answered by a named person. First, because
        # it is the only click whose answer depends on WHO sent it: the blocking
        # confirm below (ask_user_confirm, and the gate in front of a tool call)
        # is addressed to the conversation and takes any click that carries its
        # token. The broker rewrites its own card once it has the answer, so
        # nothing is replaced from here.
        if cb.secret_approval:
            outcome = self._dispatcher.resolve_secret_approval(
                cb.secret_approval, cb.value, cb.user_id
            )
            if outcome is SecretApprovalOutcome.NOT_ALLOWED:
                return web.json_response(self._codec.reply_notice(_NOT_AN_APPROVER_MESSAGE))
            if outcome is SecretApprovalOutcome.RESOLVED:
                return web.json_response(self._codec.reply_none())
            return web.json_response(self._codec.reply_replace(_BUTTONS_RETIRED_MESSAGE))

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
        if not allowed or cb.token not in allowed:
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
        if await self._dispatcher.open_screen(
            agent, cb.command,
            channel_id=cb.channel_id, conversation_id=conversation_id,
            kind=kind, user_id=cb.user_id,
        ):
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
            return web.json_response(self._codec.reply_ack(_AGENT_UNAVAILABLE_MESSAGE))
        return web.json_response(self._codec.reply_ack(_COMMAND_ACK_MESSAGE))
