"""The tools an MCP client sees on the gateway, and the one that comes with it.

A gateway tool is the application's: a name, a description and a schema for
the client's model, a scope it needs, and ``run`` over a ``GatewayCall`` — the
principal, the arguments, and two things only the gateway can do: ``ask`` an
agent in the conversation the call belongs to, and tell the client of
``progress`` while the call runs. ``AskAgentTool`` is the tool a deployment
starts with: the agent itself, as a tool.

Conversations: a call names the conversation it continues by the handle the
gateway gave an earlier call (``_meta`` of the call and of its result); a call
with none that asks an agent opens a new conversation and answers with its
handle. The conversation is the principal's: the same handle presented for
another subject is another conversation.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from crucible.gateways.mcp.auth import SCOPE_READ, Principal

# The ``_meta`` key, on a call and on its result, that names the conversation.
META_CONVERSATION = "crucible/conversation"


class CallFailed(Exception):
    """The call did not do what was asked — the tool says why in words for the
    client's model (``isError`` on the result)."""


class GatewayCall:
    """One ``tools/call`` as a gateway tool sees it."""

    def __init__(
        self,
        principal: Principal,
        arguments: Mapping[str, Any],
        handle: str,
        *,
        asker: Callable[["GatewayCall", str, str], Awaitable[str]],
        progress: Callable[[str], Awaitable[None]],
        wants_progress: bool = False,
    ) -> None:
        self.principal = principal
        self.arguments = arguments
        # The conversation this call belongs to: "" until an agent is asked.
        self.handle = handle
        # Whether the client asked to be told of progress (a progress token on
        # the call, an event stream accepted): the gateway follows the turn's
        # steps for it only then.
        self.wants_progress = wants_progress
        self._asker = asker
        self._progress = progress

    async def ask(self, text: str, *, agent: str = "") -> str:
        """Put ``text`` to ``agent`` (the gateway's only agent when "") as the
        principal's message in the call's conversation — opening one when the
        call named none — and answer with what the agent said. Raises
        ``CallFailed`` when the turn failed, the conversation is busy with
        another call, or no such agent runs here."""
        return await self._asker(self, text, agent)

    async def progress(self, text: str) -> None:
        """A word to the client on what the call is doing. Nothing happens when
        the client did not ask for progress."""
        await self._progress(text)


class McpGatewayTool(Protocol):
    """A tool the gateway offers MCP clients. ``input_schema`` is the JSON
    schema of ``arguments``; ``scope`` the one a token needs to see and call
    it (``SCOPE_READ``, ``SCOPE_ACT``); ``requires_task`` whether the call must
    run as a task (a long one, one that may wait on a person) — a client that
    cannot follow a task is refused it."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    scope: str
    requires_task: bool

    async def run(self, call: GatewayCall) -> str: ...


class AskAgentTool:
    """The agent as a tool: the client's model asks, the agent answers, in a
    conversation the agent remembers across calls."""

    name = "ask_agent"
    description = (
        "Ask the agent a question or give it a task, in words. The agent answers "
        "with everything it knows and can do; it remembers the conversation, so "
        "a follow-up needs no repetition."
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to ask or tell the agent."},
            "agent": {
                "type": "string",
                "description": "Which agent, when this gateway has several; leave out when it has one.",
            },
        },
        "required": ["text"],
    }
    scope = SCOPE_READ
    requires_task = False

    async def run(self, call: GatewayCall) -> str:
        text = str(call.arguments.get("text", "")).strip()
        if not text:
            raise CallFailed("say what to ask: `text` is empty")
        return await call.ask(text, agent=str(call.arguments.get("agent", "") or ""))
