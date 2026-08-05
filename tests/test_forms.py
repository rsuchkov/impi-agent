"""open_form: Form (de)serialization, InteractionService, and the receiver dialog paths."""

from pathlib import Path

import aiohttp

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.service import InteractionService
from crucible.ports.chat.interactions import form_from_json, form_to_json
from crucible.ports.chat.types import KIND_DM, Action, ConversationRef, Form, FormField
from crucible.store.sessions import SqliteSessionStore
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of


def _form() -> Form:
    return Form(
        title="Bug report", intro="Fill this in", submit_label="Send",
        fields=(
            FormField(name="summary", label="Summary", type="text", placeholder="one line"),
            FormField(name="details", label="Details", type="textarea", optional=True),
            FormField(name="prio", label="Priority", type="select", options=("low", "high")),
            FormField(name="urgent", label="Urgent", type="bool", optional=True),
        ),
    )


def test_form_json_roundtrip() -> None:
    assert form_from_json(form_to_json(_form())) == _form()


class FakePoster(FakeChat):
    """A full ChatClient fake; records posted widgets under ``posted`` and opened
    dialogs under ``dialogs`` (inherited)."""

    def __init__(self) -> None:
        super().__init__()
        self.posted: list[tuple] = []

    async def post_actions(self, ref: ConversationRef, text, actions: list[Action], *, callback_url) -> str:
        self.posted.append((ref, text, actions, callback_url))
        return "pid"


async def test_form_service_registers_spec_and_posts_button(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
        assert await svc.open_form("assistant", rec.runtime_session_id, _form()) is True

        _, text, actions, _ = poster.posted[0]
        assert text == "Fill this in"  # the intro
        token = actions[0].context["form"]  # marks a form-open click
        stored = await store.get_form(token)
        assert stored is not None and stored.conversation_id == "dm1"
        assert form_from_json(stored.spec).title == "Bug report"
    finally:
        await store.close()


async def test_form_service_unknown_session_returns_false(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        svc = InteractionService(presence_of(FakePoster()), store, store, store, callback_url="http://x/i")
        assert await svc.open_form("assistant", "no-session", _form()) is False
    finally:
        await store.close()


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


async def _server(port: int, store, poster) -> tuple[InteractionsServer, SinkSpy]:
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, presence_of(object(), sink=spy), PendingUiRequests(), store  # type: ignore[arg-type]
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), presence_of(poster),
        host="127.0.0.1", port=port, dialog_submit_url="http://x/dialog",
    )
    await server.start()
    return server, spy


async def _post_form(store, poster) -> str:
    rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
    svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/interact")
    await svc.open_form("assistant", rec.runtime_session_id, _form())
    return poster.posted[0][2][0].context["form"]


async def test_open_click_opens_dialog_with_trigger(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    server, _spy = await _server(8481, store, poster)
    try:
        token = await _post_form(store, poster)
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8481/interact",
                json={"trigger_id": "trg-123", "context": {"form": token}},
            ) as resp:
                assert resp.status == 200

        assert len(poster.dialogs) == 1
        trg, form, submit_url, state = poster.dialogs[0]
        assert trg == "trg-123" and state == token and submit_url == "http://x/dialog"
        assert form.title == "Bug report"
        assert await store.get_form(token) is not None  # not consumed until submit
    finally:
        await server.stop()
        await store.close()


async def test_dialog_submission_feeds_back_and_consumes(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    server, spy = await _server(8482, store, poster)
    try:
        token = await _post_form(store, poster)
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8482/dialog",
                json={"type": "dialog_submission", "state": token, "cancelled": False,
                      "user_id": "u", "submission": {
                          "summary": "crash", "details": "", "prio": "high", "urgent": True}},
            ) as resp:
                assert resp.status == 200

        assert len(spy.submitted) == 1
        text = spy.submitted[0].text
        assert "crash" in text and "high" in text and "yes" in text  # bool -> yes
        assert spy.submitted[0].synthetic is True
        assert await store.get_form(token) is None  # one-shot: consumed
    finally:
        await server.stop()
        await store.close()


async def test_dialog_cancel_consumes_without_feedback(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    server, spy = await _server(8483, store, poster)
    try:
        token = await _post_form(store, poster)
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8483/dialog",
                json={"type": "dialog_submission", "state": token, "cancelled": True,
                      "submission": {}},
            ) as resp:
                assert resp.status == 200

        assert spy.submitted == []  # cancel -> nothing fed back
        assert await store.get_form(token) is None  # still consumed
    finally:
        await server.stop()
        await store.close()


# --- the extended field vocabulary --------------------------------------------

def test_form_json_roundtrip_carries_every_attribute() -> None:
    form = Form(title="T", fields=(
        FormField(name="tags", label="Tags", type="multiselect", options=("a", "b"),
                  help_text="as many as apply", placeholder="pick", optional=True),
        FormField(name="who", label="Who", type="user"),
        FormField(name="note", label="*heads up*", type="label"),
    ))
    assert form_from_json(form_to_json(form)) == form


async def test_submission_resolves_people_and_channels(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    # The sink's chat is what the dispatcher resolves ids through.
    chat = FakeChat()
    chat.channel_names["c-77"] = "incidents"
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        store, presence_of(chat, sink=spy), PendingUiRequests(), store
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), presence_of(poster),
        host="127.0.0.1", port=8484, dialog_submit_url="http://x/dialog",
    )
    await server.start()
    try:
        form = Form(title="Assign", fields=(
            FormField(name="note", label="*fill this in*", type="label"),
            FormField(name="who", label="Assignee", type="user"),
            FormField(name="team", label="Reviewers", type="users"),
            FormField(name="where", label="Channel", type="channel"),
            FormField(name="tags", label="Tags", type="multiselect", options=("a", "b")),
            FormField(name="urgent", label="Urgent", type="bool"),
        ))
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/i")
        await svc.open_form("assistant", rec.runtime_session_id, form)
        token = poster.posted[0][2][0].context["form"]

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8484/dialog",
                json={"state": token, "cancelled": False, "user_id": "u", "submission": {
                    "who": "u-1", "team": "u-1,u-2", "where": "c-77",
                    "tags": "a, b", "urgent": True}},
            ) as resp:
                assert resp.status == 200

        text = spy.submitted[0].text
        assert "- Assignee: @roman (u-1)" in text  # id resolved, id kept
        assert "- Reviewers: @roman (u-1), @roman (u-2)" in text
        assert "- Channel: ~incidents (c-77)" in text
        assert "- Tags: a, b" in text
        assert "- Urgent: yes" in text  # MM sends a real boolean
        assert "fill this in" not in text  # a label collects nothing
    finally:
        await server.stop()
        await store.close()


async def test_unresolvable_id_degrades_to_the_raw_value(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    spy = SinkSpy()
    # A chat client with no channel directory at all (e.g. an offline lookup).
    dispatcher = InteractionDispatcher(
        store, presence_of(FakeChat(), sink=spy), PendingUiRequests(), store
    )
    server = InteractionsServer(
        dispatcher, MattermostCallbackCodec(), presence_of(poster),
        host="127.0.0.1", port=8485, dialog_submit_url="http://x/dialog",
    )
    await server.start()
    try:
        form = Form(title="T", fields=(FormField(name="where", label="Channel", type="channel"),))
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/i")
        await svc.open_form("assistant", rec.runtime_session_id, form)
        token = poster.posted[0][2][0].context["form"]

        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8485/dialog",
                json={"state": token, "cancelled": False, "user_id": "u",
                      "submission": {"where": "c-unknown"}},
            ) as resp:
                assert resp.status == 200

        assert "- Channel: c-unknown" in spy.submitted[0].text  # raw, never blank
    finally:
        await server.stop()
        await store.close()
