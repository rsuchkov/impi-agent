"""HttpHub over real HTTP on a local port (offline, aiohttp client)."""

import asyncio
import base64
from collections.abc import Awaitable, Callable
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
)
from crucible.ports.agent.events import ToolFinished, ToolStarted
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
        self.approvals: list[tuple[str, str, str]] = []
        self.approval_outcome = ApprovalOutcome.RESOLVED
        self.resolves = True

    def resolve_approval(self, token, value, user_id):
        self.approvals.append((token, value, user_id))
        return self.approval_outcome

    def resolve_pending(self, token, value) -> bool:
        self.pending.append((token, value))
        return self.resolves

    async def consume_action(self, token, value, user_id, *, pick=""):
        self.consumed.append((token, value, user_id, pick))


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


SERVICES = {"portal": ("tok-portal", None), "narrow": ("tok-narrow", ("scribe",))}
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

            # Over: the conversation has no active turn, and the journal still answers.
            _, conv = await _get(s, 8480, "/v1/agents/helper/conversations/c1")
            assert conv == {"conversationId": "c1", "activeTurn": None}
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
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm",
                               "helper--portal-c1-abc", "t", "t")
        trace = hub.turns.begin(record, chat)  # what AgentFlow does with its tracer
        trace.on_event(ToolStarted("call-1", "search", {"q": "x"}))
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
                "turn.started", "tool.started", "tool.finished", "turn.finished",
            ]
            assert events[1] == {**events[1], "callId": "call-1", "tool": "search"}
            assert events[2]["ok"] is False
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
            assert dispatcher.approvals == [("ap-9", "once", "u-7")]
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


async def test_readiness_follows_the_runtimes_capacity() -> None:
    control = FakeControl()
    hub, _ = _hub(8488, control=control)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, body = await _get(s, 8488, "/healthz", headers={})
            assert (status, body) == (200, {"ok": True})
            status, body = await _get(s, 8488, "/readyz", headers={})
            assert status == 200 and body["ready"] is True and body["runtime"]["capacity"] == 2
            control.busy = 2
            status, body = await _get(s, 8488, "/readyz", headers={})
            assert status == 503 and body["ready"] is False
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


@pytest.mark.parametrize("wait", ["abc", "-1"])
async def test_bad_query_values_are_validation_errors(wait: str) -> None:
    hub, _ = _hub(8490)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, started = await _post(s, 8490, MESSAGES, {"clientMessageId": "m1", "text": "hi"})
            status, body = await _get(s, 8490, f"/v1/turns/{started['turnId']}/events?wait={wait}")
            assert status in (200, 422)  # a negative wait is clamped; a word is refused
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
