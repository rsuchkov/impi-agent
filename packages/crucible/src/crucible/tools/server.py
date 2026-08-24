"""ToolServer: a localhost HTTP receiver the tool extension calls into.

`POST /tool/{name}` with header `X-Tool-Token` (a per-agent secret the engine
minted and forwarded into that agent's runtime env). The token both authenticates
the caller and identifies WHICH agent is calling, so a tool acts as that agent
and a stray local process without the token can't reach the endpoint. Bound to
127.0.0.1 only.

Three further routes serve the secret broker. `POST /secrets/lease` is a tool
call in everything but name — same token, same agent identity, same session
header — and lives here rather than on the public receiver precisely because it
must not be reachable from outside the container. `POST /secrets/unlock` and
`GET /secrets/status` carry no token: unlocking is authenticated by knowing the
key, and the status says only whether the engine can serve, never what it holds.

What is deliberately NOT here: any route that reads a value, lists names, or
edits a policy. Loopback is where the agents' shells are, so a route reachable
by a local process is a route reachable by an agent. Those verbs belong to the
operator CLI, which talks to the backend directly with the operator's own
credential.
"""

import ipaddress
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiohttp import web

from crucible.approvals.ports import ToolApproving
from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.files import FileService
from crucible.ports.chat.interactions import InteractionService
from crucible.ports.tasks import TaskService
from crucible.secrets.ports import (
    LeaseRequest,
    SecretLeasing,
    UnlockMaterial,
    parse_ref,
    wire_status,
)
from crucible.tools.base import ToolContext, ToolError
from crucible.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Resolve a runtime session id -> (channel_id, last_user_id) for the current
# turn, or None if unknown. A plain callable so the tool layer never imports the
# store; the composition root supplies it from the session store.
SessionResolver = Callable[[str], Awaitable[tuple[str, str] | None]]

_TOKEN_HEADER = "X-Tool-Token"
# The engine ↔ tool-extension contract (not the runtime's — it only relays
# the env we inject). The engine sets RUNTIME_SESSION_ID in the runtime's child
# env; the extension forwards it as this header; here it becomes the runtime_session_id the
# store keys on.
_SESSION_HEADER = "X-Runtime-Session"


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
        secret_svc: SecretLeasing | None = None,
        tool_gate: ToolApproving | None = None,
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
        self._secret_svc = secret_svc
        # Asks a human before a tool that declares it runs. See the note in
        # interactions/toolgate.py for why the runtime-side gate is not enough.
        self._tool_gate = tool_gate
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/tool/{name}", self._handle)
        app.router.add_post("/secrets/lease", self._lease)
        app.router.add_post("/secrets/unlock", self._unlock)
        app.router.add_get("/secrets/status", self._secret_status)
        self._runner = web.AppRunner(app)
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
        channel_id, user_id = "", ""
        if self._session_resolver is not None and runtime_session_id:
            resolved = await self._session_resolver(runtime_session_id)
            if resolved is not None:
                channel_id, user_id = resolved

        # The confirmation a tool declares, enforced HERE and not only in the
        # runtime's extension: the extension's gate can be walked around by
        # anything in the agent's container that can reach this port, which is
        # the same shell the agent runs commands in.
        if tool.requires_confirmation:
            if self._tool_gate is None:
                # Fail closed. A composition with no way to ask cannot answer
                # "yes" on a human's behalf, and the runtime's own backstop
                # already refuses for the same reason.
                logger.warning("tool %s needs a confirmation and there is no gate", tool.name)
                return web.json_response({"error": "cannot be confirmed here"}, status=403)
            if not await self._tool_gate.confirm(
                agent, tool.name, args, runtime_session_id=runtime_session_id
            ):
                return web.json_response({"error": "declined by the user"}, status=403)

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
        )
        try:
            result = await tool.execute(ctx, args)
        except ToolError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("tool %s crashed (agent %s)", tool.name, agent)
            return web.json_response({"error": "internal tool error"}, status=500)

        logger.info("tool %s ran for agent %s", tool.name, agent)
        return web.json_response({"result": result})

    # -- the secret broker ----------------------------------------------------

    async def _lease(self, request: web.Request) -> web.Response:
        """Ask for a secret on behalf of the calling agent.

        The response carries a value or one of two words. It never carries the
        reason a request was turned down: a caller that could tell "no such
        secret" from "not yours" could map the store by trying names.
        """
        agent = self._tokens.get(request.headers.get(_TOKEN_HEADER, ""))
        if agent is None:
            return web.json_response({"error": "unauthorized"}, status=401)
        if self._secret_svc is None:
            return web.json_response({"error": "secrets are not enabled"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            lease = _lease_request(agent, request.headers.get(_SESSION_HEADER, ""), body)
        except ValueError as exc:
            # A malformed request, not a refusal — the caller built it wrong and
            # needs to be told which part.
            return web.json_response({"error": str(exc)}, status=400)

        result = await self._secret_svc.lease(lease)
        if not result.granted:
            return web.json_response(
                {"granted": False, "status": wire_status(result.decision)}
            )
        return web.json_response({"granted": True, "values": dict(result.values)})

    async def _unlock(self, request: web.Request) -> web.Response:
        """Hand the engine the material that makes the backend usable.

        No token: knowing the key IS the authentication, and there is nothing a
        caller can learn by trying. Loopback only, so this is the operator CLI
        running inside the container and nothing else.
        """
        if not _is_loopback(request):
            return web.json_response({"error": "not found"}, status=404)
        if self._secret_svc is None:
            return web.json_response({"error": "secrets are not enabled"}, status=404)
        try:
            body = await request.json()
        except Exception:
            body = {}
        material = UnlockMaterial(
            unseal_key=str(body.get("unseal_key") or ""),
            auth_secret=str(body.get("auth_secret") or ""),
        )
        if not material:
            return web.json_response({"error": "no unlock material"}, status=400)
        try:
            state = await self._secret_svc.unlock(material)
        except Exception as exc:
            logger.warning("secrets: unlock failed")
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response(_status_payload(state))

    async def _secret_status(self, request: web.Request) -> web.Response:
        if not _is_loopback(request):
            return web.json_response({"error": "not found"}, status=404)
        if self._secret_svc is None:
            return web.json_response({"enabled": False})
        state = await self._secret_svc.status()
        return web.json_response({"enabled": True, **_status_payload(state)})


def _lease_request(agent: str, runtime_session_id: str, body: Any) -> LeaseRequest:
    """Turn the wire body into a LeaseRequest, or say what is wrong with it."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    raw = body.get("bindings")
    if not isinstance(raw, list) or not raw:
        raise ValueError("bindings must be a non-empty list")
    bindings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each binding is an object with 'env' and 'ref'")
        env_name = str(item.get("env") or "")
        if not env_name.isidentifier():
            raise ValueError(f"not an environment variable name: {env_name!r}")
        bindings.append((env_name, parse_ref(str(item.get("ref") or ""))))
    command = body.get("command")
    return LeaseRequest(
        agent=agent,
        runtime_session_id=runtime_session_id,
        bindings=tuple(bindings),
        reason=str(body.get("reason") or ""),
        command=tuple(str(part) for part in command) if isinstance(command, list) else (),
    )


def _status_payload(state) -> dict[str, Any]:
    return {
        "reachable": state.reachable,
        "sealed": state.sealed,
        "authenticated": state.authenticated,
        "usable": state.usable,
        "detail": state.detail,
    }


def _is_loopback(request: web.Request) -> bool:
    """Whether the caller is on this machine. The server binds 127.0.0.1 by
    default, but the bind address is configurable, so the untokenized routes
    check rather than assume."""
    remote = request.remote or ""
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False
