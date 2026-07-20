"""ToolServer: a localhost HTTP receiver the tool extension calls into.

`POST /tool/{name}` with header `X-Tool-Token` (a per-agent secret the engine
minted and forwarded into that agent's runtime env). The token both authenticates
the caller and identifies WHICH agent is calling, so a tool acts as that agent
and a stray local process without the token can't reach the endpoint. Bound to
127.0.0.1 only.
"""

import logging
from collections.abc import Mapping
from typing import Any

from aiohttp import web

from crucible.ports.chat.admin import ChatAdmin
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.forms import FormService
from crucible.ports.chat.widgets import WidgetService
from crucible.tools.base import ToolContext, ToolError
from crucible.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

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
        widgets: WidgetService | None = None,
        forms: FormService | None = None,
    ) -> None:
        self._registry = registry
        self._directory = directory
        self._admins = admins
        self._tokens = tokens
        self._allowlists = allowlists
        self._host = host
        self._port = port
        self._tool_configs = tool_configs or {}
        self._widgets = widgets
        self._forms = forms
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/tool/{name}", self._handle)
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

        ctx = ToolContext(
            agent_name=agent,
            directory=self._directory,
            chat_admin=admin,
            settings=self._tool_configs.get(tool.name),
            runtime_session_id=request.headers.get(_SESSION_HEADER, ""),
            widgets=self._widgets,
            forms=self._forms,
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
