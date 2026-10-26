"""What the replicas hand each other over the hub: a turn, a click."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest

from crucible.approvals import ApprovalOutcome
from crucible.attachments import AttachmentStore
from crucible.gateways.http import HttpHub, HubForwarder, InternalRoutes, TokenCallers
from crucible.gateways.http.internal import (
    CLICKS_PATH,
    TURNS_PATH,
    ForwardFailed,
    message_from_wire,
    message_to_wire,
)
from crucible.interactions import AgentSink
from crucible.interactions.presence import MappingPresence
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.types import (
    KIND_THREAD,
    Attachment,
    ConversationRef,
    EventReport,
    IncomingMessage,
    QuotedMessage,
)
from crucible.ports.replica import ClickKind, ForwardedClick
from crucible.ports.turn import TurnScope, TurnSecrets
from tests.fakes.fake_chat import FakeChat
from tests.test_http_hub import FakeDirectory, FakeDispatcher, FakeSink


def _message(tmp_path: Path) -> IncomingMessage:
    file = tmp_path / "notes.txt"
    file.write_bytes(b"hello from pod a")
    return IncomingMessage(
        ref=ConversationRef(channel_id="C1", conversation_id="1700.1", message_id="1700.9", thread_root_id="1700.1"),
        text="please look", user_id="U1", username="alice",
        timestamp=datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc), kind=KIND_THREAD,
        mentioned=True, gateway="slack", hop_depth=1,
        attachments=(Attachment(name="notes.txt", path=str(file), mime="text/plain", size=16),),
        turn=TurnScope(turn_id="t-1", secrets=TurnSecrets({"jira": "s3cret"}), attributes={"engine_turn": "1"}, flags={"x"}),
        quoted=QuotedMessage(author="bob", text="the original"),
        engine_started=True, event=EventReport(source="ci", text="build failed"),
        raw={"platform": {"ts": "1700.9"}},
    )


def test_a_message_goes_over_the_wire_whole_but_for_its_secrets_and_its_files_paths(tmp_path: Path) -> None:
    msg = _message(tmp_path)
    wire = message_to_wire(msg)
    assert "s3cret" not in str(wire)  # a turn's secrets stay behind
    assert wire["attachments"][0]["name"] == "notes.txt" and "data" in wire["attachments"][0]
    back = message_from_wire(wire, (Attachment(name="notes.txt", path="/pod-b/notes.txt", mime="text/plain", size=16),))
    assert back.ref == msg.ref and back.text == msg.text and back.timestamp == msg.timestamp
    assert (back.user_id, back.username, back.kind, back.mentioned, back.gateway, back.hop_depth) == (
        "U1", "alice", KIND_THREAD, True, "slack", 1
    )
    assert back.quoted == msg.quoted and back.event == msg.event and back.engine_started
    assert back.turn is not None and back.turn.turn_id == "t-1" and back.turn.attributes == {"engine_turn": "1"}
    assert back.turn.flags == {"x"} and back.turn.secrets.get("jira") is None
    assert back.attachments[0].path == "/pod-b/notes.txt"  # as saved on the receiving side
    assert back.raw == {"platform": {"ts": "1700.9"}}


async def _hub(port: int, tmp_path: Path, token: str = "shared", **kw) -> tuple[HttpHub, FakeSink, FakeDispatcher, FakeChat]:
    sink, chat = FakeSink(kw.pop("script", _acted)), FakeChat()
    dispatcher = FakeDispatcher()
    presence = MappingPresence({"helper": AgentSink(sink=sink, gateways={"slack": chat, "mattermost": FakeChat()}, default="mattermost")})
    internal = InternalRoutes(
        token, presence, dispatcher=dispatcher,  # type: ignore[arg-type]  # structural test double
        attachments=AttachmentStore(tmp_path / "files", max_bytes=1024, retention_days=1),
        keying=kw.pop("keying", None),
    )
    hub = HttpHub("127.0.0.1", port, TokenCallers({}), directory=FakeDirectory(), max_wait_s=0.2, routes=internal.routes)
    await hub.start()
    return hub, sink, dispatcher, chat


async def _acted(msg, chat):
    _CHATS.append(chat)
    return TurnOutcome.ACTED


_CHATS: list = []


async def test_a_forwarded_turn_runs_on_the_owner_through_the_gateways_chat_and_answers_with_its_outcome(tmp_path: Path) -> None:
    hub, sink, _, slack = await _hub(8571, tmp_path)
    forwarder = HubForwarder("shared")
    try:
        outcome = await forwarder.forward_turn("http://127.0.0.1:8571", "helper", [_message(tmp_path)])
        assert outcome is TurnOutcome.ACTED
        assert len(sink.submitted) == 1
        got = sink.submitted[0]
        assert got.text == "please look" and got.gateway == "slack" and got.ref.message_id == "1700.9"
        assert _CHATS[-1] is slack  # the owner answers through the gateway the message came by
        # The file travelled as bytes and was written into this replica's store.
        assert Path(got.attachments[0].path).read_bytes() == b"hello from pod a"
        assert str(tmp_path / "files") in got.attachments[0].path
    finally:
        await forwarder.aclose()
        await hub.stop()


async def test_a_forwarded_click_is_answered_by_the_dispatcher_here(tmp_path: Path) -> None:
    hub, _, dispatcher, _ = await _hub(8572, tmp_path)
    forwarder = HubForwarder("shared")
    try:
        dispatcher.approval_outcome = ApprovalOutcome.NOT_ALLOWED
        answer = await forwarder.forward_click("http://127.0.0.1:8572", ClickKind.APPROVAL, "a1.tok", "once", "U9", "slack")
        assert answer is ForwardedClick.NOT_ALLOWED
        assert dispatcher.approvals == [("a1.tok", "once", "U9", "slack")]  # the clicker and their gateway
        dispatcher.resolves = True
        assert await forwarder.forward_click("http://127.0.0.1:8572", ClickKind.PENDING, "a1.tok2", "yes", "U9", "") is ForwardedClick.RESOLVED
        assert dispatcher.pending == [("a1.tok2", "yes")]
        dispatcher.resolves = False
        assert await forwarder.forward_click("http://127.0.0.1:8572", ClickKind.PENDING, "a1.tok3", "yes", "U9", "") is ForwardedClick.NOT_MINE
    finally:
        await forwarder.aclose()
        await hub.stop()


async def test_the_internal_routes_refuse_a_stranger_and_an_unknown_agent(tmp_path: Path) -> None:
    hub, sink, _, _ = await _hub(8573, tmp_path)
    wrong = HubForwarder("not-the-secret")
    right = HubForwarder("shared")
    try:
        with pytest.raises(ForwardFailed, match="401"):
            await wrong.forward_turn("http://127.0.0.1:8573", "helper", [_message(tmp_path)])
        assert sink.submitted == []
        with pytest.raises(ForwardFailed, match="404"):
            await right.forward_turn("http://127.0.0.1:8573", "nobody", [_message(tmp_path)])
        async with aiohttp.ClientSession() as s:
            async with s.post(f"http://127.0.0.1:8573{TURNS_PATH}", data="not json",
                              headers={"Authorization": "Bearer shared"}) as r:
                assert r.status == 422
            async with s.post(f"http://127.0.0.1:8573{CLICKS_PATH}", json={"kind": "WHATEVER", "token": "t"},
                              headers={"Authorization": "Bearer shared"}) as r:
                assert r.status == 422
        with pytest.raises(ForwardFailed):
            await right.forward_turn("http://127.0.0.1:8599", "helper", [_message(tmp_path)])  # nobody listens
    finally:
        await wrong.aclose()
        await right.aclose()
        await hub.stop()
        await asyncio.sleep(0)


def test_a_forwarded_clicks_outcomes_are_the_approval_outcomes() -> None:
    """The port may not import the approvals leaf, so the two enums are kept in
    step here: the dispatcher converts one into the other by name."""
    assert {m.name for m in ForwardedClick} == {m.name for m in ApprovalOutcome}


async def test_whether_a_person_may_answer_is_asked_of_the_replica_that_holds_the_question(tmp_path: Path) -> None:
    hub, _, dispatcher, _ = await _hub(8573, tmp_path)
    forwarder = HubForwarder("shared")
    try:
        dispatcher.answerers = {"U9"}
        assert await forwarder.ask_may_answer("http://127.0.0.1:8573", "a1.tok", "U9", "slack") is True
        assert await forwarder.ask_may_answer("http://127.0.0.1:8573", "a1.tok", "U8", "slack") is False
    finally:
        await forwarder.aclose()
        await hub.stop()


async def test_a_forwarded_file_is_saved_under_the_conversation_as_the_owner_keys_it(tmp_path: Path) -> None:
    class _TaskKey:
        def key(self, agent: str, msg) -> str:
            return "task-7"

    hub, sink, _, _ = await _hub(8574, tmp_path, keying=_TaskKey())
    forwarder = HubForwarder("shared")
    try:
        assert await forwarder.forward_turn("http://127.0.0.1:8574", "helper", [_message(tmp_path)]) is TurnOutcome.ACTED
        got = sink.submitted[-1]
        store = AttachmentStore(tmp_path / "files", max_bytes=1024, retention_days=1)
        assert Path(got.attachments[0].path).parent == store.conversation_dir("helper", "task-7")
    finally:
        await forwarder.aclose()
        await hub.stop()
