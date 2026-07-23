"""InteractionService (concrete) + InteractionsServer round-trip on the receiver side."""

import asyncio
from pathlib import Path

import aiohttp

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import AgentSink, InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.service import InteractionService
from crucible.ports.chat.types import KIND_DM, KIND_THREAD, Action, ConversationRef
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat


class FakePoster(FakeChat):
    """A full ChatClient fake that records posted widgets under ``posted``."""

    def __init__(self) -> None:
        super().__init__()
        self.posted: list[tuple] = []

    async def post_actions(self, ref: ConversationRef, text, actions: list[Action], *, callback_url) -> str:
        self.posted.append((ref, text, actions, callback_url))
        return "widget-post-id"


async def test_widget_service_registers_and_posts(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    try:
        rec, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        svc = InteractionService(
            {"assistant": poster}, store, store, store, callback_url="http://host.containers.internal:8423/interact"
        )
        ok = await svc.ask("assistant", rec.runtime_session_id, "Обедать?", ["Да", "Нет"])
        assert ok is True

        ref, text, actions, cb = poster.posted[0]
        assert ref.channel_id == "ch1" and ref.thread_root_id == "root1"  # thread -> reply in thread
        assert text == "Обедать?"
        assert [a.label for a in actions] == ["Да", "Нет"]
        token = actions[0].context["token"]
        # the interaction is registered under that token
        taken = await store.take_interaction(token)
        assert taken is not None and taken.conversation_id == "root1"
    finally:
        await store.close()


async def test_widget_service_select_posts_one_dropdown(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    try:
        rec, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        svc = InteractionService({"assistant": poster}, store, store, store, callback_url="http://x/interact")
        ok = await svc.ask(
            "assistant", rec.runtime_session_id, "City?", ["A", "B", "C"], style="select"
        )
        assert ok is True

        _, text, actions, _ = poster.posted[0]
        assert text == "City?"
        assert len(actions) == 1  # a select is ONE action carrying all options
        assert actions[0].kind == "select"
        assert actions[0].options == ("A", "B", "C")
        assert "token" in actions[0].context
    finally:
        await store.close()


async def test_widget_service_returns_false_for_unknown_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        svc = InteractionService({"assistant": FakePoster()}, store, store, store, callback_url="http://x/interact")
        assert await svc.ask("assistant", "no-such-session", "q", ["a", "b"]) is False
    finally:
        await store.close()


# --- InteractionsServer -----------------------------------------------------


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


async def _integrations(port: int, store) -> tuple[InteractionsServer, SinkSpy, PendingUiRequests]:
    spy = SinkSpy()
    pending = PendingUiRequests()
    dispatcher = InteractionDispatcher(
        store, {"assistant": AgentSink(sink=spy, chat=object())}, pending, store  # type: ignore[arg-type]
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), {}, host="127.0.0.1", port=port
    )
    await server.start()
    return server, spy, pending


async def test_click_feeds_choice_back_into_conversation(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService({"assistant": poster}, store, store, store, callback_url="http://x/interact")
    server, spy, _pending = await _integrations(8471, store)
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        await svc.ask("assistant", rec.runtime_session_id, "Choose", ["Yes", "No"])
        token = poster.posted[0][2][0].context["token"]

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8471/interact",
                json={"user_id": "uid-roman", "context": {"token": token, "value": "Yes"}},
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert "attachments" in body["update"]["props"]  # buttons retired

        # the choice was fed back as a message in the same conversation
        assert len(spy.submitted) == 1
        assert spy.submitted[0].text == "Yes"
        assert spy.submitted[0].conversation_id == "dm1"
    finally:
        await server.stop()
        await store.close()


async def test_select_pick_feeds_selected_option_back(tmp_path: Path) -> None:
    # A dropdown pick arrives as context.selected_option (MM adds it), not value.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService({"assistant": poster}, store, store, store, callback_url="http://x/interact")
    server, spy, _pending = await _integrations(8473, store)
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        await svc.ask("assistant", rec.runtime_session_id, "City?", ["A", "B"], style="select")
        token = poster.posted[0][2][0].context["token"]

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8473/interact",
                json={"user_id": "u", "context": {"token": token, "selected_option": "B"}},
            ) as resp:
                assert resp.status == 200

        assert len(spy.submitted) == 1
        assert spy.submitted[0].text == "B"
    finally:
        await server.stop()
        await store.close()


async def test_blocking_click_resolves_pending_and_skips_sink(tmp_path: Path) -> None:
    # A blocking UI request's click resolves the waiting Future (the paused turn
    # continues) and must NOT feed a synthetic message into the sink.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy, pending = await _integrations(8474, store)
    try:
        fut = pending.register("tok1", method="confirm", agent="assistant", conversation_id="dm1")

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8474/interact",
                json={"user_id": "u", "context": {"token": "tok1", "value": "Allow"}},
            ) as resp:
                assert resp.status == 200

        outcome = await asyncio.wait_for(fut, 1.0)
        assert outcome.confirmed is True
        assert spy.submitted == []  # blocking path does not synthesize a message
    finally:
        await server.stop()
        await store.close()


async def test_click_with_used_token_is_benign(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    server, spy, _pending = await _integrations(8472, store)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8472/interact",
                json={"user_id": "u", "context": {"token": "unknown", "value": "Yes"}},
            ) as resp:
                assert resp.status == 200  # not an error — buttons just retired
        assert spy.submitted == []  # nothing fed back
    finally:
        await server.stop()
        await store.close()
