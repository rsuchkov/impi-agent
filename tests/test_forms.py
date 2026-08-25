"""open_form: Form (de)serialization, InteractionService, and the receiver dialog paths."""

from dataclasses import replace
from pathlib import Path

import aiohttp

from crucible.gateways.mattermost import MattermostCallbackCodec
from crucible.interactions import InteractionDispatcher, InteractionsServer
from crucible.interactions.pending_ui import PendingUiRequests
from crucible.interactions.ports import FormHandlers
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


async def _server(
    port: int, store, poster, handlers=None
) -> tuple[InteractionsServer, SinkSpy]:
    spy = SinkSpy()
    dispatcher = InteractionDispatcher(
        # Same client on both sides: the dispatcher retires the button through the
        # agent's chat client, and the test reads it back off the poster.
        store, presence_of(poster, sink=spy), PendingUiRequests(), store,
        handlers=handlers,
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
        # The button is struck off its own message: the form is answered, and a
        # second click would find nothing to open.
        assert poster.retracted == [("pid", "✅ Submitted.")]  # the button's own post
    finally:
        await server.stop()
        await store.close()


async def test_dialog_cancel_keeps_the_form_open_for_a_second_try(tmp_path: Path) -> None:
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
        # …but the form survives: closing the modal by accident must not cost the
        # user the button.
        assert await store.get_form(token) is not None
        assert poster.retracted == []
    finally:
        await server.stop()
        await store.close()


# --- the extended field vocabulary --------------------------------------------

async def test_open_button_wears_the_form_s_own_label(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    try:
        rec, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
        svc = InteractionService(presence_of(poster), store, store, store, callback_url="http://x/i")

        await svc.open_form("assistant", rec.runtime_session_id, _form())
        assert poster.posted[0][2][0].label == "📝 Fill in…"  # engine default

        titled = Form(title="Bug", fields=_form().fields, open_label="Report a bug")
        await svc.open_form("assistant", rec.runtime_session_id, titled)
        assert poster.posted[1][2][0].label == "Report a bug"
    finally:
        await store.close()


def test_form_json_roundtrip_carries_every_attribute() -> None:
    form = Form(title="T", open_label="Report a bug", fields=(
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


# --- a form the application answers itself ----------------------------------------


class SpyHandler:
    """A ``FormHandler``: it is called only for forms that named it."""

    def __init__(self, *, explodes: bool = False) -> None:
        self.seen: list[tuple[str, dict, str]] = []
        self._explodes = explodes

    async def handle(self, record, values, user_id: str) -> None:
        self.seen.append((record.token, dict(values), user_id))
        if self._explodes:
            raise RuntimeError("the handler fell over")


def _handlers(name: str, handler) -> FormHandlers:
    registry = FormHandlers()
    registry.register(name, handler)
    return registry


async def _submit(port: int, token: str, submission: dict) -> None:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"http://127.0.0.1:{port}/dialog",
            json={"type": "dialog_submission", "state": token, "cancelled": False,
                  "user_id": "u", "submission": submission},
        ) as resp:
            assert resp.status == 200


async def _post_handled_form(store, poster, handler_name: str) -> str:
    """A form written with a handler's name, as an application writes one."""
    token = await _post_form(store, poster)
    record = await store.get_form(token)
    assert record is not None
    await store.delete_form(token)
    await store.create_form(replace(record, handler=handler_name))
    return token


async def test_a_form_names_the_handler_that_answers_it(tmp_path: Path) -> None:
    """Values a form collects for the application must not become a message in
    the conversation — the point of routing, and the case where one of them is
    a credential."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    handler = SpyHandler()
    server, spy = await _server(8486, store, poster, handlers=_handlers("ops", handler))
    try:
        token = await _post_handled_form(store, poster, "ops")
        await _submit(8486, token, {"summary": "crash", "prio": "high"})

        assert handler.seen == [(token, {"summary": "crash", "prio": "high"}, "u")]
        assert spy.submitted == []  # nothing was fed into the conversation
        assert poster.retracted == []  # and nothing was rewritten
        assert await store.get_form(token) is None  # still one-shot
    finally:
        await server.stop()
        await store.close()


async def test_a_form_with_no_handler_takes_the_agent_path(tmp_path: Path) -> None:
    """The regression that matters for the engine: an unnamed form — every form
    an agent opens — behaves exactly as it did."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    handler = SpyHandler()
    server, spy = await _server(8487, store, poster, handlers=_handlers("ops", handler))
    try:
        token = await _post_form(store, poster)
        await _submit(8487, token, {"summary": "crash", "prio": "high"})

        assert handler.seen == []  # never consulted
        assert len(spy.submitted) == 1
        assert "crash" in spy.submitted[0].text
        assert poster.retracted == [("pid", "✅ Submitted.")]
    finally:
        await server.stop()
        await store.close()


async def test_a_handler_that_fails_does_not_hand_the_values_on(tmp_path: Path) -> None:
    """The reason routing beats asking. A handler that raised while claiming
    ownership by return value would drop the values into a conversation — and
    for the forms this exists for, that is the one unacceptable outcome."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    server, spy = await _server(
        8488, store, poster, handlers=_handlers("ops", SpyHandler(explodes=True))
    )
    try:
        token = await _post_handled_form(store, poster, "ops")
        await _submit(8488, token, {"summary": "crash"})

        assert spy.submitted == []
        assert poster.retracted == []
    finally:
        await server.stop()
        await store.close()


async def test_a_form_naming_an_unregistered_handler_is_dropped(tmp_path: Path) -> None:
    """A composition error, and the safe reading of one: the values go nowhere
    rather than to whoever happens to be listening."""
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    poster = FakePoster()
    server, spy = await _server(8489, store, poster, handlers=FormHandlers())
    try:
        token = await _post_handled_form(store, poster, "nobody-registered-this")
        await _submit(8489, token, {"summary": "crash"})

        assert spy.submitted == []
        assert poster.retracted == []
    finally:
        await server.stop()
        await store.close()
