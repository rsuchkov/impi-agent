"""The tools an MCP client sees on the gateway, and the two that come with it.

A gateway tool is the application's: a name, a description and a schema for
the client's model, a scope it needs, and ``run`` over a ``GatewayCall`` — the
caller, the arguments, and what only the gateway can do: ``ask`` an agent in
the conversation the call belongs to, ``wait`` for a turn there, tell the
client of ``progress``. ``AskAgentTool`` and ``WaitAgentTool`` are the tools a
deployment starts with: the agent itself, as a tool, and the way to come back
for an answer that took longer than a call may.

Conversations: a tool names the argument that carries the conversation's
handle (``conversation_argument``); a call with none that asks an agent opens
a new conversation, and the result says its handle — in the text, where the
client's model reads it, and in ``structuredContent``. The conversation is the
caller's: the same handle presented for another subject is another
conversation.

Long calls: a tool that runs an agent's turn (``long_running``) may take
minutes and may stop to ask the person. The gateway runs such a call as a
task for a client that declared the tasks extension, and for any other client
waits as long as a call may and then answers with where the turn stands
(``TurnState``) — the client comes back with ``wait_agent``.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from crucible.gateways.mcp.auth import McpCaller
from crucible.ports.chat.gateway import MCP_SCOPE_READ
from crucible.ports.mcp_turns import ENDED, STATE_AWAITING_APPROVAL, STATE_DONE

# The ``_meta`` key, on a call and on its result, that names the conversation
# — beside the tool's own argument and the result's ``structuredContent``.
META_CONVERSATION = "crucible/conversation"
# The result's ``structuredContent`` keys.
CONVERSATION_KEY = "conversation"
STATE_KEY = "state"
STEP_KEY = "step"


class CallFailed(Exception):
    """The call did not do what was asked — the tool says why in words for the
    client's model (``isError`` on the result)."""


@dataclass(frozen=True)
class TurnState:
    """Where a conversation's latest turn stands, for a tool that waits on it:
    the state word (``ports.mcp_turns.STATE_*``), the latest step while it
    runs, and once it has ended what it answered and whether that was an error."""

    handle: str
    state: str
    step: str = ""
    answer: str = ""
    is_error: bool = False


class GatewayCall:
    """One ``tools/call`` as a gateway tool sees it."""

    def __init__(
        self,
        caller: McpCaller,
        arguments: Mapping[str, Any],
        handle: str,
        *,
        ask_agent: Callable[["GatewayCall", str, str], Awaitable[str]],
        wait_turn: Callable[["GatewayCall", str], Awaitable[TurnState]],
        progress: Callable[[str], Awaitable[None]],
        wants_progress: bool = False,
    ) -> None:
        self.caller = caller
        self.arguments = arguments
        # The conversation this call belongs to: from the tool's argument or
        # the call's ``_meta``; "" until an agent is asked, which opens one.
        self.handle = handle
        # The turn this call is, once the gateway runs it as one (a long call):
        # what a client polls it by. "" for a call that is no turn.
        self.turn_id = ""
        # Whether the client asked to be told of progress (a progress token on
        # the call, an event stream accepted): the gateway follows the turn's
        # steps for it only then.
        self.wants_progress = wants_progress
        self._ask_agent = ask_agent
        self._wait_turn = wait_turn
        self._progress = progress

    async def ask(self, text: str, *, agent: str = "") -> str:
        """Put ``text`` to ``agent`` (the gateway's only agent when "") as the
        caller's message in the call's conversation — opening one when the
        call named none — and answer with what the agent said. Raises
        ``CallFailed`` when the turn failed, or no such agent runs here. One
        ask per call: the call is the turn."""
        return await self._ask_agent(self, text, agent)

    async def wait(self, handle: str) -> TurnState:
        """Where the conversation ``handle`` stands: its latest turn's state,
        after waiting as long as a call may for it to end or change. Raises
        ``CallFailed`` for a handle that names no conversation of the caller's."""
        return await self._wait_turn(self, handle)

    async def progress(self, text: str) -> None:
        """A word to the client on what the call is doing. Nothing happens when
        the client did not ask for progress."""
        await self._progress(text)


class McpGatewayTool(Protocol):
    """A tool the gateway offers MCP clients. ``input_schema`` is the JSON
    schema of ``arguments``; ``scope`` the one a token needs to see and call
    it (``MCP_SCOPE_READ``, ``MCP_SCOPE_ACT``); ``long_running`` whether the call
    runs an agent's turn — minutes, perhaps a wait on the person — so the
    gateway runs it as a task or answers with where it stands rather than
    holding the call open; ``conversation_argument`` the name of the argument
    that carries the conversation's handle ("" when the tool has none)."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    scope: str
    long_running: bool
    conversation_argument: str

    async def run(self, call: GatewayCall) -> str: ...


_CONVERSATION_SCHEMA = {
    "type": "string",
    "description": (
        "The conversation to go on in: the `conversation` an earlier result gave. Leave out to start a new one."
    ),
}


class AskAgentTool:
    """The agent as a tool: the client's model asks, the agent answers, in a
    conversation the agent remembers across calls."""

    name = "ask_agent"
    description = (
        "Ask the agent a question or give it a task, in words. The agent answers "
        "with everything it knows and can do; it remembers the conversation, so "
        "a follow-up with the same `conversation` needs no repetition. An answer "
        "that takes longer than this call comes back through `wait_agent`."
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to ask or tell the agent."},
            "agent": {
                "type": "string",
                "description": "Which agent, when this gateway has several; leave out when it has one.",
            },
            CONVERSATION_KEY: _CONVERSATION_SCHEMA,
        },
        "required": ["text"],
    }
    scope = MCP_SCOPE_READ
    long_running = True
    conversation_argument = CONVERSATION_KEY

    async def run(self, call: GatewayCall) -> str:
        text = str(call.arguments.get("text", "")).strip()
        if not text:
            raise CallFailed("say what to ask: `text` is empty")
        return await call.ask(text, agent=str(call.arguments.get("agent", "") or ""))


class WaitAgentTool:
    """Come back for an answer the agent is still working on."""

    name = "wait_agent"
    description = (
        "Wait for the agent to finish what it was asked in a conversation, and get its answer. "
        "Call it when `ask_agent` said the agent was still working; call it again while it still is."
    )
    input_schema: Mapping[str, Any] = {
        "type": "object",
        "properties": {
            CONVERSATION_KEY: {"type": "string", "description": "The `conversation` the earlier result gave."},
        },
        "required": [CONVERSATION_KEY],
    }
    scope = MCP_SCOPE_READ
    long_running = False
    conversation_argument = CONVERSATION_KEY

    async def run(self, call: GatewayCall) -> str:
        if not call.handle:
            raise CallFailed("say which conversation to wait for: `conversation` is empty")
        state = await call.wait(call.handle)
        if state.state in ENDED:
            if state.is_error or state.state != STATE_DONE:
                raise CallFailed(state.answer or f"the agent's turn ended: {state.state}")
            return state.answer
        return state_words(state)


def state_words(state: TurnState) -> str:
    """How a turn still running is told to the client's model."""
    if state.state == STATE_AWAITING_APPROVAL:
        return (
            f"The agent is waiting for the person's yes in their chat ({state.step or 'a confirmation'}). "
            f"Call wait_agent with conversation {state.handle} once they have answered."
        )
    step = f" (now: {state.step})" if state.step else ""
    return f"The agent is still working{step}. Call wait_agent with conversation {state.handle} to get the answer."
