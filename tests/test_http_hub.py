"""HttpHub over real HTTP on a local port (offline, aiohttp client)."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest

from crucible.approvals import APPROVAL_KEY, ApprovalOutcome
from crucible.attachments import AttachmentStore
from crucible.gateways.http import HttpChatClient, HttpHub, HttpTurns, TokenCallers
from crucible.gateways.http.journal import (
    STATUS_AWAITING_INPUT,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
)
from crucible.ports.agent.events import TextProgress, ToolFinished, ToolStarted
from crucible.ports.agent.runtime import RuntimeStats
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import Action, IncomingMessage
from crucible.store.base import SessionRecord

Script = Callable[[IncomingMessage, HttpChatClient], Awaitable[TurnOutcome]]


async def _reply(msg: IncomingMessage, chat: HttpChatClient) -> TurnOutcome:
    await chat.post_reply(msg.ref, f"you said: {msg.text}")
    return TurnOutcome.REPLIED


class FakeSink:
    """A TrackedSink whose turn is a scripted coroutine instead of a flow."""

    def __init__(self, script: Script = _reply) -> None:
        self.script = script
        self.submitted: list[IncomingMessage] = []

    def submit(self, msg, chat) -> None:
        self.submit_tracked(msg, chat)

    def submit_tracked(self, msg, chat) -> asyncio.Future:
        self.submitted.append(msg)
        return asyncio.ensure_future(self.script(msg, chat))


class FakeDispatcher:
    def __init__(self) -> None:
        self.pending: list[tuple[str, str]] = []
        self.consumed: list[tuple[str, str, str, str]] = []
        self.approvals: list[tuple[str, str, str, str]] = []
        self.approval_outcome = ApprovalOutcome.RESOLVED
        self.resolves = True

    async def resolve_approval(self, token, value, user_id, *, gateway=""):
        self.approvals.append((token, value, user_id, gateway))
        return self.approval_outcome

    answerers: set[str] | None = None  # who may answer any token; None = anyone

    async def may_answer(self, token, user_id, *, gateway=""):
        return self.answerers is None or user_id in self.answerers

    async def resolve_pending(self, token, value) -> bool:
        self.pending.append((token, value))
        return self.resolves

    async def consume_action(self, token, value, user_id, *, pick=""):
        self.consumed.append((token, value, user_id, pick))

    async def redraw_screen(self, state_raw, value, *, post_id, user_id):
        from crucible.interactions.screens import Redrawn

        return Redrawn(f"for {user_id} only" if value == "whisper" else "")


class FakeControl:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.busy = 0

    async def cancel(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    def stats(self) -> RuntimeStats:
        return RuntimeStats(alive=self.busy, busy=self.busy, capacity=2, waiting=0)


class FakeDirectory:
    def agent_user_ids(self):
        return frozenset()

    def list_agents(self):
        return [AgentInfo("helper", "helps", "a helper", "", "http:helper")]


SERVICES = {"portal": ("tok-portal", None), "narrow": ("tok-narrow", ("scribe",)), "other": ("tok-other", None)}
AUTH = {"Authorization": "Bearer tok-portal", "X-User-Id": "u-7", "X-Username": "alice"}


def _hub(port: int, **kw) -> tuple[HttpHub, FakeSink]:
    sink = FakeSink(kw.pop("script", _reply))
    hub = HttpHub(
        "127.0.0.1", port, TokenCallers(SERVICES), directory=FakeDirectory(), max_wait_s=0.2, **kw
    )
    hub.register_agent("helper", sink, HttpChatClient(hub.turns, "helper"))
    return hub, sink


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


async def _post(s, port, path, body, headers=AUTH):
    async with s.post(_url(port, path), json=body, headers=headers) as r:
        return r.status, await r.json()


async def _get(s, port, path, headers=AUTH):
    async with s.get(_url(port, path), headers=headers) as r:
        return r.status, await r.json()


async def _events(s, port, turn_id, after=0, wait=0.2):
    return await _get(s, port, f"/v1/turns/{turn_id}/events?after={after}&wait={wait}")


async def _drain(s, port, turn_id, timeout=2.0) -> tuple[list[dict], str]:
    """Poll until the turn has finished; the events in order, and the status."""
    events: list[dict] = []
    after = 0
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        _, page = await _events(s, port, turn_id, after=after)
        events += page["events"]
        after = page["cursor"]
        if events and events[-1]["type"] == "turn.finished":
            return events, page["status"]
    raise AssertionError(f"the turn did not finish: {events}")


MESSAGES = "/v1/agents/helper/conversations/c1/messages"


async def test_a_message_becomes_a_turn_whose_journal_the_client_reads() -> None:
    hub, sink = _hub(8480)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, started = await _post(s, 8480, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            assert status == 202 and started["cursor"] == 0 and started["conversationId"] == "c1"
            events, final = await _drain(s, 8480, started["turnId"])
            assert [e["type"] for e in events] == ["turn.started", "message", "turn.finished"]
            assert events[1]["text"] == "you said: hi" and events[1]["format"] == "markdown"
            assert events[2]["outcome"] == "replied" and final == STATUS_DONE
            assert [e["seq"] for e in events] == [1, 2, 3]

            # The message reached the sink in the neutral shape, realm-namespaced.
            msg = sink.submitted[0]
            assert msg.conversation_id == "portal:c1" and msg.ref.message_id == "portal:m1"
            assert msg.user_id == "u-7" and msg.username == "alice" and msg.is_dm
            # Stamped on arrival: the speaker line's time is the model's "now".
            assert msg.timestamp is not None
            assert abs(datetime.now(timezone.utc) - msg.timestamp) < timedelta(seconds=5)

            # Over: the conversation has no active turn, and the journal still answers.
            _, conv = await _get(s, 8480, "/v1/agents/helper/conversations/c1")
            assert conv == {"conversationId": "c1", "activeTurn": None, "pendingCards": []}
            _, again = await _events(s, 8480, started["turnId"], after=3)
            assert again["events"] == [] and again["cursor"] == 3 and again["status"] == STATUS_DONE
    finally:
        await hub.stop()


async def test_busy_is_explicit_and_a_retry_is_the_same_turn() -> None:
    release = asyncio.Event()

    async def slow(msg, chat):
        await release.wait()
        return TurnOutcome.ACTED

    hub, _ = _hub(8481, script=slow)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, first = await _post(s, 8481, MESSAGES, {"clientMessageId": "m1", "text": "one"})
            assert status == 202
            # The same client message again: not a second turn.
            status, retry = await _post(s, 8481, MESSAGES, {"clientMessageId": "m1", "text": "one"})
            assert status == 200 and retry["turnId"] == first["turnId"]
            # Another message while the turn runs: refused, with the turn to follow.
            status, busy = await _post(s, 8481, MESSAGES, {"clientMessageId": "m2", "text": "two"})
            assert status == 409 and busy["error"]["code"] == "turn_in_progress"
            assert busy["turnId"] == first["turnId"]
            # Resuming after a reload: the conversation names the running turn.
            _, conv = await _get(s, 8481, "/v1/agents/helper/conversations/c1")
            assert conv["activeTurn"]["turnId"] == first["turnId"]
            assert conv["activeTurn"]["status"] == "running" and conv["activeTurn"]["cursor"] == 1
            # Another conversation of the same caller is not blocked.
            status, _ = await _post(
                s, 8481, "/v1/agents/helper/conversations/c2/messages",
                {"clientMessageId": "m1", "text": "elsewhere"},
            )
            assert status == 202
            release.set()
            events, _ = await _drain(s, 8481, first["turnId"])
            assert events[-1]["outcome"] == "acted"
    finally:
        await hub.stop()


async def test_a_waiting_poll_is_capped_whatever_the_client_asks() -> None:
    async def forever(msg, chat):
        await asyncio.Event().wait()
        return TurnOutcome.REPLIED

    hub, _ = _hub(8482, script=forever)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8482, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            began = asyncio.get_running_loop().time()
            _, page = await _events(s, 8482, started["turnId"], after=1, wait=30)
            assert page["events"] == [] and page["cursor"] == 1
            assert asyncio.get_running_loop().time() - began < 1.0  # max_wait_s is 0.2
    finally:
        await hub.stop()


async def test_the_runtimes_tool_events_reach_the_journal() -> None:
    async def tools(msg, chat):
        assert msg.gateway == "http"  # the hub stamps what it made
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm",
                               "helper--portal-c1-abc", "t", "t")
        trace = hub.turns.begin(record, chat)  # what AgentFlow does with its tracer
        trace.on_event(TextProgress("Searching the docs first"))
        trace.on_event(ToolStarted("call-1", "search", {"q": "x"}, caption="Searching for x"))
        trace.on_event(ToolFinished("call-1", "search", is_error=True))
        await trace.end()
        return TurnOutcome.ACTED

    hub, _ = _hub(8483, script=tools)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8483, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            events, _ = await _drain(s, 8483, started["turnId"])
            assert [e["type"] for e in events] == [
                "turn.started", "progress", "tool.started", "tool.finished", "turn.finished",
            ]
            assert events[1]["text"] == "Searching the docs first"
            assert events[2] == {**events[2], "callId": "call-1", "tool": "search", "caption": "Searching for x"}
            assert events[3]["ok"] is False
    finally:
        await hub.stop()


async def test_a_card_is_an_event_and_a_click_reaches_the_dispatcher() -> None:
    answered = asyncio.Event()

    async def asks(msg, chat):
        post_id = await chat.post_actions(
            msg.ref, "Allow?",
            [Action(id="yes", label="Allow", value="Allow", context={"token": "tk-1"})],
            callback_url="",
        )
        assert post_id
        await answered.wait()
        await chat.retract(post_id, "done")  # the gate retires its own card
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8484, script=asks, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8484, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8484, started["turnId"], after=1, wait=0.2)
            card = page["events"][0]
            assert card["type"] == "actions" and card["text"] == "Allow?"
            assert card["actions"][0] == {
                "id": "yes", "label": "Allow", "value": "Allow", "style": "", "kind": "button", "options": [],
            }
            assert page["status"] == STATUS_AWAITING_INPUT

            status, answer = await _post(
                s, 8484, f"/v1/turns/{started['turnId']}/actions",
                {"postId": card["postId"], "actionId": "yes", "value": "Allow"},
            )
            assert status == 200 and answer == {"outcome": "resolved"}
            assert dispatcher.pending == [("tk-1", "Allow")]
            # The same control again: it was retired by the click.
            status, _ = await _post(
                s, 8484, f"/v1/turns/{started['turnId']}/actions",
                {"postId": card["postId"], "actionId": "yes", "value": "Allow"},
            )
            assert status == 404
            answered.set()
            events, _ = await _drain(s, 8484, started["turnId"])
            assert [e["type"] for e in events][2:] == ["actions.retired", "turn.finished"]
    finally:
        await hub.stop()


async def test_a_click_on_a_credential_request_is_an_approval() -> None:
    async def asks(msg, chat):
        await chat.post_actions(
            msg.ref, "May helper use the token?",
            [Action(id="once", label="Allow once", value="once", context={APPROVAL_KEY: "ap-9"})],
            callback_url="",
        )
        await asyncio.sleep(0.3)
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    dispatcher.approval_outcome = ApprovalOutcome.NOT_ALLOWED
    hub, _ = _hub(8485, script=asks, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8485, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8485, started["turnId"], after=1, wait=0.2)
            post_id = page["events"][0]["postId"]
            status, answer = await _post(
                s, 8485, f"/v1/turns/{started['turnId']}/actions",
                {"postId": post_id, "actionId": "once", "value": "once"},
            )
            assert (status, answer) == (200, {"outcome": "not_allowed"})
            assert dispatcher.approvals == [("ap-9", "once", "u-7", "http")]
            assert dispatcher.pending == []  # never mistaken for a plain widget
    finally:
        await hub.stop()


async def test_who_may_call_and_read_what() -> None:
    async def forever(msg, chat):
        await asyncio.Event().wait()
        return TurnOutcome.REPLIED

    hub, _ = _hub(8486, script=forever)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _post(s, 8486, MESSAGES, {"clientMessageId": "m1", "text": "hi"},
                                       headers={"Authorization": "Bearer nope"})
            assert status == 401 and body["error"]["code"] == "unauthorized"
            status, body = await _post(s, 8486, MESSAGES, {"clientMessageId": "m1", "text": "hi"},
                                       headers={"Authorization": "Bearer tok-narrow"})
            assert status == 403  # this caller may only address `scribe`
            status, body = await _post(s, 8486, MESSAGES, {"text": "hi"})
            assert status == 422 and "clientMessageId" in body["error"]["message"]

            _, started = await _post(s, 8486, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            other = {**AUTH, "X-User-Id": "u-8"}
            status, _ = await _get(s, 8486, f"/v1/turns/{started['turnId']}/events", headers=other)
            assert status == 404  # another person's turn does not exist for me
            _, conv = await _get(s, 8486, "/v1/agents/helper/conversations/c1", headers=other)
            assert conv["activeTurn"] is None
            _, agents = await _get(s, 8486, "/v1/agents")
            assert agents == {"agents": [{"name": "helper", "role": "helps", "description": "a helper"}]}
    finally:
        await hub.stop()


async def test_cancel_interrupts_the_running_turns_session() -> None:
    async def running(msg, chat):
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm",
                               "helper--portal-c1", "t", "t")
        hub.turns.begin(record, chat)
        await asyncio.sleep(0.3)
        return TurnOutcome.REPLIED

    control = FakeControl()
    hub, _ = _hub(8487, script=running, control=control)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, _ = await _post(s, 8487, "/v1/agents/helper/conversations/c1/cancel", {})
            assert status == 404  # nothing running
            _, started = await _post(s, 8487, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            await asyncio.sleep(0.05)
            status, answer = await _post(s, 8487, "/v1/agents/helper/conversations/c1/cancel", {})
            assert (status, answer) == (200, {"turnId": started["turnId"], "cancelled": True})
            assert control.cancelled == ["helper--portal-c1"]
    finally:
        await hub.stop()


async def test_readiness_is_not_load() -> None:
    """A pod whose every runtime slot is busy is a busy pod, not a broken one:
    taking it out of the endpoints would move its users away from the turns
    running on it. Load is reported on /stats for whoever scales."""
    release = asyncio.Event()

    async def slow(msg, chat):
        await release.wait()
        return TurnOutcome.REPLIED

    control = FakeControl()
    hub, _ = _hub(8488, control=control, script=slow)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _get(s, 8488, "/healthz", headers={})
            assert (status, body) == (200, {"ok": True})
            control.busy = 2  # every slot taken
            status, body = await _get(s, 8488, "/readyz", headers={})
            assert (status, body) == (200, {"ready": True})
            await _post(s, 8488, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            status, body = await _get(s, 8488, "/stats", headers={})
            assert status == 200
            assert body["runtime"] == {"alive": 2, "busy": 2, "capacity": 2, "waiting": 0}
            assert body["turns"] == {"active": 1}
            release.set()
    finally:
        await hub.stop()


async def test_stats_without_a_runtime_control_still_count_turns() -> None:
    hub, _ = _hub(8499)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _get(s, 8499, "/stats", headers={})
            assert (status, body) == (200, {"runtime": None, "turns": {"active": 0}})
    finally:
        await hub.stop()


async def test_a_failed_turn_says_so_and_a_file_travels_inline(tmp_path: Path) -> None:
    async def fails(msg, chat):
        assert msg.attachments and msg.attachments[0].name == "note.txt"
        assert Path(msg.attachments[0].path).read_bytes() == b"hello"
        await chat.post_notice(msg.ref, "the model timed out", code="timeout")
        return TurnOutcome.TIMEOUT

    hub, _ = _hub(8489, script=fails, attachments=AttachmentStore(tmp_path, max_bytes=1_000_000, retention_days=1))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8489, MESSAGES, {
                "clientMessageId": "m1", "text": "",
                "files": [{"name": "note.txt", "mime": "text/plain",
                           "data": base64.b64encode(b"hello").decode()}],
            })
            events, final = await _drain(s, 8489, started["turnId"])
            assert events[1] == {**events[1], "type": "notice", "code": "timeout"}
            assert events[-1]["outcome"] == "timeout" and final == STATUS_FAILED
    finally:
        await hub.stop()


async def test_finished_journals_are_forgotten_after_the_retention() -> None:
    turns = HttpTurns(journal_retention_s=0.0)
    turn, created = turns.start(agent="a", conversation="r:c", owner="r/u", client_message_id="m")
    assert created and turns.active("r:c") is turn
    turns.finish(turn, TurnOutcome.REPLIED)
    assert turns.active("r:c") is None and turns.get(turn.id) is turn
    await asyncio.sleep(0.01)
    assert turns.sweep(asyncio.get_running_loop().time()) == 1
    assert turns.get(turn.id) is None
    # The message id is remembered but its turn is gone: a retry is a new turn.
    again, created = turns.start(agent="a", conversation="r:c", owner="r/u", client_message_id="m")
    assert created and again.id != turn.id


def test_a_hub_rejects_nothing_it_does_not_understand_in_the_contract() -> None:
    # Pinned so the contract doc and the code agree on the version header.
    from crucible.gateways.http.hub import API_VERSION

    assert API_VERSION == "1.0"


@pytest.mark.parametrize(("wait", "expected"), [("abc", 422), ("-1", 200)])
async def test_bad_query_values_are_validation_errors(wait: str, expected: int) -> None:
    hub, _ = _hub(8490)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8490, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            status, body = await _get(s, 8490, f"/v1/turns/{started['turnId']}/events?wait={wait}")
            assert status == expected  # a negative wait is clamped; a word is refused
            if status == 422:
                assert body["error"]["code"] == "validation"
    finally:
        await hub.stop()


async def test_a_body_too_large_is_a_413_in_the_envelope() -> None:
    hub, _ = _hub(8491)  # no attachment store: the text-only cap applies
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _post(s, 8491, MESSAGES, {"clientMessageId": "m1", "text": "x" * (1024 * 1024 + 10)})
            assert status == 413 and body["error"]["code"] == "too_large"
            async with s.get(_url(8491, "/no/such/route"), headers=AUTH) as r:
                assert r.status == 404 and r.headers["X-Engine-Api-Version"] == "1.0"
            status, body = await _post(s, 8491, MESSAGES, {"clientMessageId": "m1", "text": "hi"}, headers={})
            assert status == 401  # before any parsing
    finally:
        await hub.stop()


async def test_the_body_cap_follows_the_attachment_limit(tmp_path: Path) -> None:
    hub, _ = _hub(8492, attachments=AttachmentStore(tmp_path, max_bytes=3_000_000, retention_days=1))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            payload = base64.b64encode(b"x" * 2_000_000).decode()
            status, _ = await _post(s, 8492, MESSAGES, {
                "clientMessageId": "m1", "text": "a big file",
                "files": [{"name": "big.bin", "mime": "application/octet-stream", "data": payload}],
            })
            assert status == 202
    finally:
        await hub.stop()


@pytest.mark.parametrize("files", ["not-a-list", [{"data": "aGk="}], [{"name": "x"}], ["x"], 7])
async def test_malformed_files_are_validation_errors(files) -> None:
    hub, _ = _hub(8493)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _post(s, 8493, MESSAGES, {"clientMessageId": "m1", "text": "hi", "files": files})
            assert status == 422 and body["error"]["code"] == "validation"
    finally:
        await hub.stop()


async def test_an_answer_is_the_controls_value_not_the_callers() -> None:
    answered = asyncio.Event()

    async def asks(msg, chat):
        from crucible.ports.chat.types import ACTION_SELECT, Choice

        await chat.post_actions(
            msg.ref, "Allow?",
            [Action(id="yes", label="Allow", value="Allow", context={"token": "tk-1"}),
             Action(id="how-long", label="For…", kind=ACTION_SELECT,
                    options=Choice.of("5m", "15m"), context={"token": "tk-1"})],
            callback_url="",
        )
        await answered.wait()
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8494, script=asks, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8494, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8494, started["turnId"], after=1, wait=0.2)
            post_id = page["events"][0]["postId"]
            actions = f"/v1/turns/{started['turnId']}/actions"
            status, body = await _post(s, 8494, actions, {"postId": post_id, "actionId": "how-long", "value": "1 year"})
            assert status == 422  # not one of the menu's options
            status, _ = await _post(s, 8494, actions, {"postId": post_id, "actionId": "yes", "value": "Deny"})
            assert status == 200 and dispatcher.pending == [("tk-1", "Allow")]  # the button's own value
            answered.set()
    finally:
        await hub.stop()


async def test_a_card_left_over_from_a_finished_turn_answers_nobody() -> None:
    async def asks_and_leaves(msg, chat):
        await chat.post_actions(
            msg.ref, "Pick one", [Action(id="a", label="A", value="a", context={"token": "tk-9"})],
            callback_url="",
        )
        return TurnOutcome.ACTED  # the card stays up; the turn is over

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8495, script=asks_and_leaves, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8495, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            events, final = await _drain(s, 8495, started["turnId"])
            post_id = next(e["postId"] for e in events if e["type"] == "actions")
            assert events[-1]["type"] == "turn.finished"
            status, _ = await _post(s, 8495, f"/v1/turns/{started['turnId']}/actions",
                                    {"postId": post_id, "actionId": "a"})
            assert status == 404 and dispatcher.pending == []
            # ...and the journal did not grow past its last event.
            _, page = await _events(s, 8495, started["turnId"], after=len(events))
            assert page["events"] == []
    finally:
        await hub.stop()


async def test_a_question_nobody_waits_on_any_more_is_retired_not_answered() -> None:
    hold = asyncio.Event()

    async def asks(msg, chat):
        await chat.post_actions(
            msg.ref, "Allow?", [Action(id="yes", label="Allow", value="Allow", context={"token": "gone"})],
            callback_url="",
        )
        await hold.wait()
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    dispatcher.resolves = False  # the token is no longer pending (timed out, withdrawn)
    hub, _ = _hub(8496, script=asks, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8496, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8496, started["turnId"], after=1, wait=0.2)
            post_id = page["events"][0]["postId"]
            status, _ = await _post(s, 8496, f"/v1/turns/{started['turnId']}/actions",
                                    {"postId": post_id, "actionId": "yes"})
            assert status == 404
            _, page = await _events(s, 8496, started["turnId"], after=2, wait=0.2)
            assert page["events"][0]["type"] == "actions.retired" and page["status"] == "running"
            hold.set()
    finally:
        await hub.stop()


async def test_a_drawn_screen_does_not_make_the_turn_wait() -> None:
    from crucible.ports.chat.types import Card

    async def draws(msg, chat):
        await chat.post_cards(msg.ref, [Card("a list", actions=(Action(id="n", label="Next", context={"screen": "s", "state": "1"}),))], callback_url="")
        post = await chat.post_actions(msg.ref, "Allow?", [Action(id="y", label="Allow", value="Allow", context={"token": "t"})], callback_url="")
        await asyncio.sleep(0.1)
        await chat.retract(post, "done")
        await asyncio.sleep(0.1)
        return TurnOutcome.ACTED

    hub, _ = _hub(8497, script=draws)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8497, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8497, started["turnId"], after=2, wait=0.2)  # cards, then actions
            assert page["status"] == STATUS_AWAITING_INPUT
            _, page = await _events(s, 8497, started["turnId"], after=3, wait=0.3)  # the retract
            assert page["events"][0]["type"] == "actions.retired"
            assert page["status"] == "running"  # the screen still up does not block
    finally:
        await hub.stop()


async def test_an_authenticator_may_refuse_with_its_own_status_and_code() -> None:
    from crucible.gateways.http import CallerRefused

    class _Refuses:
        async def authenticate(self, request, body):
            if request.headers.get("X-User-Id") == "over-quota":
                raise CallerRefused(429, "rate_limited", "too many messages; wait a few minutes",
                                    retryAfter=60)
            return None

    sink = FakeSink()
    hub = HttpHub("127.0.0.1", 8498, _Refuses(), directory=FakeDirectory(), max_wait_s=0.2)
    hub.register_agent("helper", sink, HttpChatClient(hub.turns, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _post(s, 8498, MESSAGES, {"clientMessageId": "m1", "text": "hi"},
                                       headers={"Authorization": "Bearer x", "X-User-Id": "over-quota"})
            assert status == 429
            assert body == {"error": {"code": "rate_limited", "message": "too many messages; wait a few minutes"},
                            "retryAfter": 60}
            async with s.post(_url(8498, MESSAGES), json={"clientMessageId": "m1", "text": "hi"},
                              headers={"Authorization": "Bearer x", "X-User-Id": "over-quota"}) as r:
                assert r.headers["X-Engine-Api-Version"] == "1.0"
            status, body = await _post(s, 8498, MESSAGES, {"clientMessageId": "m1", "text": "hi"},
                                       headers={"Authorization": "Bearer x", "X-User-Id": "someone"})
            assert status == 401  # None still means the plain refusal
    finally:
        await hub.stop()


async def test_token_callers_accept_only_the_exact_token() -> None:
    callers = TokenCallers({"portal": ("tok-portal", None), "narrow": ("tok-narrow", ("scribe",))})

    from typing import cast

    from aiohttp import web

    class _Request:
        def __init__(self, token: str) -> None:
            self.headers = {"Authorization": f"Bearer {token}"}

    def req(token: str) -> web.Request:
        return cast(web.Request, _Request(token))  # only the headers are read

    portal = await callers.authenticate(req("tok-portal"), None)
    narrow = await callers.authenticate(req("tok-narrow"), None)
    assert portal is not None and portal.realm == "portal"
    assert narrow is not None and narrow.agents == ("scribe",)
    assert await callers.authenticate(req("tok-portal-and-more"), None) is None
    assert await callers.authenticate(req("tok-porta"), None) is None
    assert await callers.authenticate(req("тёкен"), None) is None  # Non-ASCII on purpose
    assert await callers.authenticate(req(""), None) is None


async def test_the_turns_id_rides_on_the_messages_scope() -> None:
    from crucible.gateways.http import Caller
    from crucible.ports.turn import TurnScope, TurnSecrets

    # One scope object for the caller, as an authenticator that caches what it
    # resolved for a token would hand back.
    shared = TurnScope("", TurnSecrets({"cookie": "c"}), {"locale": "ru"})

    class _WithScope:
        async def authenticate(self, request, body):
            return Caller(realm="portal", user_id="u-1", turn=shared)

    seen: list[IncomingMessage] = []

    async def record(msg, chat):
        seen.append(msg)
        await chat.post_reply(msg.ref, "stored as well")
        return TurnOutcome.REPLIED

    hub = HttpHub("127.0.0.1", 8500, _WithScope(), directory=FakeDirectory(), max_wait_s=0.2)
    hub.register_agent("helper", FakeSink(record), HttpChatClient(hub.turns, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8500, MESSAGES, {"clientMessageId": "m1", "text": "hi"},
                                     headers={"Authorization": "Bearer any"})
            events, _ = await _drain(s, 8500, started["turnId"])
            scope = seen[0].turn
            assert scope is not None and scope.turn_id == started["turnId"]
            assert scope.secrets.get("cookie") == "c" and scope.attributes == {"locale": "ru"}  # kept
            assert any(e["type"] == "message" for e in events)
            # A flag a tool leaves is this turn's: the next turn of the same
            # caller — the same scope object from the authenticator — starts clean.
            scope.flag("session_expired")
            _, again = await _post(s, 8500, MESSAGES, {"clientMessageId": "m2", "text": "more"},
                                   headers={"Authorization": "Bearer any"})
            await _drain(s, 8500, again["turnId"])
            assert seen[1].turn is not None and seen[1].turn.flags == set()
    finally:
        await hub.stop()


async def test_a_caller_without_a_scope_still_gets_the_turn_id() -> None:
    seen: list[IncomingMessage] = []

    async def record(msg, chat):
        seen.append(msg)
        await chat.post_reply(msg.ref, "ok")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8501, script=record)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8501, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            events, _ = await _drain(s, 8501, started["turnId"])
            assert seen[0].turn is not None and seen[0].turn.turn_id == started["turnId"]
            assert len(next(e for e in events if e["type"] == "message")["messageId"]) == 32
    finally:
        await hub.stop()


# -- an application's own routes, and draining ------------------------------------


async def test_an_applications_routes_live_on_the_hubs_port_under_its_envelope() -> None:
    from aiohttp import web

    hub_holder: list[HttpHub] = []

    async def dialog(request: web.Request) -> web.Response:
        hub = hub_holder[0]
        caller = await hub.caller(request, None)  # 401 in the envelope without a token
        key = hub.conversation_key(caller, request.match_info["conversation"])
        if request.query.get("missing"):
            raise hub.refuse(404, "no_dialog", "nothing stored yet", conversationKey=key)
        return web.json_response({"key": key, "user": caller.user_id, "cap": hub.max_body})

    @web.middleware
    async def served_by(request, handler):
        response = await handler(request)
        response.headers["X-Served-By"] = "pod-7"
        return response

    sink = FakeSink()
    hub = HttpHub(
        "127.0.0.1", 8502, TokenCallers(SERVICES), directory=FakeDirectory(), max_wait_s=0.2,
        routes=[web.get("/v1/me/{conversation}/dialog", dialog)], middlewares=[served_by],
    )
    hub_holder.append(hub)
    hub.register_agent("helper", sink, HttpChatClient(hub.turns, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(_url(8502, "/v1/me/c1/dialog"), headers=AUTH) as r:
                body = await r.json()
                assert r.status == 200 and body == {"key": "portal:c1", "user": "u-7", "cap": 1024 * 1024}
                assert r.headers["X-Served-By"] == "pod-7"
                assert r.headers["X-Engine-Api-Version"] == "1.0"
            async with s.get(_url(8502, "/v1/me/c1/dialog?missing=1"), headers=AUTH) as r:
                assert r.status == 404
                assert await r.json() == {"error": {"code": "no_dialog", "message": "nothing stored yet"},
                                          "conversationKey": "portal:c1"}
            async with s.get(_url(8502, "/v1/me/c1/dialog"), headers={}) as r:
                assert r.status == 401 and (await r.json())["error"]["code"] == "unauthorized"
            # The hub's own routes carry the application's middleware too.
            async with s.get(_url(8502, "/healthz")) as r:
                assert r.headers["X-Served-By"] == "pod-7"
    finally:
        await hub.stop()


async def test_a_draining_hub_takes_no_new_turns_but_finishes_the_running_ones() -> None:
    release = asyncio.Event()

    async def slow(msg, chat):
        await release.wait()
        await chat.post_reply(msg.ref, "done")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8503, script=slow)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8503, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            assert await hub.idle(0.05) is False  # a turn is running

            hub.drain()
            status, body = await _post(s, 8503, "/v1/agents/helper/conversations/c2/messages",
                                       {"clientMessageId": "m2", "text": "new"})
            assert status == 503 and body["error"]["code"] == "draining"
            status, body = await _get(s, 8503, "/readyz", headers={})
            assert status == 503 and body["reason"] == "draining"
            # Those already here are still served: polls, and the turn itself.
            status, page = await _events(s, 8503, started["turnId"], after=0, wait=0)
            assert status == 200 and page["status"] == "running"

            waiter = asyncio.ensure_future(hub.idle(2.0))
            release.set()
            assert await waiter is True
            events, _ = await _drain(s, 8503, started["turnId"])
            assert events[-1]["outcome"] == "replied"
    finally:
        await hub.stop()


async def test_a_confirmation_arrives_as_data_not_as_a_rendered_card() -> None:
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    hold = asyncio.Event()

    async def asks(msg, chat):
        await chat.post_confirmation(
            msg.ref,
            ApprovalCard(
                agent="helper", headline="wants to run jira_transition_issue — move IPA-7 to Done",
                icon="⚠️", title="move IPA-7", tool="dangerous",
                fields=(PreviewRow("Status", "Done", before="Open"),),
                block_label="Arguments", block='{"key": "IPA-7"}', danger=True,
                expires_at="2026-10-04T12:00:00+00:00",
            ),
            [Action(id="yes", label="Allow", value="Allow", context={"token": "tk"})],
            callback_url="",
        )
        await hold.wait()
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8504, script=asks, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8504, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8504, started["turnId"], after=1, wait=0.2)
            card = page["events"][0]
            assert card["type"] == "confirmation" and "text" not in card
            assert card["agent"] == "helper" and card["icon"] == "⚠️" and card["danger"] is True
            assert card["headline"] == "wants to run jira_transition_issue — move IPA-7 to Done"
            assert card["fields"] == [{"label": "Status", "value": "Done", "before": "Open", "passage": False}]
            assert card["title"] == "move IPA-7" and card["tool"] == "dangerous"  # their own fields, no label to parse
            assert card["block"] == {"label": "Arguments", "text": '{"key": "IPA-7"}'}
            assert card["expiresAt"] == "2026-10-04T12:00:00+00:00"
            assert (card["verdict"], card["grantS"]) == ("", 0)  # still asking
            assert card["actions"][0]["id"] == "yes" and page["status"] == STATUS_AWAITING_INPUT
            # Answered like any card: by postId and actionId.
            status, _ = await _post(s, 8504, f"/v1/turns/{started['turnId']}/actions",
                                    {"postId": card["postId"], "actionId": "yes"})
            assert status == 200 and dispatcher.pending == [("tk", "Allow")]
            hold.set()
    finally:
        await hub.stop()


async def test_an_updated_confirmation_is_the_same_post_rewritten_as_data() -> None:
    """The verdict is a card too: the client replaces the post it drew, and with
    no actions left the turn is no longer waiting on a human."""
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    answered, go_on = asyncio.Event(), asyncio.Event()

    async def asks(msg, chat):
        post_id = await chat.post_confirmation(
            msg.ref, ApprovalCard(agent="helper", headline="is asking for a secret."),
            [Action(id="yes", label="Allow", value="Allow", context={"token": "tk"})],
            callback_url="",
        )
        await answered.wait()
        await chat.update_confirmation(
            post_id,
            ApprovalCard(
                agent="helper", headline="— Allowed for 15 min.", icon="🔐",
                fields=(PreviewRow("Secret", "github-token"),),
                block_label="Command", block="gh release create v1",
                verdict="allowed_for", grant_s=900,
            ),
        )
        await go_on.wait()  # the turn is still running: what does the client see meanwhile?
        return TurnOutcome.ACTED

    hub, _ = _hub(8505, script=asks)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8505, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8505, started["turnId"], after=1, wait=0.2)
            card = page["events"][0]
            assert card["type"] == "confirmation" and page["status"] == STATUS_AWAITING_INPUT
            answered.set()
            _, page = await _events(s, 8505, started["turnId"], after=2, wait=0.5)
            assert page["status"] == STATUS_RUNNING  # nobody is waited on any more
            updated = next(e for e in page["events"] if e["type"] == "confirmation.updated")
            assert updated["postId"] == card["postId"]
            assert updated["headline"] == "— Allowed for 15 min." and updated["actions"] == []
            # The verdict as data too: a client draws its own line from it.
            assert (updated["verdict"], updated["grantS"]) == ("allowed_for", 900)
            assert updated["fields"] == [{"label": "Secret", "value": "github-token", "before": "", "passage": False}]
            assert updated["block"] == {"label": "Command", "text": "gh release create v1"}
            assert "actions.retired" not in [e["type"] for e in page["events"]]
            # Answering the retired post is refused: nothing is pending on it.
            status, _ = await _post(s, 8505, f"/v1/turns/{started['turnId']}/actions",
                                    {"postId": card["postId"], "actionId": "yes"})
            assert status == 404
            go_on.set()
    finally:
        await hub.stop()


async def test_a_card_with_nothing_to_click_does_not_hold_the_turn() -> None:
    """A notice or a verdict is a card too — told, not asked. The turn keeps
    running, and the post stays known so a later rewrite still finds it."""
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    go_on = asyncio.Event()

    async def tells(msg, chat):
        post_id = await chat.post_confirmation(
            msg.ref, ApprovalCard(agent="helper", headline="took a secret automatically.", icon="🤖"),
            [], callback_url="",
        )
        await chat.update_confirmation(
            post_id, ApprovalCard(agent="helper", headline="took a secret automatically.",
                                  fields=(PreviewRow("Times", "2 so far"),)),
        )
        await go_on.wait()
        return TurnOutcome.ACTED

    hub, _ = _hub(8506, script=tells)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8506, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8506, started["turnId"], after=1, wait=0.3)
            kinds = [e["type"] for e in page["events"]]
            assert kinds == ["confirmation", "confirmation.updated"]
            assert page["events"][0]["actions"] == [] and page["status"] == STATUS_RUNNING
            assert page["events"][1]["fields"] == [{"label": "Times", "value": "2 so far", "before": "", "passage": False}]
            go_on.set()
    finally:
        await hub.stop()


async def test_a_retry_during_a_drain_finds_the_turn_it_started() -> None:
    """Draining refuses new turns, not the retry of a message already answered:
    a client that lost the 202 must follow its turn here, not start it again
    on another pod."""
    release = asyncio.Event()

    async def slow(msg, chat):
        await release.wait()
        return TurnOutcome.ACTED

    hub, _ = _hub(8507, script=slow)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8507, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            hub.drain()
            status, body = await _post(s, 8507, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            assert (status, body["turnId"]) == (200, started["turnId"])
            status, body = await _post(s, 8507, MESSAGES, {"clientMessageId": "m2", "text": "new"})
            assert status == 503 and body["error"]["code"] == "draining"
            release.set()
    finally:
        await hub.stop()


async def test_a_hub_started_again_takes_messages_again() -> None:
    hub, _ = _hub(8508)
    await hub.start()
    hub.drain()
    await hub.stop()
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, _ = await _get(s, 8508, "/readyz", headers={})
            assert status == 200
            status, _ = await _post(s, 8508, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            assert status == 202
    finally:
        await hub.stop()


async def test_the_applications_own_probes_decide_readiness_too(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency the application cannot serve without takes the pod out of
    rotation, with its reason; one that raises or hangs does too, and what it
    raised goes to the log, not to the probe."""
    state: dict[str, str | None] = {"store": None}

    async def store() -> str | None:
        return state["store"]

    async def broken() -> str | None:
        raise RuntimeError("mongodb://admin:hunter2@db")

    async def stuck() -> str | None:
        await asyncio.sleep(10)
        return None

    hub, _ = _hub(8509, readiness_probes=[store])
    flaky, _ = _hub(8510, readiness_probes=[broken])
    await hub.start()
    await flaky.start()
    try:
        async with aiohttp.ClientSession() as s:
            assert await _get(s, 8509, "/readyz", headers={}) == (200, {"ready": True})
            state["store"] = "dialog store unreachable"
            assert await _get(s, 8509, "/readyz", headers={}) == (
                503, {"ready": False, "reason": "dialog store unreachable"},
            )
            with caplog.at_level("WARNING", logger="crucible.gateways.http.hub"):
                status, body = await _get(s, 8510, "/readyz", headers={})
            assert (status, body) == (503, {"ready": False, "reason": "broken: failed"})
            assert "hunter2" not in str(body) and "broken" in caplog.text
    finally:
        await hub.stop()
        await flaky.stop()

    monkeypatch.setattr("crucible.gateways.http.hub.READINESS_PROBE_TIMEOUT_S", 0.05)
    slow, _ = _hub(8509, readiness_probes=[stuck])
    await slow.start()
    try:
        async with aiohttp.ClientSession() as s:
            assert await _get(s, 8509, "/readyz", headers={}) == (503, {"ready": False, "reason": "stuck: timed out"})
    finally:
        await slow.stop()


async def test_what_a_screen_tells_the_clicker_alone_comes_back_in_the_answer() -> None:
    """The caller of the actions endpoint is the person who clicked: a screen's
    private note is theirs to show, in the answer and nowhere in the journal."""
    from crucible.ports.chat.types import Card

    hold = asyncio.Event()

    async def draws(msg, chat):
        await chat.post_cards(msg.ref, [Card("connections", actions=(
            Action(id="c", label="Connect", value="whisper", context={"screen": "connections", "state": "{}"}),
            Action(id="n", label="Next", value="next", context={"screen": "connections", "state": "{}"}),
        ))], callback_url="")
        await hold.wait()
        return TurnOutcome.ACTED

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8607, script=draws, dispatcher=dispatcher)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8607, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            _, page = await _events(s, 8607, started["turnId"], after=1, wait=0.2)
            post_id = page["events"][0]["postId"]
            actions = f"/v1/turns/{started['turnId']}/actions"
            status, body = await _post(s, 8607, actions, {"postId": post_id, "actionId": "c"})
            assert status == 200 and body == {"outcome": "redrawn", "privateNote": "for u-7 only"}
            _, body = await _post(s, 8607, actions, {"postId": post_id, "actionId": "n"})
            assert body == {"outcome": "redrawn"}
            hold.set()
    finally:
        await hub.stop()



async def test_the_engine_can_start_a_turn_on_the_hub_and_the_client_follows_it() -> None:
    """A scheduled task or an event opens a turn the way a caller's message
    would: a journal the client reads, `activeTurn` while it runs, the report
    on the message as data — and the outcome back to whoever asked."""
    from crucible.ports.chat.types import EventReport
    from crucible.ports.engine_turn import DispatchError, TurnRequest
    from crucible.ports.turn import TurnScope

    seen: list[IncomingMessage] = []
    gate = asyncio.Event()

    async def slow(msg, chat):
        seen.append(msg)
        await gate.wait()
        await chat.post_reply(msg.ref, "the pipeline is green")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8521, script=slow)
    await hub.start()
    try:
        request = TurnRequest(
            agent="helper", channel_id="portal:c1", conversation_id="portal:c1", kind="dm",
            text="pipeline 1234 finished", message_id="ev-1", user_id="u-7", username="gitlab",
            event=EventReport(source="gitlab", text="status: success"),
            turn=TurnScope(turn_id="", attributes={"pipeline": "1234"}),
        )
        running = asyncio.ensure_future(hub.run_turn(request))
        async with aiohttp.ClientSession() as s:
            for _ in range(50):
                _, conv = await _get(s, 8521, "/v1/agents/helper/conversations/c1")
                if conv["activeTurn"] is not None:
                    break
                await asyncio.sleep(0.02)
            assert conv["activeTurn"] is not None  # the owner sees it running
            turn_id = conv["activeTurn"]["turnId"]
            # Somebody else in the realm does not.
            _, other = await _get(s, 8521, "/v1/agents/helper/conversations/c1",
                                  headers={**AUTH, "X-User-Id": "u-8"})
            assert other["activeTurn"] is None
            # A caller's message meanwhile is refused with the running turn's id.
            status, busy = await _post(s, 8521, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            assert status == 409 and busy["turnId"] == turn_id
            gate.set()
            assert await running is TurnOutcome.REPLIED
            events, status = await _drain(s, 8521, turn_id)
        assert events[0]["type"] == "turn.started" and events[0]["startedBy"] == "engine"
        assert [e["type"] for e in events] == ["turn.started", "message", "turn.finished"]
        message = seen[0]
        assert message.engine_started and message.synthetic and message.mentioned and message.is_dm
        assert message.event == EventReport(source="gitlab", text="status: success")
        assert message.ref.message_id == "portal:ev-1" and message.conversation_id == "portal:c1"
        assert message.turn is not None and message.turn.turn_id == turn_id
        assert message.turn.attributes == {"pipeline": "1234"}
        assert message.timestamp is not None
        # The same run again is not a second turn; an unknown agent is a dispatch error.
        with pytest.raises(DispatchError, match="already started"):
            await hub.run_turn(request)
        with pytest.raises(DispatchError, match="not on this gateway"):
            await hub.run_turn(replace(request, agent="nobody"))
    finally:
        await hub.stop()


async def test_a_callers_turn_is_marked_as_theirs_and_a_draining_hub_starts_none() -> None:
    from crucible.ports.engine_turn import DispatchError, TurnRequest

    hub, _ = _hub(8522)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8522, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            events, _ = await _drain(s, 8522, started["turnId"])
            assert events[0]["startedBy"] == "caller"
        hub.drain()
        with pytest.raises(DispatchError, match="shutting down"):
            await hub.run_turn(TurnRequest(
                agent="helper", channel_id="portal:c2", conversation_id="portal:c2", kind="dm",
                text="x", message_id="ev-2", user_id="u-7",
            ))
    finally:
        await hub.stop()


async def test_a_caller_lists_the_conversations_the_engine_moved_since() -> None:
    from crucible.ports.conversation import EngineTurn

    asked: list[tuple[str, str]] = []

    async def engine_turns(agent: str, since: str):
        asked.append((agent, since))
        return [
            EngineTurn("portal:c1", "u-7", "2026-10-15T10:05:00+00:00"),
            EngineTurn("portal:c2", "u-8", "2026-10-15T10:06:00+00:00"),  # somebody else's
            EngineTurn("other:c3", "u-7", "2026-10-15T10:07:00+00:00"),  # another realm
        ]

    hub, _ = _hub(8523, engine_turns=engine_turns)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _get(
                s, 8523, "/v1/agents/helper/conversations?startedSince=2026-10-15T10:00:00%2B00:00"
            )
            assert status == 200
            assert body == {"conversations": [{"conversationId": "c1", "startedAt": "2026-10-15T10:05:00+00:00"}]}
            assert asked == [("helper", "2026-10-15T10:00:00+00:00")]
            status, body = await _get(s, 8523, "/v1/agents/helper/conversations?startedSince=yesterday")
            assert status == 422
            status, body = await _get(s, 8523, "/v1/agents/scribe/conversations?startedSince=2026-10-15T10:00:00%2B00:00")
            assert status == 403  # not an agent on this hub
    finally:
        await hub.stop()

    plain, _ = _hub(8524)
    await plain.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, _ = await _get(s, 8524, "/v1/agents/helper/conversations?startedSince=2026-10-15T10:00:00%2B00:00")
            assert status == 404  # nothing wired to answer from
    finally:
        await plain.stop()


async def test_an_engine_turn_without_a_scope_of_its_own_is_marked_as_the_engines() -> None:
    from crucible.ports.engine_turn import TurnRequest
    from crucible.ports.turn import ENGINE_TURN_ATTRIBUTE

    seen: list[IncomingMessage] = []

    async def record(msg, chat):
        seen.append(msg)
        return TurnOutcome.ACTED

    hub, _ = _hub(8525, script=record)
    await hub.start()
    try:
        await hub.run_turn(TurnRequest(
            agent="helper", channel_id="portal:c1", conversation_id="portal:c1", kind="dm",
            text="the digest", message_id="sched-1", user_id="u-7",
        ))
        assert seen[0].turn is not None
        assert seen[0].turn.attributes == {ENGINE_TURN_ATTRIBUTE: "1"}  # nobody is behind this turn
    finally:
        await hub.stop()


async def test_a_truncated_turn_finishes_done_and_says_so() -> None:
    async def cut(msg, chat):
        await chat.post_reply(msg.ref, "The transitions are: To Do →")
        await chat.post_notice(msg.ref, "That answer was cut short by the model's length limit.", code="truncated")
        return TurnOutcome.TRUNCATED

    hub, _ = _hub(8489, script=cut)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8489, MESSAGES, {"clientMessageId": "m1", "text": "go"})
            events, final = await _drain(s, 8489, started["turnId"])
            assert [e["type"] for e in events] == ["turn.started", "message", "notice", "turn.finished"]
            assert events[2]["code"] == "truncated"
            assert events[3]["outcome"] == "truncated" and final == STATUS_DONE
    finally:
        await hub.stop()


def test_a_cards_fields_say_which_are_passages_on_the_wire() -> None:
    from crucible.gateways.http.client import wire_card
    from crucible.ports.chat.types import ApprovalCard, PreviewRow

    wire = wire_card(ApprovalCard(agent="a", headline="h", fields=(PreviewRow("Comment", "x", passage=True), PreviewRow("Issue", "y"))))
    assert [(f["label"], f["passage"]) for f in wire["fields"]] == [("Comment", True), ("Issue", False)]


async def test_a_card_from_a_turn_on_another_gateway_is_held_by_the_conversation_and_answered_there() -> None:
    """The conversation is live on Slack and on the panel; a turn runs on Slack
    and its tool asks for a yes. The card is forwarded to the panel's gateway,
    where no turn runs — so the conversation holds it: a client asking the
    conversation sees it and answers it by the conversation, not by a turn."""
    from crucible.ports.chat.types import ApprovalCard, ConversationRef

    dispatcher = FakeDispatcher()
    hub, _ = _hub(8512, dispatcher=dispatcher)
    chat = HttpChatClient(hub.turns, "helper")
    ref = ConversationRef(channel_id="portal:u-7", conversation_id="portal:c1", message_id="", thread_root_id="")
    card = ApprovalCard(agent="helper", headline="wants to run dangerous", title="wipe (files)", tool="dangerous",
                        expires_at="2026-10-04T12:00:00+00:00")
    allow = Action(id="yes", label="Allow", value="Allow", context={APPROVAL_KEY: "ap-1"})
    deny = Action(id="no", label="Deny", value="Deny", context={APPROVAL_KEY: "ap-1"})
    await hub.start()
    try:
        post_id = await chat.post_confirmation(ref, card, [allow, deny], callback_url="")
        assert post_id  # held, not dropped
        async with aiohttp.ClientSession() as s:
            dispatcher.answerers = {"u-7"}  # the card is the asker's
            _, seen = await _get(s, 8512, "/v1/agents/helper/conversations/c1")
            assert seen["activeTurn"] is None
            [pending] = seen["pendingCards"]
            # Somebody else of the realm sees the conversation but not the card: its arguments are the asker's.
            _, other_user = await _get(s, 8512, "/v1/agents/helper/conversations/c1", headers={**AUTH, "X-User-Id": "u-8"})
            assert other_user["pendingCards"] == []
            assert pending["postId"] == post_id and pending["tool"] == "dangerous" and pending["verdict"] == ""
            assert [a["id"] for a in pending["actions"]] == ["yes", "no"]
            # Another realm's conversation of the same name holds nothing.
            _, other = await _get(s, 8512, "/v1/agents/helper/conversations/c1",
                                  headers={"Authorization": "Bearer tok-narrow", "X-User-Id": "u-7"})
            assert other.get("pendingCards", []) == [] or other.get("error")
            # A click nobody knows is 404, as is the post named from another realm's conversation;
            # the card's own control reaches the approvals as this caller, on http.
            status, _ = await _post(s, 8512, "/v1/agents/helper/conversations/c1/actions",
                                    {"postId": post_id, "actionId": "maybe"})
            assert status == 404
            status, _ = await _post(s, 8512, "/v1/agents/helper/conversations/c1/actions",
                                    {"postId": post_id, "actionId": "yes"},
                                    headers={"Authorization": "Bearer tok-other", "X-User-Id": "u-7"})
            assert status == 404 and dispatcher.approvals == []
            status, answer = await _post(s, 8512, "/v1/agents/helper/conversations/c1/actions",
                                         {"postId": post_id, "actionId": "yes", "value": "ignored"})
            assert status == 200 and answer == {"outcome": "resolved"}
            assert dispatcher.approvals == [("ap-1", "Allow", "u-7", "http")]
            # The verdict comes back as a card with nothing to click: the conversation holds it no more.
            await chat.update_confirmation(post_id, replace(card, headline="— Allowed once", verdict="allowed_once"), [])
            _, after = await _get(s, 8512, "/v1/agents/helper/conversations/c1")
            assert after["pendingCards"] == []
            status, _ = await _post(s, 8512, "/v1/agents/helper/conversations/c1/actions",
                                    {"postId": post_id, "actionId": "yes"})
            assert status == 404
            # A card rewritten while still asking is replaced, not doubled; one answered elsewhere is let go.
            second = await chat.post_confirmation(ref, card, [allow], callback_url="")
            await chat.update_confirmation(second, replace(card, headline="still asking"), [allow])
            _, again = await _get(s, 8512, "/v1/agents/helper/conversations/c1")
            assert [c["headline"] for c in again["pendingCards"]] == ["still asking"]
            dispatcher.approval_outcome = ApprovalOutcome.NOT_MINE
            status, answer = await _post(s, 8512, "/v1/agents/helper/conversations/c1/actions",
                                         {"postId": second, "actionId": "yes"})
            assert (status, answer) == (200, {"outcome": "not_mine"})
            _, gone = await _get(s, 8512, "/v1/agents/helper/conversations/c1")
            assert gone["pendingCards"] == []
    finally:
        await hub.stop()


def test_a_card_and_its_controls_come_back_from_json_as_they_went() -> None:
    from crucible.ports.chat.interactions import (
        actions_from_json,
        actions_to_json,
        approval_card_from_json,
        approval_card_to_json,
    )
    from crucible.ports.chat.types import ApprovalCard, Choice, PreviewRow

    card = ApprovalCard(
        agent="helper", headline="wants to run wipe", icon="⚠️", title="wipe (files)", tool="mcp__files__wipe",
        fields=(PreviewRow("path", "/tmp", before="/"), PreviewRow("script", "a\nb", passage=True)),
        block_label="Arguments", block="{}", danger=True, expires_at="2026-10-04T12:00:00+00:00",
        verdict="", grant_s=0,
    )
    actions = (
        Action(id="yes", label="Allow", value="Allow", style="primary", context={APPROVAL_KEY: "t1", "n": 2}),
        Action(id="for", label="Allow for", kind="select", context={APPROVAL_KEY: "t1"},
               options=(Choice("10 min", "grant:600"), Choice("1 h", "grant:3600"))),
    )
    assert approval_card_from_json(approval_card_to_json(card)) == card
    assert actions_from_json(actions_to_json(actions)) == actions


async def test_a_card_held_by_one_replica_is_shown_and_answered_on_another(tmp_path: Path) -> None:
    """Two hubs — two replicas of the gateway — share one store: a card the
    turn's replica holds is listed by the other, answered there (the approvals
    forward the click to the replica that asked), and gone from both when the
    verdict comes on the first."""
    from crucible.ports.chat.types import ApprovalCard, ConversationRef
    from crucible.store.sqlite import SqliteSessionStore

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    dispatcher = FakeDispatcher()
    hub_a, _ = _hub(8513, dispatcher=dispatcher, turns=HttpTurns(cards=store))
    hub_b, _ = _hub(8514, dispatcher=dispatcher, turns=HttpTurns(cards=store))
    chat_a = HttpChatClient(hub_a.turns, "helper")
    ref = ConversationRef(channel_id="portal:u-7", conversation_id="portal:c1", message_id="", thread_root_id="")
    card = ApprovalCard(agent="helper", headline="wants to run dangerous", title="wipe (files)", tool="dangerous")
    allow = Action(id="yes", label="Allow", value="Allow", context={APPROVAL_KEY: "a1.ap-1"})
    await hub_a.start()
    await hub_b.start()
    try:
        post_id = await chat_a.post_confirmation(ref, card, [allow], callback_url="")
        async with aiohttp.ClientSession() as s:
            _, on_b = await _get(s, 8514, "/v1/agents/helper/conversations/c1")
            assert [c["postId"] for c in on_b["pendingCards"]] == [post_id]
            status, answer = await _post(s, 8514, "/v1/agents/helper/conversations/c1/actions",
                                         {"postId": post_id, "actionId": "yes"})
            assert (status, answer) == (200, {"outcome": "resolved"})
            assert dispatcher.approvals == [("a1.ap-1", "Allow", "u-7", "http")]
            await chat_a.update_confirmation(post_id, replace(card, verdict="allowed_once"), [])  # the verdict, on a
            _, after = await _get(s, 8514, "/v1/agents/helper/conversations/c1")
            assert after["pendingCards"] == []
    finally:
        await hub_a.stop()
        await hub_b.stop()
        await store.close()
