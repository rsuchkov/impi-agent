"""The scripted runtime process an application tests against."""

from pathlib import Path
from typing import Any, ClassVar

import pytest

from crucible.ports.agent.errors import NOTICE_MODEL_UNAVAILABLE, notice_for
from crucible.ports.agent.events import ToolFinished
from crucible.runtimes.pi.errors import PiProcessError
from crucible.runtimes.pi.hosts import HostRouter
from crucible.runtimes.pi.profiles import PiProfile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.runtimes.pi.spawn import SpawnRequest
from crucible.runtimes.pi.testing import ScriptedHost, ScriptedTurn, ToolCall
from crucible.tools.base import Tool, ToolContext
from crucible.tools.proofs import SessionProofBook
from crucible.tools.registry import ToolRegistry
from crucible.tools.server import ToolServer

PORT = 8615


class _Directory:
    def list_agents(self) -> list:
        return []


class _Whoami(Tool):
    name: ClassVar[str] = "whoami"
    description: ClassVar[str] = "says which conversation called it"
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
        return {"agent": ctx.agent_name, "session": ctx.runtime_session_id, "args": args}


class _Elsewhere(_Whoami):
    name: ClassVar[str] = "elsewhere"


def _profile(**env: str) -> PiProfile:
    return PiProfile(name="assistant", config_dir=Path("agents") / "assistant", timeout=5.0, env=env)


async def test_a_scripted_turn_calls_a_real_tool_server_as_the_runtime_would() -> None:
    """The tool call proves its session like the real extension's, so the
    server's allowlist and proof check are what the test exercises."""
    proofs = SessionProofBook()
    server = ToolServer(
        ToolRegistry((_Whoami(), _Elsewhere())),  # type: ignore[arg-type]
        directory=_Directory(),  # type: ignore[arg-type]
        admins={}, tokens={"tok": "assistant"}, allowlists={"assistant": frozenset({"whoami"})},
        port=PORT, session_proofs=proofs,
    )
    asked: list[tuple[SpawnRequest, str]] = []

    def script(request: SpawnRequest, message: str) -> ScriptedTurn:
        asked.append((request, message))
        return ScriptedTurn(text="It was you.", tool_calls=(ToolCall("whoami", {"q": 1}), ToolCall("elsewhere")))

    host = ScriptedHost(script)
    runtime = PiRuntime(hosts=HostRouter(host), session_proofs=proofs)
    finished: list[ToolFinished] = []
    await server.start()
    try:
        result = await runtime.run_stateful(
            _profile(TOOL_URL=f"http://127.0.0.1:{PORT}", TOOL_TOKEN="tok"), "assistant--c1", "who am I?",
            on_event=lambda event: finished.append(event) if isinstance(event, ToolFinished) else None,
        )
        assert result.text == "It was you."
        assert [m for _, m in asked] == ["who am I?"]
        assert host.opened[0].session_id == "assistant--c1"
        allowed, refused = host.tool_outcomes
        assert allowed.status == 200
        assert allowed.body["result"] == {"agent": "assistant", "session": "assistant--c1", "args": {"q": 1}}
        assert refused.status == 403  # not on the allowlist: the server's own refusal
        # The session reported both calls, as it does for the real runtime.
        assert list(result.tool_calls) == ["whoami", "elsewhere"]
        assert [(e.tool, e.is_error) for e in finished] == [("whoami", False), ("elsewhere", True)]
    finally:
        await runtime.close()
        await server.stop()


async def test_a_failing_model_service_ends_the_turn_as_the_runtime_reports_it() -> None:
    host = ScriptedHost(lambda request, message: ScriptedTurn(error="503 Service Unavailable"))
    runtime = PiRuntime(hosts=HostRouter(host))
    try:
        with pytest.raises(PiProcessError) as failed:
            await runtime.run_stateful(_profile(), "assistant--c1", "hi")
        assert notice_for(failed.value)[0] == NOTICE_MODEL_UNAVAILABLE
    finally:
        await runtime.close()


async def test_a_turn_can_be_cancelled_and_the_conversation_goes_on() -> None:
    import asyncio

    release = asyncio.Event()

    async def script(request: SpawnRequest, message: str) -> ScriptedTurn:
        if message == "slow":
            await release.wait()
        return ScriptedTurn(text=f"re: {message}")

    runtime = PiRuntime(hosts=HostRouter(ScriptedHost(script)))
    try:
        turn = asyncio.ensure_future(runtime.run_stateful(_profile(), "assistant--c1", "slow"))
        await asyncio.sleep(0.05)
        assert await runtime.cancel("assistant--c1") is True
        assert (await turn).stop_reason == "aborted"
        assert (await runtime.run_stateful(_profile(), "assistant--c1", "next")).text == "re: next"
    finally:
        await runtime.close()


async def test_the_scripted_process_answers_commands_as_the_real_one_does() -> None:
    """A prompt starts a run and a follow-up queues behind it, and a run ends
    with agent_end then agent_settled — what the driver waits for."""
    import asyncio
    import json

    from crucible.runtimes.pi.testing import ScriptedProcess

    process = ScriptedProcess(
        SpawnRequest(agent="a", profile_dir=Path(".")), lambda request, message: ScriptedTurn(text="hi"), [],
    )
    await process.send(json.dumps({"id": "p", "type": "prompt", "message": "x"}))
    await process.send(json.dumps({"id": "f", "type": "follow_up", "message": "y"}))
    seen: list[dict] = []

    async def read() -> None:
        async for line in process.lines():
            seen.append(json.loads(line))
            if sum(1 for e in seen if e.get("type") == "agent_settled") == 2:
                return

    await asyncio.wait_for(read(), 5)
    await process.aclose()
    dispositions = {e["id"]: e["data"]["disposition"] for e in seen if e.get("type") == "response"}
    assert dispositions == {"p": "started", "f": "queued"}
    types = [e["type"] for e in seen if e["type"] in ("agent_end", "agent_settled")]
    assert types[:2] == ["agent_end", "agent_settled"]
