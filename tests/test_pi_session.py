import asyncio

import pytest

from crucible.ports.agent.events import ToolFinished, ToolStarted
from crucible.ports.agent.ui import UiOutcome, UiRequest
from crucible.runtimes.pi.errors import PiProcessError, PiTimeout
from crucible.runtimes.pi.protocol import PiEvent
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
        for n, tool in enumerate(tools):
            transport.emit({
                "type": "tool_execution_start", "toolCallId": f"call_{n}",
                "toolName": tool, "args": {"q": tool},
            })
            transport.emit({
                "type": "tool_execution_end", "toolCallId": f"call_{n}",
                "toolName": tool, "isError": False,
                "result": {"content": [{"type": "text", "text": "done"}], "details": {}},
            })
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
        transport.emit({"type": "agent_settled", "aborted": False})

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
        transport.emit({"type": "agent_settled", "aborted": False})

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
        transport.emit({"type": "agent_settled", "aborted": False})

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
        transport.emit({"type": "agent_settled", "aborted": False})

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
        transport.emit({"type": "agent_settled", "aborted": False})

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


async def test_pause_clock_holds_the_timeout_until_the_last_waiter_leaves(monkeypatch) -> None:
    """The pause the tool server takes while its gate waits — reentrant, because
    the bridge may be waiting on the same person at the same time, and the
    clock must not restart when the inner of two waits ends."""
    from crucible.runtimes.pi import session as session_module

    monkeypatch.setattr(session_module, "_TURN_POLL_INTERVAL", 0.02)
    transport = FakeTransport()  # never answers
    session = PiRpcSession(transport)
    session.start()

    turn = asyncio.ensure_future(session.prompt("hi", timeout=0.1))
    async with session.pause_clock():
        async with session.pause_clock():
            await asyncio.sleep(0.2)
        await asyncio.sleep(0.2)  # the inner wait ended; the outer still holds
        assert not turn.done()
    with pytest.raises(PiTimeout):  # nobody waiting: the timeout resumes and fires
        await turn


async def test_cancel_interrupts_the_turn_and_keeps_the_session_usable() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})  # and then it thinks for a long time
        elif command.get("type") == "abort":
            transport.emit(
                {"type": "message_update",
                 "assistantMessageEvent": {"type": "text_end", "content": "So far:"}}
            )
            transport.emit({"type": "agent_end", "messages": []})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("hi", timeout=5.0))
    await _wait_until(lambda: "prompt" in transport.sent_types())

    assert await session.cancel(timeout=1.0) is True
    assert "abort" in transport.sent_types()
    assert (await turn).text == "So far:"  # the turn ended with what it had

    # Not poisoned: the next turn runs as usual.
    transport._reactor = _normal_turn_reactor(transport, text="Next")
    assert (await session.prompt("again", timeout=5.0)).text == "Next"


async def test_cancel_says_so_when_the_runtime_does_not_end_the_turn() -> None:
    transport = FakeTransport()  # ignores the abort; the turn never ends
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("hi", timeout=5.0))
    await _wait_until(lambda: "prompt" in transport.sent_types())

    assert await session.cancel(timeout=0.05) is False
    assert not turn.done()
    assert await session.cancel(timeout=0.05) is False  # still in flight; idempotent
    await session.close()
    with pytest.raises(PiProcessError):
        await turn


async def test_cancel_with_no_turn_in_flight_does_nothing() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport)
    assert await session.cancel(timeout=0.05) is False
    assert transport.sent == []


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
    assert "tool_started" in seen  # translated, not the wire's own name
    assert "agent_end" in seen


async def test_tool_activity_crosses_the_port_in_its_neutral_shape() -> None:
    """A listener sees what ran, with what, and how long it took — without
    knowing what this runtime calls its events or where it keeps the fields."""
    transport = FakeTransport()

    async def react(command: dict) -> None:
        if command.get("type") != "prompt":
            return
        transport.emit({"type": "agent_start"})
        transport.emit({
            "type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
            "args": {"command": "make test"},
        })
        await asyncio.sleep(0.03)  # the tool running
        transport.emit({
            "type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash",
            "isError": True, "result": {"content": [{"type": "text", "text": "boom"}]},
        })
        transport.emit({"type": "agent_end", "messages": []})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    seen: list[object] = []
    session = PiRpcSession(transport)
    session.start()
    await session.prompt("hi", timeout=1.0, on_event=seen.append)

    started = next(e for e in seen if isinstance(e, ToolStarted))
    finished = next(e for e in seen if isinstance(e, ToolFinished))
    assert (started.call_id, started.tool, started.args) == ("c1", "bash", {"command": "make test"})
    assert (finished.call_id, finished.tool, finished.is_error) == ("c1", "bash", True)
    assert finished.duration_s >= 0.03
    # Everything that is not tool activity is still the driver's own event.
    raw = [e for e in seen if isinstance(e, PiEvent)]
    assert {e.type for e in raw} >= {"agent_start", "agent_end"}
    assert not any(isinstance(e, PiEvent) and e.type.startswith("tool_execution") for e in seen)


async def test_a_turns_own_listener_wins_over_the_sessions() -> None:
    # The session-wide callback is an observer for whoever built the session;
    # a turn that brought its own must be the one that hears the turn.
    transport = FakeTransport()
    transport._reactor = _normal_turn_reactor(transport, text="ok", tools=("list_agents",))
    session_saw: list[str] = []
    turn_saw: list[str] = []

    session = PiRpcSession(transport, on_event=lambda ev: session_saw.append(ev.type))
    session.start()
    await session.prompt("hi", timeout=1.0, on_event=lambda ev: turn_saw.append(ev.type))

    assert "tool_started" in turn_saw
    assert session_saw == []


async def test_cancel_of_a_turn_on_a_dead_process_does_not_raise() -> None:
    transport = FakeTransport()
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("hi", timeout=5.0))
    await _wait_until(lambda: "prompt" in transport.sent_types())
    transport.closed = True  # stdin is gone: an abort cannot be delivered

    assert await session.cancel(timeout=0.05) is False  # said, not raised
    transport.closed = False
    await session.close()
    with pytest.raises(PiProcessError):
        await turn


def _aborted_turn_end(transport) -> None:
    transport.emit({"type": "agent_start"})
    transport.emit(
        {
            "type": "agent_end",
            "willRetry": False,
            "messages": [
                {"role": "assistant", "content": [], "stopReason": "toolUse"},
                {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "This operation was aborted",
                },
            ],
        }
    )
    transport.emit({"type": "agent_settled", "aborted": False})


async def test_an_abort_the_session_asked_for_is_a_cancelled_turn_however_it_is_worded() -> None:
    """Since pi 0.82 a turn the engine aborted ends with `stopReason: error`
    and "This operation was aborted"; 0.80 said `aborted`. Both are the cancel
    the engine asked for, not a provider failure."""
    transport = FakeTransport()
    session = PiRpcSession(transport)
    session.start()

    def react(command: dict) -> None:
        if command.get("type") == "abort":
            _aborted_turn_end(transport)

    transport._reactor = react
    turn = asyncio.ensure_future(session.prompt("hi", timeout=2.0))
    await asyncio.sleep(0.01)
    await session.abort()
    result = await turn
    assert result.stop_reason == "aborted" and result.text == ""


async def test_the_same_words_without_an_abort_from_the_session_are_a_failure() -> None:
    """Node says "This operation was aborted" for ANY aborted request — a
    provider timeout, a dropped connection. Unasked, it is an error."""
    transport = FakeTransport()
    transport._reactor = lambda command: _aborted_turn_end(transport)
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiProcessError, match="aborted"):
        await session.prompt("hi", timeout=1.0)


async def test_the_result_names_the_model_that_answered() -> None:
    # After a move to a fallback the answer comes from another model than the
    # agent's own; the run's last answer says which.
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_end", "messages": [
            {"role": "assistant", "content": "", "stopReason": "error", "provider": "dead", "model": "m1"},
            {"role": "assistant", "content": "ok", "provider": "openai-codex", "model": "gpt-5.5"},
        ]})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    assert (await session.prompt("x", timeout=1.0)).model == "openai-codex/gpt-5.5"


async def test_the_result_sums_what_the_runs_answers_cost() -> None:
    """pi puts ``usage`` on every assistant message; a turn that moved to a
    fallback has two, and the turn cost both. None when pi reported none."""
    from crucible.ports.agent import TokenUsage

    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_end", "messages": [
            {"role": "assistant", "content": "", "stopReason": "error", "provider": "a", "model": "m1",
             "usage": {"input": 100, "output": 0, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 150,
                       "cost": {"input": 0.001, "output": 0, "cacheRead": 0.0001, "cacheWrite": 0, "total": 0.0011}}},
            {"role": "user", "content": "retry"},
            {"role": "assistant", "content": "ok", "provider": "b", "model": "m2",
             "usage": {"input": 200, "output": 40, "cacheRead": 0, "cacheWrite": 10, "totalTokens": 250,
                       "cost": {"input": 0.002, "output": 0.0008, "cacheRead": 0, "cacheWrite": 0.0001, "total": 0.0029}}},
            {"role": "assistant", "content": "no usage here"},
        ]})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    result = await session.prompt("x", timeout=1.0)
    assert result.usage == TokenUsage(input=300, output=40, cache_read=50, cache_write=10, total=400, cost_usd=0.004)
    transport2 = FakeTransport()

    def quiet(command: dict) -> None:
        transport2.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "ok"}]})
        transport2.emit({"type": "agent_settled", "aborted": False})

    transport2._reactor = quiet
    session2 = PiRpcSession(transport2)
    session2.start()
    assert (await session2.prompt("x", timeout=1.0)).usage is None
    # A number that is not one (a string, a null, a boolean) counts as 0 — the
    # turn's result is not lost over a field the engine only reports.
    transport3 = FakeTransport()

    def odd(command: dict) -> None:
        transport3.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "ok", "usage": {
            "input": "many", "output": None, "totalTokens": True, "cost": {"total": "n/a"}}}]})
        transport3.emit({"type": "agent_settled", "aborted": False})

    transport3._reactor = odd
    session3 = PiRpcSession(transport3)
    session3.start()
    assert (await session3.prompt("x", timeout=1.0)).usage == TokenUsage()


async def test_a_result_without_a_named_model_says_none() -> None:
    transport = FakeTransport()
    def react(command: dict) -> None:
        transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "ok"}]})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    assert (await session.prompt("x", timeout=1.0)).model == ""


# --- a turn ends when the runtime settles --------------------------------------------


async def test_a_run_the_runtime_recovers_by_itself_is_part_of_the_turn() -> None:
    """After a run's final agent_end the runtime may still go on — recover an
    overflowed context, retry a compaction — and only agent_settled says it
    will not. The turn's answer is the last run's."""
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "agent_start"})
        transport.emit({"type": "agent_end", "willRetry": False, "messages": [
            {"role": "assistant", "stopReason": "error", "errorMessage": "context overflow", "content": []},
        ]})
        transport.emit({"type": "agent_start"})  # recovered on its own
        transport.emit({"type": "message_update",
                        "assistantMessageEvent": {"type": "text_end", "content": "after compaction"}})
        transport.emit({"type": "agent_end", "messages": [], "willRetry": False})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("hi", timeout=1.0)
    assert result.text == "after compaction"


async def test_an_ended_run_does_not_end_the_turn_before_the_runtime_settles() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "x"}})
        transport.emit({"type": "agent_end", "messages": []})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiTimeout):
        await session.prompt("hi", timeout=0.3)


async def test_a_prompt_an_extension_consumed_ends_the_turn_at_once() -> None:
    """A "handled" prompt starts no run, so nothing will settle: waiting for
    it would be waiting for the timeout."""
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "response", "id": command["id"], "command": "prompt", "success": True,
                        "data": {"disposition": "handled"}})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    result = await session.prompt("/something", timeout=1.0)
    assert result.text == "" and result.tool_calls == []


async def test_a_process_gone_after_its_run_ended_still_answers() -> None:
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "kept"}})
        transport.emit({"type": "agent_end", "messages": []})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("hi", timeout=5.0))
    await asyncio.sleep(0.05)
    await session.close()
    assert (await turn).text == "kept"


async def test_a_process_that_exits_after_its_run_ended_still_answers() -> None:
    """The process's own end — stdout closing — not only the engine closing
    the session: the run it finished is the answer."""
    transport = FakeTransport()

    def react(command: dict) -> None:
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "kept"}})
        transport.emit({"type": "agent_end", "messages": []})
        transport.eof()

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    assert (await session.prompt("hi", timeout=5.0)).text == "kept"


async def test_a_process_that_exits_mid_run_is_a_failure() -> None:
    transport = FakeTransport()
    transport._reactor = lambda command: transport.eof()
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiProcessError, match="exited unexpectedly"):
        await session.prompt("hi", timeout=5.0)


async def test_a_settle_with_no_run_behind_it_is_not_an_answer() -> None:
    transport = FakeTransport()
    transport._reactor = lambda command: transport.emit({"type": "agent_settled", "aborted": False})
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiProcessError, match="settled without ending a run"):
        await session.prompt("hi", timeout=1.0)


def _remark_then_tool_reactor(transport: FakeTransport, *, remark: str, tools=("search",), answer: str | None):
    """The model writes ``remark``, calls ``tools``, then writes ``answer`` (or
    nothing when None)."""

    def _text(content: str) -> None:
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}})
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": content}})

    def react(command: dict) -> None:
        if command.get("type") != "prompt":
            return
        transport.emit({"type": "agent_start"})
        _text(remark)
        for n, tool in enumerate(tools):
            transport.emit({"type": "tool_execution_start", "toolCallId": f"call_{n}", "toolName": tool, "args": {}})
            transport.emit({"type": "tool_execution_end", "toolCallId": f"call_{n}", "toolName": tool, "isError": False})
        if answer is not None:
            _text(answer)
        transport.emit({"type": "agent_end", "messages": []})
        transport.emit({"type": "agent_settled", "aborted": False})

    return react


async def test_a_remark_before_a_call_is_progress_and_the_answer_is_what_follows_the_last_call() -> None:
    from crucible.ports.agent.events import TextProgress, ToolStarted

    transport = FakeTransport()
    transport._reactor = _remark_then_tool_reactor(
        transport, remark="Found the workflow, now listing transitions…", answer="Here are the transitions."
    )
    session = PiRpcSession(transport)
    session.start()
    seen: list[object] = []

    result = await session.prompt("hi", timeout=1.0, on_event=seen.append)

    assert result.text == "Here are the transitions."  # the remark is not in the answer
    progress = [e for e in seen if isinstance(e, TextProgress)]
    assert [e.text for e in progress] == ["Found the workflow, now listing transitions…"]
    # ...and it reached the listener before the call it preceded.
    kinds = [type(e).__name__ for e in seen if isinstance(e, (TextProgress, ToolStarted))]
    assert kinds == ["TextProgress", "ToolStarted"]


async def test_silence_after_the_last_call_falls_back_to_the_last_remark() -> None:
    transport = FakeTransport()
    transport._reactor = _remark_then_tool_reactor(transport, remark="Done, the comment is posted.", answer=None)
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("hi", timeout=1.0)

    assert result.text == "Done, the comment is posted."
    assert result.tool_calls == ["search"]


def _length_reactor(transport: FakeTransport, *, text: str | None):
    """A run the model's output limit ended: ``stopReason: length``."""

    def react(command: dict) -> None:
        if command.get("type") != "prompt":
            return
        transport.emit({"type": "agent_start"})
        if text is not None:
            transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": text}})
        transport.emit({"type": "agent_end", "messages": [
            {"role": "assistant", "stopReason": "length",
             "content": [{"type": "thinking", "thinking": "let me consider every"}]},
        ]})
        transport.emit({"type": "agent_settled", "aborted": False})

    return react


async def test_a_run_the_length_limit_ended_is_truncated_with_or_without_text() -> None:
    transport = FakeTransport()
    transport._reactor = _length_reactor(transport, text="The transitions are: To Do →")
    session = PiRpcSession(transport)
    session.start()
    cut = await session.prompt("hi", timeout=1.0)
    assert cut.text == "The transitions are: To Do →" and cut.truncated and cut.stop_reason == "length"

    transport = FakeTransport()
    transport._reactor = _length_reactor(transport, text=None)  # 8k tokens of thinking, no answer
    session = PiRpcSession(transport)
    session.start()
    nothing = await session.prompt("hi", timeout=1.0)
    assert nothing.text == "" and nothing.truncated  # not an empty answer: the limit ate it


async def test_a_process_that_says_nothing_is_stalled_unless_a_tool_is_running() -> None:
    from crucible.runtimes.pi.errors import PiStalled

    def quiet(transport: FakeTransport, *, tool: bool):
        def react(command: dict) -> None:
            if command.get("type") != "prompt":
                return
            transport.emit({"type": "agent_start"})
            if tool:
                transport.emit({"type": "tool_execution_start", "toolCallId": "c0", "toolName": "bash", "args": {}})
        return react

    transport = FakeTransport()
    transport._reactor = quiet(transport, tool=False)
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiStalled, match="reported nothing"):
        await session.prompt("hi", timeout=5.0, idle_timeout=0.1)  # long before the 5 s ceiling
    assert "abort" in transport.sent_types()

    # A tool in flight is work the process waits on, not silence of its own:
    # the stall clock does not run, and only the ceiling ends the turn.
    transport = FakeTransport()
    transport._reactor = quiet(transport, tool=True)
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiTimeout) as ended:
        await session.prompt("hi", timeout=0.7, idle_timeout=0.1)
    assert not isinstance(ended.value, PiStalled)  # the ceiling, not the watchdog

    # And a process that keeps talking — deltas of text or reasoning — is at
    # work, however long it takes: every event resets the stall clock.
    transport = FakeTransport()

    async def keep_talking() -> None:
        transport.emit({"type": "agent_start"})
        for _ in range(12):
            await asyncio.sleep(0.05)
            transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "…"}})
        transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "done"}})
        transport.emit({"type": "agent_end", "messages": []})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = lambda command: (asyncio.ensure_future(keep_talking()) if command.get("type") == "prompt" else None)
    session = PiRpcSession(transport)
    session.start()
    assert (await session.prompt("hi", timeout=5.0, idle_timeout=0.2)).text == "done"


async def test_each_answer_of_the_model_crosses_the_port_with_what_it_cost() -> None:
    """pi brackets one answer of the model with turn_start/turn_end and puts
    usage, provider, model and stopReason on the message: a listener sees one
    ``ModelCallEnded`` per answer, counted from 0, with the driver's timing."""
    from crucible.ports.agent import TokenUsage
    from crucible.ports.agent.events import ModelCallEnded

    transport = FakeTransport()

    def react(command: dict) -> None:
        if command.get("type") != "prompt":
            return
        transport.emit({"type": "agent_start"})
        transport.emit({"type": "turn_start"})
        transport.emit({"type": "turn_end", "message": {
            "role": "assistant", "provider": "openai", "model": "gpt-5.5", "stopReason": "toolUse",
            "usage": {"input": 100, "output": 20, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 170, "cost": {"total": 0.001}},
        }})
        transport.emit({"type": "turn_end", "message": {"role": "user", "usage": {"totalTokens": 99999}}})  # not the model's
        transport.emit({"type": "turn_start"})
        transport.emit({"type": "turn_end", "message": {"role": "assistant", "content": "done", "stopReason": "stop"}})  # an older pi: no usage
        transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]})
        transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    seen: list[object] = []
    session = PiRpcSession(transport)
    session.start()
    await session.prompt("hi", timeout=1.0, on_event=seen.append)
    answers = [e for e in seen if isinstance(e, ModelCallEnded)]
    assert [(a.index, a.model, a.stop_reason) for a in answers] == [(0, "openai/gpt-5.5", "toolUse"), (1, "", "stop")]
    assert answers[0].usage == TokenUsage(input=100, output=20, cache_read=50, cache_write=0, total=170, cost_usd=0.001)
    assert answers[1].usage is None and all(a.duration_s >= 0 for a in answers)
    assert all(e.type == "model_call_ended" for e in answers)


def _user_message(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _text_done(text: str) -> dict:
    return {"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": text}}


async def test_a_message_put_to_the_turn_is_read_after_the_calls_in_flight_and_the_turn_goes_on() -> None:
    """As pi does it (probe A): a steer sent while a tool runs is read after
    the tool's result, before the model's next call; the session sends it as
    pi's steer, sees the model read it (``MessageRead``), and the result is
    the model's answer to both."""
    from crucible.ports.agent.events import Answered, MessageRead

    transport = FakeTransport()
    seen: list[object] = []

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message("Run sleep, then say DONE")})
            transport.emit({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash", "args": {"command": "sleep 6"}})
        if command.get("type") == "steer":
            transport.emit({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash"})
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "toolUse"}})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message(command["message"])})
            transport.emit(_text_done("DONE\n4"))
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
            transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "DONE\n4"}]})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    assert await session.steer("too early") is False  # no turn in flight
    turn = asyncio.ensure_future(session.prompt("Run sleep, then say DONE", timeout=5.0, on_event=seen.append))
    for _ in range(200):
        if any(isinstance(e, ToolStarted) for e in seen):
            break
        await asyncio.sleep(0.005)
    assert await session.steer("Also: what is 2+2?") is True
    result = await turn
    assert [c["type"] for c in transport.sent] == ["prompt", "steer"]
    assert transport.sent[1]["message"] == "Also: what is 2+2?"
    assert result.text == "DONE\n4" and result.unread == ()
    reads = [e for e in seen if isinstance(e, MessageRead)]
    assert [r.text for r in reads] == ["Also: what is 2+2?"]
    assert not [e for e in seen if isinstance(e, Answered)]  # nothing was written before the message was read
    # The prompt's own user message is not a read of anything put to the turn.
    assert all(e.text != "Run sleep, then say DONE" for e in reads)


async def test_what_the_model_wrote_before_reading_the_message_is_its_answer_to_what_came_before(monkeypatch) -> None:
    """Probe C: a steer during a tool-less answer is read after it, and the
    loop goes on — the text before the read goes out as ``Answered``, the
    result is the answer to the message, and the wrap-up clock starts over
    at the read: the note is due ``wrap_up_after`` after it, not after the
    turn began."""
    from crucible.ports.agent.events import Answered, MessageRead
    from crucible.ports.agent.runtime import WRAP_UP_NOTE
    from crucible.runtimes.pi import session as session_module

    monkeypatch.setattr(session_module, "_TURN_POLL_INTERVAL", 0.02)  # the clocks are read often enough to tell

    transport = FakeTransport()
    seen: list[object] = []
    read_at: list[float] = []

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message("Count to 60")})
            transport.emit(_text_done("1\n2\n3"))
        elif command.get("type") == "steer" and command["message"] != WRAP_UP_NOTE:
            read_at.append(asyncio.get_running_loop().time())
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message(command["message"])})
            # The model works on the message for a while — long enough for the
            # turn's own wrap-up clock, not for a clock started at the read.
        elif command.get("type") == "steer":
            transport.emit(_text_done("BYE"))
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
            transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "BYE"}]})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("Count to 60", timeout=5.0, on_event=seen.append, wrap_up_after=0.6))
    await asyncio.sleep(0.3)  # the model is writing
    assert await session.steer("Stop and say BYE") is True
    await asyncio.sleep(0.45)  # 0.75 s into the turn, 0.45 s after the read: no note yet
    assert WRAP_UP_NOTE not in [c.get("message") for c in transport.sent]
    result = await turn  # the note goes at 0.6 s after the read and ends the run
    assert WRAP_UP_NOTE in [c.get("message") for c in transport.sent]
    assert result.text == "BYE"
    answered = [e for e in seen if isinstance(e, Answered)]
    assert [a.text for a in answered] == ["1\n2\n3"]  # the answer to what came before, on its own
    read = seen.index(next(e for e in seen if isinstance(e, MessageRead)))
    assert seen.index(answered[0]) < read  # said before the read is reported


async def test_a_message_the_run_ended_without_reading_is_taken_back_out_of_the_queue() -> None:
    """Probe B: a steer that lands after the model's final answer stays in pi's
    queue for the next prompt, where it would be read after that prompt's own
    text — so the session clears the queue and hands the message back unread."""
    transport = FakeTransport()

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message("Say HELLO")})
            transport.emit(_text_done("HELLO"))
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
        if command.get("type") == "steer":
            # pi: the loop had already decided; the steer waits for the next prompt.
            transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "HELLO"}]})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("Say HELLO", timeout=5.0))
    for _ in range(200):
        if transport.sent and transport.sent[0].get("type") == "prompt" and session._turn is not None and session._turn.text_parts:  # noqa: SLF001
            break
        await asyncio.sleep(0.005)
    assert await session.steer("Now say BYE") is True
    result = await turn
    await asyncio.sleep(0.01)  # the clear goes out as the turn ends, not awaited by it
    assert result.text == "HELLO" and result.unread == ("Now say BYE",)
    assert transport.sent_types() == ["prompt", "steer", "clear_queue"]
    assert await session.steer("after the turn") is False
    # A turn nobody steered clears nothing.
    transport2 = FakeTransport()

    def plain(command: dict) -> None:
        if command.get("type") == "prompt":
            transport2.emit({"type": "agent_start"})
            transport2.emit(_text_done("x"))
            transport2.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "x"}]})
            transport2.emit({"type": "agent_settled", "aborted": False})

    transport2._reactor = plain
    session2 = PiRpcSession(transport2)
    session2.start()
    assert (await session2.prompt("x", timeout=1.0)).unread == ()
    await asyncio.sleep(0.01)
    assert transport2.sent_types() == ["prompt"]


async def test_an_answer_already_out_is_not_the_result_again() -> None:
    """The text before a read went out as ``Answered``; if the model then ends
    without another word, the run's messages — which still hold that text —
    are not read as the result, or the person would get it twice."""
    from crucible.ports.agent.events import Answered

    transport = FakeTransport()
    seen: list[object] = []

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit({"type": "turn_start"})
            transport.emit(_text_done("The whole answer."))
        elif command.get("type") == "steer":
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})
            transport.emit({"type": "turn_start"})
            transport.emit({"type": "message_start", "message": _user_message(command["message"])})
            transport.emit({"type": "turn_end", "message": {"role": "assistant", "stopReason": "stop"}})  # nothing more said
            transport.emit({"type": "agent_end", "messages": [{"role": "assistant", "content": "The whole answer."}, {"role": "user", "content": "ok"}]})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    turn = asyncio.ensure_future(session.prompt("Tell me", timeout=5.0, on_event=seen.append))
    for _ in range(200):
        if session._turn is not None and session._turn.text_parts:  # noqa: SLF001
            break
        await asyncio.sleep(0.005)
    assert await session.steer("ok") is True
    result = await turn
    assert [e.text for e in seen if isinstance(e, Answered)] == ["The whole answer."]
    assert result.text == ""  # already out; not again
    await asyncio.sleep(0.01)
    assert "clear_queue" not in transport.sent_types()  # it was read: nothing to clear


def _assistant_turn_end(tokens: int) -> dict:
    return {"type": "turn_end", "message": {"role": "assistant", "usage": {"totalTokens": tokens}}, "toolResults": []}


async def test_a_turn_over_its_token_budget_is_told_once_and_stopped_a_quarter_past_it() -> None:
    from crucible.ports.agent.runtime import BUDGET_NOTE
    from crucible.runtimes.pi.errors import PiOverBudget

    steers: list[str] = []

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit(_assistant_turn_end(600))  # the model's first message
            transport.emit({"type": "turn_end", "message": {"role": "user", "usage": {"totalTokens": 99999}}})  # not the model's: not counted
            transport.emit(_assistant_turn_end(500))  # 1100 of 1000: told to round off
        if command.get("type") == "steer":
            steers.append(command["message"])
            transport.emit(_assistant_turn_end(300))  # 1400 ≥ 1250: stopped

    transport = FakeTransport()
    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    with pytest.raises(PiOverBudget, match="spent 1400 tokens against a budget of 1000"):
        await session.prompt("hi", timeout=5.0, tokens_per_turn=1000)
    assert steers == [BUDGET_NOTE]
    assert "abort" in transport.sent_types()  # the turn was aborted, the session is poisoned


async def test_a_turn_within_its_token_budget_is_told_nothing() -> None:
    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            transport.emit(_assistant_turn_end(400))
            transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "fine"}})
            transport.emit({"type": "agent_end", "messages": []})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport = FakeTransport()
    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()
    result = await session.prompt("hi", timeout=5.0, tokens_per_turn=1000)
    assert result.text == "fine" and "steer" not in transport.sent_types()


async def test_a_long_turn_is_told_once_to_wrap_up_and_the_answer_it_then_gives_is_the_result() -> None:
    from crucible.ports.agent.runtime import WRAP_UP_NOTE

    def react(command: dict) -> None:
        if command.get("type") == "prompt":
            transport.emit({"type": "agent_start"})
            # ...and then it works in silence that is not a stall: deltas keep coming.
        if command.get("type") == "steer":  # read before the next model call, not after the run
            assert command["message"] == WRAP_UP_NOTE
            transport.emit({"type": "message_update", "assistantMessageEvent": {"type": "text_end", "content": "Done so far: A. Left: B."}})
            transport.emit({"type": "agent_end", "messages": []})
            transport.emit({"type": "agent_settled", "aborted": False})

    transport = FakeTransport()
    transport._reactor = react
    session = PiRpcSession(transport)
    session.start()

    result = await session.prompt("hi", timeout=5.0, wrap_up_after=0.1)

    assert result.text == "Done so far: A. Left: B."
    assert transport.sent_types().count("steer") == 1
