"""ToolServer: a localhost HTTP receiver the tool extension calls into.

`POST /tool/{name}` with header `X-Tool-Token` (a per-agent secret the engine
minted and forwarded into that agent's runtime env). The token both authenticates
the caller and identifies WHICH agent is calling, so a tool acts as that agent
and a stray local process without the token can't reach the endpoint. Bound to
127.0.0.1 only.

Secrets are deliberately absent. They used to be served from here, and the
reason they are not is the same reason this port binds loopback: loopback is
where the agents' shells are. The broker now runs in its own container beside
the store it opens, and an agent reaches it over mutual TLS.
"""

import asyncio
import contextlib
import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from crucible.approvals.ports import ConfirmOutcome, NotAsked, ToolApproving
from crucible.constant_time import find_by_secret
from crucible.ports.agent.runtime import TurnClock
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.files import FileService
from crucible.ports.chat.interactions import InteractionService
from crucible.ports.chat.types import CallPreview
from crucible.ports.connections import ConnectionAccessFactory
from crucible.ports.conversation import ConversationResolver
from crucible.ports.tasks import TaskService
from crucible.ports.turn import TurnScopes
from crucible.tools.base import (
    SPEAKS_TO_USER_NOTE,
    UNTRUSTED_NOTE,
    Describing,
    Tool,
    ToolContext,
    ToolError,
)
from crucible.tools.proofs import SessionProofBook
from crucible.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_TOKEN_HEADER = "X-Tool-Token"
# The engine ↔ tool-extension contract (not the runtime's — it only relays
# the env we inject). The engine sets RUNTIME_SESSION_ID in the runtime's child
# env; the extension forwards it as this header; here it becomes the
# runtime_session_id the store keys on. Part of the contract: the value IS the
# session record's ``runtime_session_id``, byte for byte — a tool, or whatever
# an application keeps per running turn, may look the conversation up by it.
_SESSION_HEADER = "X-Runtime-Session"
# The session's own secret, issued to its process at spawn: with a proof book
# wired, a session id is believed only when this matches what was issued.
_PROOF_HEADER = "X-Session-Proof"

# What the model is told when a confirmation does not end in a yes. Three
# sentences rather than one, because they call for different next steps: a
# person's no is final for this call; a person's silence is not a no — the
# model may raise it when they are back, or carry on with what needs no
# approval; nobody to ask is a fact about where the agent runs. A gate that
# cannot tell these apart teaches the model to read every absence as a refusal.
DECLINED_ERROR = "declined by the user"
UNANSWERED_ERROR = (
    "nobody answered the confirmation in time, so the call did not run. That is "
    "not a refusal: do not retry it now, but either tell the user you need their "
    "approval and ask them to say so when they are back, or go on with what "
    "does not need it"
)
NO_APPROVER_ERROR = (
    "cannot be confirmed here: there is nobody to ask. Tell the user, and do not retry"
)
_REFUSALS = {
    ConfirmOutcome.DENIED: DECLINED_ERROR,
    ConfirmOutcome.UNANSWERED: UNANSWERED_ERROR,
    ConfirmOutcome.NO_APPROVER: NO_APPROVER_ERROR,
}
# The gate's own refusal, with its reason: nobody was asked, so there is no
# person to come back to — the model tells the user and stops.
NOT_ASKED_ERROR = "not asked: {reason}. Tell the user, and do not retry"
# A tool the agent has and this conversation does not: said so, because the
# model may have read the tool's name somewhere and there is nothing to retry.
NOT_IN_CONVERSATION_ERROR = "not available in this conversation"
# The agent has the tool, but not through the gateway this conversation is in: a
# channel-admin tool asked for from a chat without channel administration.
NOT_THROUGH_GATEWAY_ERROR = "not available through this gateway"


def _log_if_orphaned(task: "asyncio.Future[Any]") -> None:
    """A tool that failed after its caller hung up has nobody to tell; say so
    in the log rather than let asyncio complain about an unread exception."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None and not isinstance(exc, ToolError):
        logger.warning("a tool failed after its caller had gone: %r", exc)


class ToolServer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        directory: AgentDirectory,
        admins: Mapping[str, ChatAdmin],
        tokens: Mapping[str, str],  # token -> agent name
        gateway_admins: Mapping[str, Mapping[str, ChatAdmin | None]] | None = None,
        gateway_caps: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
        allowlists: Mapping[str, frozenset[str]],  # agent name -> allowed tool names
        session_allowlists: Mapping[str, frozenset[str]] | None = None,
        host: str = "127.0.0.1",
        port: int = 8422,
        tool_configs: Mapping[str, Any] | None = None,  # tool name -> its config
        interaction_svc: InteractionService | None = None,
        file_svc: FileService | None = None,
        task_svc: TaskService | None = None,
        conversation_resolver: ConversationResolver | None = None,
        connections: ConnectionAccessFactory | None = None,
        tool_gate: ToolApproving | None = None,
        clock: TurnClock | None = None,
        turns: TurnScopes | None = None,
        session_proofs: SessionProofBook | None = None,
    ) -> None:
        self._registry = registry
        self._directory = directory
        self._admins = admins
        # By gateway, when the agent has several: a call is served with the
        # admin client and within the capabilities of the gateway the
        # conversation is in, not the best of the agent's.
        self._gateway_admins = gateway_admins or {}
        self._gateway_caps = gateway_caps or {}
        self._tokens = tokens
        self._allowlists = allowlists
        # The second door, behind the agent's: a conversation entitled to fewer
        # tools than its agent has an entry here (runtime session id -> its
        # tools), kept current by whoever builds the conversation's profile.
        # Checked once the session id is believed, so a process cannot step
        # out of its conversation's set by naming another conversation. Held
        # by reference, like `allowlists`: the mapping is empty when the server
        # is built and filled as conversations start, so `or {}` here would
        # quietly swap it for one nobody writes to.
        self._session_allowlists: Mapping[str, frozenset[str]] = (
            session_allowlists if session_allowlists is not None else {}
        )
        self._host = host
        self._port = port
        self._tool_configs = tool_configs or {}
        self._interaction_svc = interaction_svc
        self._file_svc = file_svc
        self._task_svc = task_svc
        # Which conversation a call runs inside, asked once per call with the
        # calling agent and its proven session (ports/conversation.py): a plain
        # callable, so the tool layer never imports the store.
        self._conversation_resolver = conversation_resolver
        # The connected accounts of the person a call's turn acts for.
        self._connections = connections
        # Asks a human before a tool that declares it runs — the only gate there
        # is; see interactions/toolgate.py for why it has to live here.
        self._tool_gate = tool_gate
        # Pauses the calling turn's timeout while the gate waits: the person
        # deciding is not the runtime being stuck. None = the turn keeps counting.
        self._clock = clock
        # Where the turn running on a session keeps what is its alone; a tool
        # finds it through the session id its call carries.
        self._turns = turns
        # Makes a call's session id a fact rather than a claim: the agent token
        # is shared by every process of that agent, so without this any of them
        # could name another conversation and reach what its turn holds.
        self._proofs = session_proofs
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/tool/{name}", self._handle)
        # A caller that hangs up cancels its handler: the runtime's extension
        # closes the connection when the turn is aborted (or gives up on a call),
        # and a confirmation still waiting on a person must then be withdrawn
        # rather than run later for a turn that is gone.
        self._runner = web.AppRunner(app, handler_cancellation=True)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(
            "tool server on http://%s:%d, tools: %s",
            self._host,
            self._port,
            ", ".join(self._registry.names()),
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _agent_for_token(self, presented: str) -> str | None:
        return find_by_secret(self._tokens.items(), presented)

    async def _handle(self, request: web.Request) -> web.Response:
        agent = self._agent_for_token(request.headers.get(_TOKEN_HEADER, ""))
        if agent is None:
            return web.json_response({"error": "unauthorized"}, status=401)

        name = request.match_info["name"]
        tool = self._registry.get(name)
        if tool is None:
            return web.json_response({"error": "unknown tool"}, status=404)

        # A valid token authorizes ONLY the tools in that agent's allowlist. The
        # per-agent manifest gates what the runtime advertises, but this is the
        # enforced server-side gate — otherwise any agent with a token could POST
        # any tool.
        if name not in self._allowlists.get(agent, frozenset()):
            return web.json_response({"error": "forbidden"}, status=403)

        try:
            args = await request.json()
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}

        runtime_session_id = request.headers.get(_SESSION_HEADER, "")
        if self._proofs is not None and not runtime_session_id:
            # Every process the runtime starts names a session and proves it,
            # a memoryless run included. A call naming none did not come from
            # one — a shell in the agent's container calling the server by
            # hand, with the token it found in the environment.
            logger.warning("tool %s: agent %s called without a session; refused", tool.name, agent)
            return web.json_response({"error": "session required"}, status=403)
        if runtime_session_id and self._proofs is not None:
            if not self._proofs.verify(runtime_session_id, request.headers.get(_PROOF_HEADER, "")):
                logger.warning(
                    "tool %s: agent %s named session %s without its proof; refused",
                    tool.name, agent, runtime_session_id,
                )
                return web.json_response({"error": "session not proven"}, status=403)
        scoped = self._session_allowlists.get(runtime_session_id) if runtime_session_id else None
        if scoped is not None and name not in scoped:
            # The agent has the tool; this conversation was not given it. The
            # process was told as much by its manifest, so a call that arrives
            # anyway is the model acting on a name it read elsewhere.
            logger.info(
                "tool %s: not in the set of conversation %s (agent %s); refused",
                tool.name, runtime_session_id, agent,
            )
            return web.json_response({"error": NOT_IN_CONVERSATION_ERROR}, status=403)
        conversation = (
            await self._conversation_resolver(agent, runtime_session_id)
            if self._conversation_resolver is not None and runtime_session_id else None
        )
        turn = self._turns.current(runtime_session_id) if self._turns is not None else None
        # The gateway the conversation is in decides what this call may do and
        # with which admin client. May be None: a gateway without channel
        # administration (e.g. Slack) has none; tools that need it raise a
        # ToolError, the rest ignore it.
        gateway = conversation.gateway if conversation else ""
        by_gateway = self._gateway_admins.get(agent, {})
        admin = by_gateway[gateway] if gateway in by_gateway else self._admins.get(agent)
        caps = self._gateway_caps.get(agent, {}).get(gateway)
        if caps is not None and tool.requires - caps:
            logger.info(
                "tool %s: conversation %s is on gateway %s, which lacks %s; refused",
                tool.name, runtime_session_id, gateway or "-", ", ".join(sorted(tool.requires - caps)),
            )
            return web.json_response({"error": NOT_THROUGH_GATEWAY_ERROR}, status=403)

        ctx = ToolContext(
            agent_name=agent,
            directory=self._directory,
            chat_admin=admin,
            settings=self._tool_configs.get(tool.name),
            runtime_session_id=runtime_session_id,
            interaction_svc=self._interaction_svc,
            file_svc=self._file_svc,
            task_svc=self._task_svc,
            channel_id=conversation.channel_id if conversation else "",
            user_id=conversation.user_id if conversation else "",
            files_dir=conversation.files_dir if conversation else None,
            conversation_kind=conversation.kind if conversation else "",
            direct=conversation.direct if conversation else False,
            connections=(
                self._connections(agent, conversation.user_id, conversation.gateway, turn)
                if self._connections is not None and conversation and conversation.user_id else None
            ),
            turn=turn,
        )

        # The confirmation a tool declares, enforced HERE and not in the
        # runtime's extension: the extension's token can be used by anything in
        # the agent's container that can reach this port, which is the same
        # shell the agent runs commands in.
        if tool.requires_confirmation:
            if self._tool_gate is None:
                # Fail closed. A composition with no way to ask cannot answer
                # "yes" on a human's behalf.
                logger.warning("tool %s needs a confirmation and there is no gate", tool.name)
                return web.json_response({"error": "cannot be confirmed here"}, status=403)
            pause = (
                self._clock.human_wait(runtime_session_id)
                if self._clock is not None
                else contextlib.nullcontext()
            )
            async with pause:
                # The preview reads the system (a record, a repository) and is
                # part of asking: it belongs to the paused time too.
                preview = await self._preview_of(tool, ctx, args)
                outcome = await self._tool_gate.confirm(
                    agent, tool.name, args, runtime_session_id=runtime_session_id,
                    preview=preview, display_name=tool.display_name,
                )
            if isinstance(outcome, NotAsked):
                return web.json_response(
                    {"error": NOT_ASKED_ERROR.format(reason=outcome.reason)}, status=403
                )
            if outcome is not ConfirmOutcome.ALLOWED:
                return web.json_response({"error": _REFUSALS[outcome]}, status=403)
            if request.transport is None or request.transport.is_closing():
                # Approved, but for nobody: the caller left while the card was
                # up and the cancellation has not reached this handler yet.
                logger.warning("tool %s was approved after its caller hung up; not run", tool.name)
                return web.json_response({"error": "abandoned by the caller"}, status=403)

        try:
            # Once started, a tool runs to its end: a half-done write because
            # the caller hung up mid-way is worse than a finished one nobody
            # reads. (The wait for a person, above, is what cancellation is for.)
            running = asyncio.ensure_future(tool.execute(ctx, args))
            running.add_done_callback(_log_if_orphaned)
            result = await asyncio.shield(running)
        except ToolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("tool %s crashed (agent %s)", tool.name, agent)
            return web.json_response({"error": "internal tool error"}, status=500)

        logger.info("tool %s ran for agent %s", tool.name, agent)
        # Beside the result, never merged into it: a tool's own result stays its
        # own shape (open_screen already returns keys of its own), and callers
        # that read `result` are unaffected. The description carries the same
        # sentence, but it is read once at registration — this one arrives at the
        # moment the model is deciding whether to write anything else.
        if tool.returns_untrusted:
            # Wrapped, not merely annotated: the boundary around what strangers
            # wrote has to be visible where the text is, so the model can tell
            # the tool's own fields from the quoted ones.
            result = {"untrusted": True, "note": UNTRUSTED_NOTE, "data": result}
        body: dict[str, Any] = {"result": result}
        if tool.speaks_to_user:
            body["note"] = SPEAKS_TO_USER_NOTE
        return web.json_response(body)

    @staticmethod
    async def _preview_of(
        tool: Tool, ctx: ToolContext, args: dict[str, Any]
    ) -> CallPreview | None:
        """What the tool says the call would do, or None — a preview that
        fails must not stand between the person and the decision; the
        arguments are still an honest description of the call."""
        if not isinstance(tool, Describing):
            return None
        try:
            return await tool.describe(ctx, args)
        except Exception:
            logger.exception("tool %s could not describe its call; showing the arguments", tool.name)
            return None
