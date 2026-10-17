"""A stand-in for the runtime's process, for an application's own tests.

Everything above the process stays real: ``PiRuntime``, its sessions, the
spawn request it builds (environment, tool token, session proof, scope), the
tool server and the tools. What is scripted is only what the model would
have done — the text it answers with, the tools it calls, a failure of its
service. A tool call goes to the tool server over HTTP with the same headers
the runtime's tool extension sends, so the server authenticates it, checks
the allowlist, verifies the session proof and asks the gate exactly as it
would for the real thing.

    host = ScriptedHost(lambda request, message: ScriptedTurn(
        text="Done.", tool_calls=(ToolCall("add_comment", {"body": "hi"}),),
    ))
    runtime = PiRuntime(hosts=HostRouter(host), session_proofs=proofs, ...)

A turn that decides on what a tool came back with is a coroutine with a third
parameter, the process's line to the tool server:

    async def script(request, message, tools: ToolCaller) -> ScriptedTurn:
        issue = await tools.call(ToolCall("get_issue", {"key": "X-1"}))
        if issue.status != 200:
            return ScriptedTurn(text="I could not read it.")
        return ScriptedTurn(text=issue.body["result"]["summary"])

``host.opened`` holds every spawn request, ``host.tool_outcomes`` what each
tool call came back with.
"""

import asyncio
import contextlib
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from crucible.runtimes.pi.runtime import SESSION_PROOF_ENV
from crucible.runtimes.pi.spawn import SpawnRequest


@dataclass(frozen=True)
class ToolCall:
    """One call the scripted model makes. ``headers`` replace the ones the
    process was given, for a call that names another session or carries a
    proof of its own making — what the server says to it is the test."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptedTurn:
    """What the scripted model does with one prompt: call ``tool_calls`` in
    order, then answer ``text``. ``error`` instead ends the turn the way the
    runtime reports a failure of the model's service (``"503 Service
    Unavailable"``, ``"Connection error."``)."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class ToolOutcome:
    """What a scripted tool call came back with: the tool server's status and
    body, or status 0 and the reason when it was never reached."""

    name: str
    args: Mapping[str, Any]
    status: int
    body: Mapping[str, Any]


class ToolCaller:
    """The scripted process's line to the tool server: ``call`` sends one call
    as the runtime's tool extension would — the token, the session id and the
    session proof the process was given — reports it in the event stream and
    returns what came back. Handed to a script that takes a third parameter,
    so a turn can read a result before it decides what to do next."""

    def __init__(self, process: "ScriptedProcess") -> None:
        self._process = process

    async def call(self, call: ToolCall) -> ToolOutcome:
        return await self._process.call_tool(call)


# What the model does with a prompt: from (the spawn request of the process,
# the prompt), or from those and the tools when the script takes three.
TurnScript = (
    Callable[[SpawnRequest, str], ScriptedTurn | Awaitable[ScriptedTurn]]
    | Callable[[SpawnRequest, str, ToolCaller], ScriptedTurn | Awaitable[ScriptedTurn]]
)


def _takes_tools(script: TurnScript) -> bool:
    try:
        parameters = inspect.signature(script).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_POSITIONAL for p in parameters) or sum(
        1 for p in parameters if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ) >= 3


class ScriptedHost:
    """A ``RuntimeHost`` whose processes follow ``script``."""

    def __init__(self, script: TurnScript) -> None:
        self._script = script
        self.opened: list[SpawnRequest] = []
        self.tool_outcomes: list[ToolOutcome] = []

    async def open(self, request: SpawnRequest) -> "ScriptedProcess":
        self.opened.append(request)
        return ScriptedProcess(request, self._script, self.tool_outcomes)

    async def aclose(self) -> None:
        return None


class ScriptedProcess:
    """A ``PiTransport``: answers each prompt as the script says, in the
    runtime's own event stream, while the session reads it like any other."""

    def __init__(self, request: SpawnRequest, script: TurnScript, outcomes: list[ToolOutcome]) -> None:
        self._request = request
        self._script = script
        self._outcomes = outcomes
        self._lines: asyncio.Queue[str | None] = asyncio.Queue()
        self._turn: asyncio.Task[None] | None = None
        self._closed = False
        self._calls = 0

    async def send(self, line: str) -> None:
        if self._closed:
            raise BrokenPipeError("the scripted process has ended")
        command = json.loads(line)
        kind = command.get("type")
        # A prompt starts a run; a follow-up is queued behind it.
        disposition = {"prompt": "started", "follow_up": "queued"}.get(str(kind))
        started = {"data": {"disposition": disposition}} if disposition else {}
        self._emit({"type": "response", "id": command.get("id"), "command": kind, "success": True, **started})
        if kind in ("prompt", "follow_up"):
            # In the background, as the real process does: the turn's clock
            # and a gate's wait for a person run while it works.
            self._turn = asyncio.ensure_future(self._run(str(command.get("message", ""))))
        elif kind == "abort" and self._turn is not None and not self._turn.done():
            self._turn.cancel()
            self._emit(_agent_end(stop_reason="aborted"))
            self._emit({"type": "agent_settled", "aborted": True})

    async def lines(self) -> AsyncIterator[str]:
        while True:
            line = await self._lines.get()
            if line is None:
                return
            yield line

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn
        self._lines.put_nowait(None)

    async def _run(self, message: str) -> None:
        self._emit({"type": "agent_start"})
        turn = (
            self._script(self._request, message, ToolCaller(self))  # type: ignore[call-arg]
            if _takes_tools(self._script)
            else self._script(self._request, message)  # type: ignore[call-arg]
        )
        if isinstance(turn, Awaitable):
            turn = await turn
        for call in turn.tool_calls:
            await self.call_tool(call)
        if turn.error:
            self._emit(_agent_end(stop_reason="error", error=turn.error))
            self._emit({"type": "agent_settled", "aborted": False})
            return
        self._emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}})
        self._emit({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": turn.text}})
        self._emit({"type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": turn.text}})
        self._emit(_agent_end(stop_reason="stop", text=turn.text))
        self._emit({"type": "agent_settled", "aborted": False})

    async def call_tool(self, call: ToolCall) -> ToolOutcome:
        """One call, reported in the event stream as the real process reports
        it, recorded in the host's ``tool_outcomes`` and returned."""
        call_id = f"call-{self._calls}"
        self._calls += 1
        self._emit({"type": "tool_execution_start", "toolName": call.name,
                    "toolCallId": call_id, "args": dict(call.args)})
        outcome = await self._send_call(call)
        self._outcomes.append(outcome)
        self._emit({"type": "tool_execution_end", "toolName": call.name,
                    "toolCallId": call_id, "isError": not 200 <= outcome.status < 300})
        return outcome

    async def _send_call(self, call: ToolCall) -> ToolOutcome:
        """The tool extension's request (extension/index.ts), from the
        variables this process was given — or the headers the call names."""
        env = self._request.env
        url, token = env.get("TOOL_URL", ""), env.get("TOOL_TOKEN", "")
        if not url or not token:
            return ToolOutcome(call.name, call.args, 0, {"error": "no tool server for this agent"})
        headers = {
            "X-Tool-Token": token,
            "X-Runtime-Session": env.get("RUNTIME_SESSION_ID", ""),
            "X-Session-Proof": env.get(SESSION_PROOF_ENV, ""),
            **call.headers,
        }
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(f"{url}/tool/{call.name}", json=dict(call.args), headers=headers) as response:
                    body = await response.json(content_type=None)
                    return ToolOutcome(call.name, call.args, response.status, body or {})
        except (aiohttp.ClientError, ValueError) as exc:
            return ToolOutcome(call.name, call.args, 0, {"error": str(exc)})

    def _emit(self, event: dict[str, Any]) -> None:
        self._lines.put_nowait(json.dumps(event) + "\n")


def _agent_end(*, stop_reason: str, text: str = "", error: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "stopReason": stop_reason,
                               "content": [{"type": "text", "text": text}] if text else []}
    if error:
        message["errorMessage"] = error
    return {"type": "agent_end", "messages": [message]}
