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

``host.opened`` holds every spawn request, ``host.tool_outcomes`` what each
tool call came back with.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from crucible.runtimes.pi.runtime import SESSION_PROOF_ENV
from crucible.runtimes.pi.spawn import SpawnRequest


@dataclass(frozen=True)
class ToolCall:
    """One call the scripted model makes, before it answers."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)


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


# (the spawn request of the process, the prompt) -> what the model does.
TurnScript = Callable[[SpawnRequest, str], ScriptedTurn | Awaitable[ScriptedTurn]]


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

    async def send(self, line: str) -> None:
        if self._closed:
            raise BrokenPipeError("the scripted process has ended")
        command = json.loads(line)
        kind = command.get("type")
        started = {"data": {"disposition": "started"}} if kind in ("prompt", "follow_up") else {}
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
        turn = self._script(self._request, message)
        if isinstance(turn, Awaitable):
            turn = await turn
        self._emit({"type": "agent_start"})
        for number, call in enumerate(turn.tool_calls):
            call_id = f"call-{number}"
            self._emit({"type": "tool_execution_start", "toolName": call.name,
                        "toolCallId": call_id, "args": dict(call.args)})
            outcome = await self._call_tool(call)
            self._outcomes.append(outcome)
            self._emit({"type": "tool_execution_end", "toolName": call.name,
                        "toolCallId": call_id, "isError": not 200 <= outcome.status < 300})
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

    async def _call_tool(self, call: ToolCall) -> ToolOutcome:
        """The tool extension's request (extension/index.ts), from the
        variables this process was given."""
        env = self._request.env
        url, token = env.get("TOOL_URL", ""), env.get("TOOL_TOKEN", "")
        if not url or not token:
            return ToolOutcome(call.name, call.args, 0, {"error": "no tool server for this agent"})
        headers = {
            "X-Tool-Token": token,
            "X-Runtime-Session": env.get("RUNTIME_SESSION_ID", ""),
            "X-Session-Proof": env.get(SESSION_PROOF_ENV, ""),
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
