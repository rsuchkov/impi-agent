"""InteractionService (concrete) + InteractionsServer round-trip on the receiver side."""

import asyncio
import logging
from pathlib import Path

import aiohttp
import pytest

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import (
    InteractionDispatcher,
    InteractionsServer,
    MappingPresence,
)
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.service import InteractionService
from crucible.ports.chat.types import KIND_DM, KIND_THREAD, Action, ConversationRef
from crucible.store.sqlite import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of


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
            presence_of(poster), store, store, store, callback_url="http://host.containers.internal:8423/interact"
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
        svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
        ok = await svc.ask(
            "assistant", rec.runtime_session_id, "City?", ["A", "B", "C"], style="select"
        )
        assert ok is True

        _, text, actions, _ = poster.posted[0]
        assert text == "City?"
        assert len(actions) == 1  # a select is ONE action carrying all options
        assert actions[0].kind == "select"
        assert [c.value for c in actions[0].options] == ["A", "B", "C"]
        assert "token" in actions[0].context
    finally:
        await store.close()


async def test_widget_service_returns_false_for_unknown_session(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        svc = InteractionService(presence_of(FakePoster()), store, store, store, callback_url="http://x/interact")
        assert await svc.ask("assistant", "no-such-session", "q", ["a", "b"]) is False
    finally:
        await store.close()


# --- InteractionsServer -----------------------------------------------------


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []
        self.chats: list = []  # the chat each message was submitted with

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)
        self.chats.append(chat)


async def _integrations(
    port: int, store, *, gateways: dict | None = None
) -> tuple[InteractionsServer, SinkSpy, PendingUiRequests]:
    spy = SinkSpy()
    pending = PendingUiRequests()
    dispatcher = InteractionDispatcher(
        store, presence_of(object(), sink=spy, gateways=gateways), pending, store  # type: ignore[arg-type]
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}), host="127.0.0.1", port=port
    )
    await server.start()
    return server, spy, pending


async def test_click_feeds_choice_back_into_conversation(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    server, spy, _pending = await _integrations(8471, store)
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM, gateway="mattermost")  # the speaker wrote there
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
        assert spy.submitted[0].gateway == "mattermost"  # the click comes back through the widget's gateway
    finally:
        await server.stop()
        await store.close()


async def test_select_pick_feeds_selected_option_back(tmp_path: Path) -> None:
    # A dropdown pick arrives as context.selected_option (MM adds it), not value.
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
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


# --- workspace pickers ---------------------------------------------------------

async def test_user_picker_posts_a_server_fed_menu(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        assert await svc.ask("assistant", rec.runtime_session_id, "Who?", [], style="users") is True

        action = poster.posted[0][2][0]
        assert action.kind == "user_select" and action.options == ()  # the workspace supplies them
        assert action.context["pick"] == "user"  # travels so the click can be resolved
    finally:
        await store.close()


async def test_picked_person_reaches_the_agent_as_a_name(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, presence_of(FakeChat(), sink=spy), PendingUiRequests(), store
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), MappingPresence({}), host="127.0.0.1", port=8477
    )
    await server.start()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        await svc.ask("assistant", rec.runtime_session_id, "Who?", [], style="users")
        ctx = poster.posted[0][2][0].context

        async with aiohttp.ClientSession() as s:
            # MM echoes the whole context back and adds the picked id.
            async with s.post(
                "http://127.0.0.1:8477/interact",
                json={"user_id": "uid", "context": {**ctx, "selected_option": "u-42"}},
            ) as resp:
                assert resp.status == 200

        # An id alone would be meaningless to the model.
        assert spy.submitted[0].text == "@roman (u-42)"
    finally:
        await server.stop()
        await store.close()


async def test_the_log_does_not_replay_the_click(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The token in the action context is what authorizes the click; the log
    names the card by its first characters and never the whole credential."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    server, _spy, _pending = await _integrations(8479, store)
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        await svc.ask("assistant", rec.runtime_session_id, "Choose", ["Yes", "No"])
        token = poster.posted[0][2][0].context["token"]
        with caplog.at_level(logging.INFO, logger="crucible.interactions.server"):
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "http://127.0.0.1:8479/interact",
                    json={"user_id": "u1", "context": {"token": token, "value": "Yes"}},
                ) as resp:
                    assert resp.status == 200
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert token[:8] in joined
        assert token not in joined
    finally:
        await server.stop()


async def test_what_a_screen_tells_the_clicker_alone_comes_back_as_an_ephemeral_reply() -> None:
    """Mattermost shows a click's ephemeral_text to the person who clicked and
    nobody else: where a screen's private note goes."""
    from crucible.interactions.screens import Redrawn

    class _Screens:
        async def redraw_screen(self, state_raw, value, *, post_id, user_id):
            return Redrawn(f"for {user_id} only")

    server = InteractionsServer(
        _Screens(), MattermostCallbackCodec(), MappingPresence({}), host="127.0.0.1", port=8605,  # type: ignore[arg-type]
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8605/interact",
                json={"user_id": "u1", "post_id": "p1", "context": {"screen": "connections", "state": "{}", "value": "x"}},
            ) as resp:
                body = await resp.json()
        assert body == {"ephemeral_text": "for u1 only"}
    finally:
        await server.stop()


async def test_a_click_comes_back_through_the_gateway_the_widget_was_posted_on(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    slack_chat = object()
    server, spy, _pending = await _integrations(8472, store, gateways={"slack": slack_chat})
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM, gateway="slack")  # the widget went out on Slack
        await svc.ask("assistant", rec.runtime_session_id, "Choose", ["Yes", "No"])
        token = poster.posted[0][2][0].context["token"]
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8472/interact",
                json={"user_id": "uid-roman", "context": {"token": token, "value": "Yes"}},
            ) as resp:
                assert resp.status == 200
        assert spy.submitted[0].gateway == "slack"
        assert spy.chats[0] is slack_chat  # the reply will go back through Slack
    finally:
        await server.stop()
        await store.close()


async def test_a_widget_and_a_form_go_through_the_gateway_the_conversation_last_spoke_on(tmp_path: Path) -> None:
    """An agent on two gateways posts a widget where the conversation is — not
    on its default — and a form's submission comes back the same way."""
    from crucible.ports.chat.types import Form, FormField

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    mattermost, slack = FakePoster(), FakePoster()
    svc = InteractionService(
        presence_of(mattermost, gateways={"slack": slack}), store, store, store, callback_url="http://x/interact"
    )
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, presence_of(mattermost, sink=spy, gateways={"slack": slack}), PendingUiRequests(), store  # type: ignore[arg-type]
    )
    try:
        rec, _ = await store.get_or_create("assistant", "C9", "1700.1", KIND_THREAD, gateway="slack")
        assert await svc.ask("assistant", rec.runtime_session_id, "Choose", ["Yes", "No"])
        assert len(slack.posted) == 1 and mattermost.posted == []
        form = Form(title="Ticket", fields=(FormField(name="summary", label="Summary"),))
        assert await svc.open_form("assistant", rec.runtime_session_id, form)
        assert len(slack.posted) == 2 and mattermost.posted == []
        token = slack.posted[1][2][0].context["form"]
        record = await store.get_form(token)
        assert record is not None and record.gateway == "slack"
        assert await dispatcher.submit_form(token, {"summary": "done"}, False, "uid-roman")
        assert spy.chats[-1] is slack and spy.submitted[-1].gateway == "slack"
        assert slack.retracted  # the button retired on Slack, where it was
    finally:
        await store.close()


def test_a_turn_posts_in_the_thread_it_answers_in_when_the_address_has_none() -> None:
    from crucible.interactions.service import turn_ref
    from crucible.ports.turn import TurnPlace
    from crucible.store.base import GatewayAddress, SessionRecord

    dm = SessionRecord("assistant", "D1", "D1", KIND_DM, "rs", "2026-01-01", "2026-01-01", last_gateway="slack")
    # A direct message answered under the person's message: the widget goes there.
    ref = turn_ref(dm, "slack", TurnPlace("slack", "1700.1"))
    assert (ref.channel_id, ref.conversation_id, ref.message_id, ref.thread_root_id) == ("D1", "D1", "1700.1", "1700.1")
    # Answered at the top, between turns, or on another gateway: the address as it stands.
    assert turn_ref(dm, "slack", TurnPlace("slack", "")) == turn_ref(dm, "slack", None)
    assert turn_ref(dm, "slack", None).thread_root_id == ""
    assert turn_ref(dm, "mattermost", TurnPlace("slack", "1700.1")).thread_root_id == ""
    # A conversation that is a thread keeps its own thread whatever the place says.
    thread = SessionRecord("assistant", "C1", "9.9", KIND_THREAD, "rs", "2026-01-01", "2026-01-01", last_gateway="slack")
    assert turn_ref(thread, "slack", TurnPlace("slack", "1700.1")).thread_root_id == "9.9"
    # The address on the gateway, not the record's own fields, when one is known there.
    moved = SessionRecord(
        "assistant", "D1", "D1", KIND_DM, "rs", "2026-01-01", "2026-01-01", last_gateway="slack",
        gateways='{"slack": {"channel_id": "D1", "conversation_id": "D1", "kind": "dm"}}',
    )
    assert turn_ref(moved, "slack", TurnPlace("slack", "1700.1")).thread_root_id == "1700.1"
    assert GatewayAddress("D1", "D1", KIND_DM).kind == KIND_DM


async def test_a_widget_asked_in_a_direct_message_answered_in_a_thread_goes_there_and_the_click_comes_back_there(
    tmp_path: Path,
) -> None:
    from crucible.ports.turn import TurnPlace
    from crucible.turns import TurnRegistry

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    turns = TurnRegistry()
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact", places=turns)
    server, spy, _pending = await _integrations(8473, store)
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM, gateway="mattermost")
        async with turns.bind(rec.runtime_session_id, None, place=TurnPlace("mattermost", "p1")):
            await svc.ask("assistant", rec.runtime_session_id, "Choose", ["Yes", "No"])
        ref = poster.posted[0][0]
        assert (ref.conversation_id, ref.thread_root_id) == ("dm1", "p1")  # under the person's message
        token = poster.posted[0][2][0].context["token"]
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8473/interact",
                json={"user_id": "uid-1", "context": {"token": token, "value": "Yes"}},
            ) as resp:
                assert resp.status == 200
        msg = spy.submitted[0]
        assert msg.conversation_id == "dm1" and msg.kind == KIND_DM  # the DM's conversation still
        assert msg.ref.thread_root_id == "p1"  # answered in the same thread
        # Between turns the widget goes to the DM's top, and so does the click.
        await svc.ask("assistant", rec.runtime_session_id, "Again?", ["Yes"])
        assert poster.posted[1][0].thread_root_id == ""
        token = poster.posted[1][2][0].context["token"]
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8473/interact", json={"user_id": "uid-1", "context": {"token": token, "value": "Yes"}},
            ) as resp:
                assert resp.status == 200
        assert spy.submitted[1].ref.thread_root_id == ""
    finally:
        await server.stop()
        await store.close()
