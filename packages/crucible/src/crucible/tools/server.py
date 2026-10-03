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
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiohttp import web

from crucible.approvals.ports import ToolApproving
from crucible.approvals.preview import CallPreview
from crucible.ports.agent.runtime import TurnClock
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.files import FileService
from crucible.ports.chat.interactions import InteractionService
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

# Resolve a runtime session id -> (channel_id, last_user_id) for the current
# turn, or None if unknown. A plain callable so the tool layer never imports the
# store; the composition root supplies it from the session store.
SessionResolver = Callable[[str], Awaitable[tuple[str, str] | None]]

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
        allowlists: Mapping[str, frozenset[str]],  # agent name -> allowed tool names
        host: str = "127.0.0.1",
        port: int = 8422,
        tool_configs: Mapping[str, Any] | None = None,  # tool name -> its config
        interaction_svc: InteractionService | None = None,
        file_svc: FileService | None = None,
        task_svc: TaskService | None = None,
        session_resolver: SessionResolver | None = None,
        tool_gate: ToolApproving | None = None,
        clock: TurnClock | None = None,
        turns: TurnScopes | None = None,
        session_proofs: SessionProofBook | None = None,
    ) -> None:
        self._registry = registry
        self._directory = directory
        self._admins = admins
        self._tokens = tokens
        self._allowlists = allowlists
        self._host = host
        self._port = port
        self._tool_configs = tool_configs or {}
        self._interaction_svc = interaction_svc
        self._file_svc = file_svc
        self._task_svc = task_svc
        self._session_resolver = session_resolver
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

    async def _handle(self, request: web.Request) -> web.Response:
        agent = self._tokens.get(request.headers.get(_TOKEN_HEADER, ""))
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

        # May be None: a gateway without channel administration (e.g. Slack) has no
        # admin client. Tools that need it raise a ToolError; the rest ignore it.
        admin = self._admins.get(agent)

        try:
            args = await request.json()
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}

        runtime_session_id = request.headers.get(_SESSION_HEADER, "")
        if runtime_session_id and self._proofs is not None:
            if not self._proofs.verify(runtime_session_id, request.headers.get(_PROOF_HEADER, "")):
                logger.warning(
                    "tool %s: agent %s named session %s without its proof; refused",
                    tool.name, agent, runtime_session_id,
                )
                return web.json_response({"error": "session not proven"}, status=403)
        channel_id, user_id = "", ""
        if self._session_resolver is not None and runtime_session_id:
            resolved = await self._session_resolver(runtime_session_id)
            if resolved is not None:
                channel_id, user_id = resolved

        ctx = ToolContext(
            agent_name=agent,
            directory=self._directory,
            chat_admin=admin,
            settings=self._tool_configs.get(tool.name),
            runtime_session_id=runtime_session_id,
            interaction_svc=self._interaction_svc,
            file_svc=self._file_svc,
            task_svc=self._task_svc,
            channel_id=channel_id,
            user_id=user_id,
            turn=self._turns.current(runtime_session_id) if self._turns is not None else None,
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
                allowed = await self._tool_gate.confirm(
                    agent, tool.name, args, runtime_session_id=runtime_session_id,
                    preview=preview,
                )
            if not allowed:
                return web.json_response({"error": "declined by the user"}, status=403)
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
