"""Slash commands over the HTTP receiver: /command/{agent} → a private turn."""

from pathlib import Path

import aiohttp

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import (
    InteractionDispatcher,
    InteractionsServer,
    MappingPresence,
)
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.ports.chat.types import KIND_CHANNEL, KIND_THREAD
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.presence import presence_of

# A real Mattermost payload (captured from a live server with a slash command
# invoked inside a thread); the engine parses exactly this shape.
THREAD_PAYLOAD = {
    "channel_id": "15wipcrtgfdq9ky7koz98oo4kh",
    "channel_name": "duo-54836",
    "command": "/summarize",
    "response_url": "http://localhost:8065/hooks/commands/qikjnio1x7ftjb34qefiws7owe",
    "root_id": "4d97wbwit3fnpm39jm7ex45pjr",
    "team_domain": "test",
    "team_id": "ww3d9nsmijdstmjz8yiz3czhch",
    "text": "коротко",
    "token": "good-token",
    "trigger_id": "NG14emdiNGdodHI1OGo5",
    "user_id": "hub1dray3jypdr34ctydssqr5c",
    "user_name": "r42x",
}


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


async def _server(port: int, store, *, tokens=("good-token",)) -> tuple[InteractionsServer, SinkSpy]:
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, presence_of(object(), sink=spy), PendingUiRequests(), store  # type: ignore[arg-type]
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}),
        host="127.0.0.1", port=port,
        command_tokens=lambda agent: tuple(tokens) if agent == "assistant" else (),
    )
    await server.start()
    return server, spy


async def _post(port: int, agent: str, payload: dict) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{port}/command/{agent}", data=payload
        ) as resp:
            body = await resp.json()
            return resp.status, body


async def test_command_in_a_thread_starts_a_private_turn(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8491, store)
    try:
        status, body = await _post(8491, "assistant", THREAD_PAYLOAD)
        assert status == 200
        assert body["response_type"] == "ephemeral"  # the receipt is private too
        msg = spy.submitted[0]
        assert msg.conversation_id == THREAD_PAYLOAD["root_id"]  # the thread, not the channel
        assert msg.channel_id == THREAD_PAYLOAD["channel_id"]
        assert msg.kind == KIND_THREAD and msg.ref.thread_root_id == THREAD_PAYLOAD["root_id"]
        assert msg.text == "/summarize коротко"  # trigger + args
        assert msg.user_id == THREAD_PAYLOAD["user_id"] and msg.username == "r42x"
        assert msg.synthetic is True and msg.mentioned is True
    finally:
        await server.stop()
        await store.close()


async def test_command_outside_a_thread_runs_as_the_channel(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8492, store)
    try:
        await _post(8492, "assistant", {**THREAD_PAYLOAD, "root_id": ""})
        msg = spy.submitted[0]
        assert msg.conversation_id == THREAD_PAYLOAD["channel_id"]
        assert msg.kind == KIND_CHANNEL and msg.ref.thread_root_id == ""
    finally:
        await server.stop()
        await store.close()


async def test_wrong_token_is_refused(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8493, store)
    try:
        status, _ = await _post(8493, "assistant", {**THREAD_PAYLOAD, "token": "stolen"})
        assert status == 403
        assert spy.submitted == []  # nothing reached the agent
    finally:
        await server.stop()
        await store.close()


async def test_agent_without_configured_commands_is_refused(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8494, store)
    try:
        # 'other' has no configured tokens at all -> commands are off for it.
        status, _ = await _post(8494, "other", THREAD_PAYLOAD)
        assert status == 403
        assert spy.submitted == []
    finally:
        await server.stop()
        await store.close()


async def test_unknown_agent_answers_unavailable(tmp_path: Path) -> None:
    # Configured, but no live presence (the agent isn't running).
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, MappingPresence({}), PendingUiRequests(), store  # nobody is present
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}),
        host="127.0.0.1", port=8495, command_tokens=lambda agent: ("good-token",),
    )
    await server.start()
    try:
        status, body = await _post(8495, "assistant", THREAD_PAYLOAD)
        assert status == 200
        assert "unavailable" in body["text"].lower()
        assert spy.submitted == []
    finally:
        await server.stop()
        await store.close()


async def test_repeat_invocations_are_distinct_turns(tmp_path: Path) -> None:
    # The flow dedups on message_id, so two invocations must not collide.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8496, store)
    try:
        await _post(8496, "assistant", THREAD_PAYLOAD)
        await _post(8496, "assistant", THREAD_PAYLOAD)
        ids = [m.ref.message_id for m in spy.submitted]
        assert len(ids) == 2 and ids[0] != ids[1]
        assert all(i.startswith("cmd-") for i in ids)
    finally:
        await server.stop()
        await store.close()
