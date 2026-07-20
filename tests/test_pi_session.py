import asyncio

import pytest

from crucible.ports.agent.ui import UiOutcome, UiRequest
from crucible.runtimes.pi.errors import PiProcessError, PiTimeout
from crucible.runtimes.pi.session import PiRpcSession
from tests.fakes.fake_transport import FakeTransport


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _normal_turn_reactor(transport: FakeTransport, *, text: str = "Answer", tools=()):
    """Scripted happy-path turn in the CURRENT pi event shape (text_end)."""

    def react(command: dict) -> None:
        if command.get("type") not in ("prompt", "follow_up"):
            return
        transport.emit({"type": "agent_start"})
        for tool in tools:
            transport.emit({"type": "tool_execution_start", "toolName": tool})
            transport.emit({"type": "tool_execution_end", "toolName": tool})
        if text is not None:
            transport.emit(
                {"type": "message_update", "assistantMessageEvent": {"type": "text_start"}}
            )
            transport.emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text},
                }
            )
            transport.emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": text},
                }
            )
        transport.emit({"type": "agent_end", "messages": []})

    return react


async def test_prompt_returns_aggregated_text_and_tool_calls() -> None:
    transport = FakeTransport()
    transport._reactor = _normal_turn_reactor(transport, text="Hello", tools=("list_agents",))
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("hi", timeout=1.0)

    assert result.text == "Hello"
    assert result.tool_calls == ["list_agents"]
    assert result.duration_s > 0
    assert transport.sent_types() == ["prompt"]


async def test_text_falls_back_to_agent_end_messages() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_start"})
        transport.emit(
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "content": "from agent_end"}],
            }
        )

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("x", timeout=1.0)
    assert result.text == "from agent_end"


async def test_agent_end_fallback_skips_thinking_blocks() -> None:
    # Real agent_end content mixes thinking and text blocks; only text counts.
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_start"})
        transport.emit(
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "stopReason": "stop",
                        "content": [
                            {"type": "thinking", "thinking": "hmm", "thinkingSignature": "sig"},
                            {"type": "text", "text": "visible answer"},
                        ],
                    }
                ],
            }
        )

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("x", timeout=1.0)
    assert result.text == "visible answer"
    assert result.stop_reason == "stop"


async def test_follow_up_uses_follow_up_command() -> None:
    transport = FakeTransport()
    transport._reactor = _normal_turn_reactor(transport, text="first")
    session = PiRpcSession(transport)
    session.start()

    await session.prompt("one", timeout=1.0)
    await session.follow_up("two", timeout=1.0)

    assert transport.sent_types() == ["prompt", "follow_up"]


async def test_timeout_aborts_and_raises() -> None:
    transport = FakeTransport()  # no reactor -> no agent_end ever
    session = PiRpcSession(transport)
    session.start()

    with pytest.raises(PiTimeout):
        await session.prompt("stuck", timeout=0.05)

    assert "abort" in transport.sent_types()


async def test_session_is_poisoned_after_timeout() -> None:
    # A late agent_end from the aborted turn could complete the NEXT turn, so
    # after a timeout every subsequent turn must be refused.
    transport = FakeTransport()
    session = PiRpcSession(transport)
    session.start()

    with pytest.raises(PiTimeout):
        await session.prompt("stuck", timeout=0.05)

    with pytest.raises(PiProcessError, match="poisoned"):
        await session.prompt("next", timeout=1.0)


async def test_retry_agent_end_does_not_finish_turn() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_start"})
        # transient failure: pi will retry, turn must NOT complete here
        transport.emit({"type": "agent_end", "messages": [], "willRetry": True})
        # successful retry
        transport.emit(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_end", "content": "final answer"},
            }
        )
        transport.emit({"type": "agent_end", "messages": [], "willRetry": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("hi", timeout=1.0)
    assert result.text == "final answer"


async def test_llm_error_raises_process_error() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_start"})
        transport.emit(
            {
                "type": "agent_end",
                "willRetry": False,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [],
                        "stopReason": "error",
                        "errorMessage": "Connection error.",
                    }
                ],
            }
        )

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    with pytest.raises(PiProcessError, match="Connection error"):
        await session.prompt("hi", timeout=1.0)


async def test_process_exit_raises_process_error() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.eof()  # process dies mid-turn

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    with pytest.raises(PiProcessError):
        await session.prompt("boom", timeout=1.0)


async def test_response_failure_raises_process_error() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit(
            {
                "type": "response",
                "id": command["id"],
                "command": "prompt",
                "success": False,
                "error": "model not found",
            }
        )

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    with pytest.raises(PiProcessError, match="model not found"):
        await session.prompt("x", timeout=1.0)


async def test_extension_ui_request_allows_allowlisted_tool() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport, allowed_ui_tools=frozenset({"list_agents"}))
    session.start()

    transport.emit(
        {"type": "extension_ui_request", "id": "u1", "method": "confirm", "toolName": "list_agents"}
    )
    await _wait_until(lambda: len(transport.sent) >= 1)

    reply = transport.last_sent()
    assert reply["type"] == "extension_ui_response"
    assert reply["id"] == "u1"
    assert reply["confirmed"] is True


async def test_extension_ui_request_denies_unknown_tool() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport, allowed_ui_tools=frozenset({"list_agents"}))
    session.start()

    transport.emit(
        {"type": "extension_ui_request", "id": "u2", "method": "confirm", "toolName": "write"}
    )
    await _wait_until(lambda: len(transport.sent) >= 1)

    reply = transport.last_sent()
    assert reply["confirmed"] is False


async def test_extension_ui_request_ignores_fire_and_forget() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport)
    session.start()

    # setStatus / notify don't expect a reply; the session must not respond.
    transport.emit({"type": "extension_ui_request", "id": "s1", "method": "setStatus", "statusKey": "mcp"})
    transport.emit({"type": "extension_ui_request", "id": "n1", "method": "notify", "message": "hi"})
    await asyncio.sleep(0.05)

    assert transport.sent == []


class _FakeUiBridge:
    def __init__(self, outcome: UiOutcome | None = None, *, raises: bool = False) -> None:
        self.calls: list[tuple[str, UiRequest]] = []
        self._outcome = outcome if outcome is not None else UiOutcome(cancelled=True)
        self._raises = raises

    async def request(self, runtime_session_id: str, req: UiRequest) -> UiOutcome:
        self.calls.append((runtime_session_id, req))
        if self._raises:
            raise RuntimeError("bridge boom")
        return self._outcome


async def test_ui_request_routes_to_bridge_confirm() -> None:

    transport = FakeTransport()
    bridge = _FakeUiBridge(UiOutcome(confirmed=True))
    session = PiRpcSession(transport, ui_bridge=bridge, session_id="assistant--c1")
    session.start()

    transport.emit({"type": "extension_ui_request", "id": "u1", "method": "confirm",
                    "title": "Create channel?", "message": "proceed?"})
    await _wait_until(lambda: len(transport.sent) >= 1)

    assert transport.last_sent() == {"type": "extension_ui_response", "id": "u1", "confirmed": True}
    assert bridge.calls == [(
        "assistant--c1",
        UiRequest(request_id="u1", method="confirm", title="Create channel?", message="proceed?"),
    )]


async def test_ui_request_routes_to_bridge_select() -> None:

    transport = FakeTransport()
    bridge = _FakeUiBridge(UiOutcome(value="B"))
    session = PiRpcSession(transport, ui_bridge=bridge, session_id="a--c")
    session.start()

    transport.emit({"type": "extension_ui_request", "id": "u2", "method": "select",
                    "title": "Pick", "options": ["A", "B", "C"]})
    await _wait_until(lambda: len(transport.sent) >= 1)

    assert transport.last_sent() == {"type": "extension_ui_response", "id": "u2", "value": "B"}
    assert bridge.calls[0][1].options == ("A", "B", "C")


async def test_ui_request_bridge_cancelled_sends_only_cancelled() -> None:

    transport = FakeTransport()
    session = PiRpcSession(
        transport, ui_bridge=_FakeUiBridge(UiOutcome(cancelled=True)), session_id="a--c"
    )
    session.start()

    transport.emit({"type": "extension_ui_request", "id": "u3", "method": "confirm", "title": "?"})
    await _wait_until(lambda: len(transport.sent) >= 1)

    assert transport.last_sent() == {"type": "extension_ui_response", "id": "u3", "cancelled": True}


async def test_ui_request_bridge_failure_defaults_to_cancelled() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport, ui_bridge=_FakeUiBridge(raises=True), session_id="a--c")
    session.start()

    transport.emit({"type": "extension_ui_request", "id": "u4", "method": "confirm", "title": "?"})
    await _wait_until(lambda: len(transport.sent) >= 1)

    assert transport.last_sent()["cancelled"] is True


async def test_turn_timeout_pauses_while_bridge_awaits_human(monkeypatch) -> None:
    from crucible.runtimes.pi import session as session_module

    monkeypatch.setattr(session_module, "_TURN_POLL_INTERVAL", 0.02)
    transport = FakeTransport()
    release = asyncio.Event()

    class _BlockingBridge:
        async def request(self, runtime_session_id, req):
            await release.wait()
            return UiOutcome(confirmed=True)

    def react(command: dict) -> None:
        if command.get("type") in ("prompt", "follow_up"):
            # Ask mid-turn, then never finish: the turn would time out if the
            # clock weren't paused while the human is being asked.
            transport.emit(
                {"type": "extension_ui_request", "id": "u1", "method": "confirm", "title": "?"}
            )

    transport._reactor = react
    session = PiRpcSession(transport, ui_bridge=_BlockingBridge(), session_id="a--c")
    session.start()

    turn = asyncio.ensure_future(session.prompt("hi", timeout=0.1))
    # Well past the 0.1s turn timeout, but the bridge is still blocking: no timeout.
    await asyncio.sleep(0.3)
    assert not turn.done()

    release.set()  # bridge returns; still no agent_end -> timeout resumes and fires
    with pytest.raises(PiTimeout):
        await turn


async def test_close_fails_in_flight_turn() -> None:
    transport = FakeTransport()  # never answers
    session = PiRpcSession(transport)
    session.start()

    task = asyncio.ensure_future(session.prompt("hang", timeout=5.0))
    await asyncio.sleep(0.02)
    await session.close()

    with pytest.raises(PiProcessError):
        await task


async def test_busy_reflects_in_flight_turn() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport)
    session.start()

    assert not session.busy
    task = asyncio.ensure_future(session.prompt("hang", timeout=5.0))
    await asyncio.sleep(0.02)
    assert session.busy
    await session.close()
    with pytest.raises(PiProcessError):
        await task
    assert not session.busy


async def test_on_event_callback_receives_events() -> None:
    transport = FakeTransport()
    transport._reactor = _normal_turn_reactor(transport, text="ok", tools=("list_agents",))
    seen: list[str] = []

    session = PiRpcSession(transport, on_event=lambda ev: seen.append(ev.type))
    session.start()
    await session.prompt("hi", timeout=1.0)

    assert "agent_start" in seen
    assert "tool_execution_start" in seen
    assert "agent_end" in seen
