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
from crucible.interactions.screens import ScreenRegistry, View
from crucible.ports.chat.types import KIND_CHANNEL, KIND_THREAD
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat
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


async def _server(
    port: int, store, *, tokens=("good-token",), token_owners=("assistant",),
    agents=None, default_agent="", present="assistant", screens=None, chat=None,
) -> tuple[InteractionsServer, SinkSpy]:
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store,
        presence_of(chat or object(), sink=spy, agent=present),  # type: ignore[arg-type]
        PendingUiRequests(), store, screens=screens,
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}),
        host="127.0.0.1", port=port,
        command_tokens=lambda agent: tuple(tokens) if agent in token_owners else (),
        agents=(lambda: tuple(agents)) if agents is not None else None,
        default_agent=default_agent,
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


async def test_default_reaches_the_only_agent_there_is(tmp_path: Path) -> None:
    # A single-agent deployment should not have to spell its agent's name in the
    # URL it registers with the platform.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8496, store, agents=("assistant",))
    try:
        status, _ = await _post(8496, "default", THREAD_PAYLOAD)

        assert status == 200
        assert spy.submitted[0].text == "/summarize коротко"
    finally:
        await server.stop()
        await store.close()


async def test_default_follows_agent_name_when_several_agents_run(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(
        8497, store, agents=("support", "assistant"), default_agent="assistant",
    )
    try:
        status, _ = await _post(8497, "default", THREAD_PAYLOAD)

        assert status == 200  # resolved to assistant, whose token this is
        assert spy.submitted
    finally:
        await server.stop()
        await store.close()


async def test_default_with_no_agent_to_resolve_to_is_refused(tmp_path: Path) -> None:
    # Several agents and none of them named as the default: guessing an owner
    # would run someone's command as the wrong bot.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(
        8498, store, agents=("support", "assistant"), default_agent="ghost",
    )
    try:
        status, _ = await _post(8498, "default", THREAD_PAYLOAD)

        assert status == 404
        assert spy.submitted == []
    finally:
        await server.stop()
        await store.close()


async def test_an_agent_really_called_default_keeps_its_own_endpoint(tmp_path: Path) -> None:
    # The reserved word must not change what an existing registration means.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(
        8499, store, token_owners=("default",), present="default",
        agents=("default", "assistant"), default_agent="assistant",
    )
    try:
        status, _ = await _post(8499, "default", THREAD_PAYLOAD)

        assert status == 200  # the token checked was the real agent's, not assistant's
        assert spy.submitted
    finally:
        await server.stop()
        await store.close()


async def test_default_still_has_to_prove_the_token(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy = await _server(8500, store, agents=("assistant",))
    try:
        status, _ = await _post(8500, "default", {**THREAD_PAYLOAD, "token": "stolen"})

        assert status == 403
        assert spy.submitted == []
    finally:
        await server.stop()
        await store.close()


# --- a screen that will not appear here -------------------------------------------


class PickyScreen:
    """A screen with an opinion about where it may be opened."""

    command = "picky"

    def __init__(self) -> None:
        self.rendered = 0

    async def admits(self, *, user_id: str, ref) -> str:
        return "" if user_id == "welcome" else "not for you, not here"

    async def render(self, state, *, user_id: str) -> View:
        self.rendered += 1
        return View.of("the contents")


async def test_a_refused_screen_answers_privately_and_posts_nothing(
    tmp_path: Path,
) -> None:
    """The refusal is the command's own answer — ephemeral, in the platform's
    shape for a command rather than for a click — and nothing reaches the
    conversation, which is the point of refusing."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat, screen = FakeChat(), PickyScreen()
    screens = ScreenRegistry()
    screens.register(screen)
    server, spy = await _server(8497, store, screens=screens, chat=chat)
    try:
        status, body = await _post(8497, "assistant", {**THREAD_PAYLOAD, "command": "/picky"})

        assert status == 200
        assert body == {"response_type": "ephemeral", "text": "not for you, not here"}
        assert chat.posted_cards == []  # nothing was drawn into the conversation
        assert screen.rendered == 0  # and nothing was rendered to draw
        assert spy.submitted == []  # nor did it fall through to the agent
    finally:
        await server.stop()
        await store.close()


async def test_an_admitted_screen_is_posted_and_answers_with_nothing(
    tmp_path: Path,
) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat, screen = FakeChat(), PickyScreen()
    screens = ScreenRegistry()
    screens.register(screen)
    server, _ = await _server(8498, store, screens=screens, chat=chat)
    try:
        status, body = await _post(
            8498, "assistant", {**THREAD_PAYLOAD, "command": "/picky", "user_id": "welcome"}
        )

        assert status == 200 and body == {}  # the card IS the answer
        assert screen.rendered == 1
        assert len(chat.posted_cards) == 1
    finally:
        await server.stop()
        await store.close()
