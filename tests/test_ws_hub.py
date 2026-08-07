"""WsHub over real websockets on a local port (offline, aiohttp client)."""

import asyncio
import base64
from pathlib import Path

import aiohttp

from crucible.attachments import AttachmentStore
from crucible.gateways.ws.client import WsChatClient
from crucible.gateways.ws.hub import WsHub
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.types import ConversationRef, OutgoingFile


class FakeSink:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append((msg, chat))


class FakeDirectory:
    def agent_user_ids(self):
        return frozenset()

    def list_agents(self):
        return [
            AgentInfo("helper", "helps", "a helper", "ws-helper", "ws:helper"),
            AgentInfo("scribe", "writes", "a scribe", "ws-scribe", "ws:scribe"),
        ]


def _hub(port: int, services: dict) -> WsHub:
    return WsHub("127.0.0.1", port, services, directory=FakeDirectory())


async def _connect(session: aiohttp.ClientSession, port: int, token: str):
    return await session.ws_connect(
        f"http://127.0.0.1:{port}/ws", headers={"Authorization": f"Bearer {token}"}
    )


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def test_rejects_unknown_token() -> None:
    hub = _hub(8471, {"probe": ("good-token", None)})
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            try:
                await _connect(session, 8471, "bad-token")
                raise AssertionError("expected WSServerHandshakeError")
            except aiohttp.WSServerHandshakeError as exc:
                assert exc.status == 401
    finally:
        await hub.stop()


async def test_message_frame_reaches_the_agent_sink_namespaced() -> None:
    hub = _hub(8472, {"probe": ("tok", None)})
    sink = FakeSink()
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", sink, chat)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8472, "tok")
            await ws.send_json({
                "type": "message", "agent": "helper",
                "conversation_id": "user-1", "text": "hi",
            })
            await _wait_until(lambda: sink.submitted)
            msg, got_chat = sink.submitted[0]
            assert msg.conversation_id == "probe:user-1"
            assert got_chat is chat
            await ws.close()
    finally:
        await hub.stop()


async def test_allowlist_blocks_foreign_agent_with_error_frame() -> None:
    hub = _hub(8473, {"probe": ("tok", ("scribe",))})
    sink = FakeSink()
    hub.register_agent("helper", sink, WsChatClient(hub, "helper"))
    hub.register_agent("scribe", FakeSink(), WsChatClient(hub, "scribe"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8473, "tok")
            await ws.send_json({
                "type": "message", "agent": "helper",
                "conversation_id": "c", "text": "hi",
            })
            frame = await ws.receive_json(timeout=2)
            assert frame["type"] == "error" and "helper" in frame["detail"]
            assert sink.submitted == []
            await ws.close()
    finally:
        await hub.stop()


async def test_agents_discovery_lists_only_allowed_registered_agents() -> None:
    hub = _hub(8474, {"probe": ("tok", ("helper",))})
    hub.register_agent("helper", FakeSink(), WsChatClient(hub, "helper"))
    hub.register_agent("scribe", FakeSink(), WsChatClient(hub, "scribe"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8474, "tok")
            await ws.send_json({"type": "agents"})
            frame = await ws.receive_json(timeout=2)
            assert frame["type"] == "agents"
            assert frame["agents"] == [
                {"name": "helper", "role": "helps", "description": "a helper"}
            ]
            await ws.close()
    finally:
        await hub.stop()


async def test_reply_routes_to_the_owning_service_and_strips_namespace() -> None:
    # Two services, same agent, SAME client conversation id: replies must reach
    # each service's own socket with its own (un-namespaced) conversation id.
    hub = _hub(8475, {"one": ("tok-1", None), "two": ("tok-2", None)})
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", FakeSink(), chat)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws1 = await _connect(session, 8475, "tok-1")
            ws2 = await _connect(session, 8475, "tok-2")
            ref1 = ConversationRef("one:user-7", "one:user-7", "m")
            ref2 = ConversationRef("two:user-7", "two:user-7", "m")
            await chat.post_reply(ref1, "for one")
            await chat.post_notice(ref2, "for two")
            frame1 = await ws1.receive_json(timeout=2)
            frame2 = await ws2.receive_json(timeout=2)
            assert frame1 == {
                "type": "reply", "agent": "helper",
                "conversation_id": "user-7", "text": "for one",
            }
            assert frame2["type"] == "notice" and frame2["text"] == "for two"
            await ws1.close()
            await ws2.close()
    finally:
        await hub.stop()


async def test_offline_replies_buffer_and_flush_on_reconnect() -> None:
    hub = _hub(8476, {"probe": ("tok", None)})
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", FakeSink(), chat)
    await hub.start()
    try:
        ref = ConversationRef("probe:u1", "probe:u1", "m")
        await chat.post_reply(ref, "answer while offline")  # no connection yet
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8476, "tok")
            frame = await ws.receive_json(timeout=2)
            assert frame["text"] == "answer while offline"
            assert frame["conversation_id"] == "u1"
            await ws.close()
    finally:
        await hub.stop()


async def test_new_connection_supersedes_the_old_one() -> None:
    hub = _hub(8477, {"probe": ("tok", None)})
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", FakeSink(), chat)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws_old = await _connect(session, 8477, "tok")
            ws_new = await _connect(session, 8477, "tok")
            # The old socket is closed by the hub; the new one gets traffic.
            closed = await ws_old.receive(timeout=2)
            assert closed.type in (
                aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED
            )
            await chat.post_reply(ConversationRef("probe:u", "probe:u", "m"), "hi")
            frame = await ws_new.receive_json(timeout=2)
            assert frame["text"] == "hi"
            await ws_new.close()
    finally:
        await hub.stop()


async def test_garbage_and_unknown_frames_answer_error_but_keep_the_socket() -> None:
    hub = _hub(8478, {"probe": ("tok", None)})
    sink = FakeSink()
    hub.register_agent("helper", sink, WsChatClient(hub, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8478, "tok")
            await ws.send_str("{not json")
            assert (await ws.receive_json(timeout=2))["type"] == "error"
            await ws.send_json({"type": "telepathy"})
            assert (await ws.receive_json(timeout=2))["type"] == "error"
            await ws.send_json({"type": "message", "agent": "helper", "text": "no conv"})
            assert (await ws.receive_json(timeout=2))["type"] == "error"
            # The socket survived all three — a valid frame still works.
            await ws.send_json({
                "type": "message", "agent": "helper",
                "conversation_id": "c", "text": "ok",
            })
            await _wait_until(lambda: sink.submitted)
            await ws.close()
    finally:
        await hub.stop()


async def test_inline_files_are_stored_and_travel_with_the_message(tmp_path) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    hub = WsHub(
        "127.0.0.1", 8478, {"probe": ("tok", None)},
        directory=FakeDirectory(), attachments=store,
    )
    sink = FakeSink()
    hub.register_agent("helper", sink, WsChatClient(hub, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8478, "tok")
            await ws.send_json({
                "type": "message", "agent": "helper", "conversation_id": "user-1",
                "text": "",
                "files": [{"name": "photo.jpg", "mime": "image/jpeg",
                           "data": base64.b64encode(b"JPEGDATA").decode()}],
            })
            await _wait_until(lambda: sink.submitted)
            msg, _ = sink.submitted[0]
            (attachment,) = msg.attachments
            assert attachment.mime == "image/jpeg"
            assert Path(attachment.path).read_bytes() == b"JPEGDATA"
            await ws.close()
    finally:
        await hub.stop()


async def test_undecodable_file_answers_error_and_never_reaches_the_agent(tmp_path) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    hub = WsHub(
        "127.0.0.1", 8479, {"probe": ("tok", None)},
        directory=FakeDirectory(), attachments=store,
    )
    sink = FakeSink()
    hub.register_agent("helper", sink, WsChatClient(hub, "helper"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8479, "tok")
            await ws.send_json({
                "type": "message", "agent": "helper", "conversation_id": "user-1",
                "text": "look", "files": [{"name": "a.png", "data": "not base64!"}],
            })
            frame = await ws.receive_json(timeout=2)
            assert frame["type"] == "error" and "base64" in frame["detail"]
            assert sink.submitted == []
            await ws.close()
    finally:
        await hub.stop()


async def test_a_sent_file_reaches_the_service_as_a_file_frame() -> None:
    hub = _hub(8480, {"probe": ("tok", None)})
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", FakeSink(), chat)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8480, "tok")
            ref = ConversationRef(
                channel_id="probe:user-1", conversation_id="probe:user-1",
                message_id="m1", thread_root_id="",
            )

            await chat.post_files(
                ref,
                [OutgoingFile(name="chart.png", data=b"PNG", mime="image/png")],
                text="the trend",
            )

            frame = await ws.receive_json(timeout=2)
            assert frame == {
                "type": "file", "agent": "helper", "conversation_id": "user-1",
                "name": "chart.png", "mime": "image/png",
                "data": base64.b64encode(b"PNG").decode(), "text": "the trend",
            }
            await ws.close()
    finally:
        await hub.stop()


async def test_a_file_sent_while_offline_is_buffered_like_a_reply() -> None:
    hub = _hub(8481, {"probe": ("tok", None)})
    chat = WsChatClient(hub, "helper")
    hub.register_agent("helper", FakeSink(), chat)
    await hub.start()
    try:
        ref = ConversationRef(
            channel_id="probe:user-1", conversation_id="probe:user-1",
            message_id="m1", thread_root_id="",
        )
        await chat.post_files(ref, [OutgoingFile(name="a.txt", data=b"x")])

        async with aiohttp.ClientSession() as session:
            ws = await _connect(session, 8481, "tok")
            frame = await ws.receive_json(timeout=2)
            assert frame["type"] == "file" and frame["name"] == "a.txt"
            await ws.close()
    finally:
        await hub.stop()
