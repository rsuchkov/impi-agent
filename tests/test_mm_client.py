from typing import cast

import pytest
from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.mattermost.client import MattermostChatClient, chunk_text
from crucible.gateways.mattermost.options import driver_options
from crucible.ports.chat.types import ConversationRef


class _Recorder:
    """Fake AsyncTypedDriver surface: records endpoint calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.client = type("C", (), {"userid": "botid"})()

        recorder = self

        class Posts:
            async def create_post(self, **kwargs):
                recorder.calls.append(("create_post", kwargs))
                return {"id": "posted-id"}

            async def patch_post(self, post_id, **options):
                recorder.calls.append(("patch_post", {"post_id": post_id, **options}))
                return {"id": post_id}

            async def create_post_ephemeral(self, user_id, post):
                recorder.calls.append(
                    ("create_post_ephemeral", {"user_id": user_id, "post": post})
                )
                return {"id": "eph-id"}

        class Reactions:
            async def save_reaction(self, options):
                recorder.calls.append(("save_reaction", options))

            async def delete_reaction(self, user_id, post_id, emoji_name):
                recorder.calls.append(
                    ("delete_reaction", {"user_id": user_id, "post_id": post_id, "emoji_name": emoji_name})
                )

        class Users:
            async def get_user(self, user_id):
                recorder.calls.append(("get_user", {"user_id": user_id}))
                # Non-ASCII name on purpose: display names must survive unicode.
                return {"username": "roman", "nickname": "", "first_name": "Роман", "last_name": "", "is_bot": False}

        class IntegrationActions:
            async def open_interactive_dialog(self, trigger_id, url, dialog):
                recorder.calls.append(
                    ("open_dialog", {"trigger_id": trigger_id, "url": url, "dialog": dialog})
                )

        self.posts = Posts()
        self.reactions = Reactions()
        self.users = Users()
        self.integration_actions = IntegrationActions()


REF = ConversationRef(
    channel_id="chan1", conversation_id="root1", message_id="post1", thread_root_id="root1"
)


def _client(driver: _Recorder, **kwargs) -> MattermostChatClient:
    # The recorder is a structural stand-in; nominal typing needs one cast.
    return MattermostChatClient(cast(AsyncTypedDriver, driver), **kwargs)


async def test_post_reply_threads_and_chunks() -> None:
    driver = _Recorder()
    chat = _client(driver, max_post_chars=10)

    await chat.post_reply(REF, "aaaa\n\nbbbb\n\ncccc")

    posts = [kwargs for name, kwargs in driver.calls if name == "create_post"]
    assert len(posts) > 1  # chunking kicked in
    assert all(p["channel_id"] == "chan1" and p["root_id"] == "root1" for p in posts)
    assert "".join(p["message"] for p in posts).replace("\n", "") == "aaaabbbbcccc"


async def test_post_reply_top_level_when_no_root() -> None:
    driver = _Recorder()
    chat = _client(driver)
    ref = ConversationRef(channel_id="dm1", conversation_id="dm1", message_id="p1")

    await chat.post_reply(ref, "hello")

    name, kwargs = driver.calls[0]
    assert kwargs["root_id"] is None  # DM: reply top-level


async def test_reactions_use_own_identity_and_never_raise() -> None:
    driver = _Recorder()
    chat = _client(driver)

    await chat.add_reaction(REF, "eyes")
    await chat.remove_reaction(REF, "eyes")

    assert driver.calls[0] == (
        "save_reaction",
        {"user_id": "botid", "post_id": "post1", "emoji_name": "eyes"},
    )
    assert driver.calls[1][0] == "delete_reaction"

    class Exploding(_Recorder):
        def __init__(self):
            super().__init__()

            class Boom:
                async def save_reaction(self, options):
                    raise RuntimeError("api down")

                async def delete_reaction(self, *a):
                    raise RuntimeError("api down")

            self.reactions = Boom()

    quiet = _client(Exploding())
    await quiet.add_reaction(REF, "eyes")  # must not raise
    await quiet.remove_reaction(REF, "eyes")


async def test_get_user_profile_builds_display_name() -> None:
    chat = _client(_Recorder())
    profile = await chat.get_user_profile("uid")
    assert profile is not None
    assert profile.username == "roman"
    assert profile.display_name == "Роман"
    assert chat.format_mention(profile.username) == "@roman"


def test_chunk_text_short_passthrough() -> None:
    assert chunk_text("hello", 100) == ["hello"]


def test_chunk_text_prefers_paragraph_breaks() -> None:
    text = "first paragraph\n\nsecond paragraph\n\nthird"
    chunks = chunk_text(text, len("first paragraph") + 5)
    assert chunks[0] == "first paragraph"
    assert all(len(c) <= len("first paragraph") + 5 for c in chunks)


def test_chunk_text_hard_cuts_unbreakable_text() -> None:
    chunks = chunk_text("x" * 25, 10)
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert "".join(chunks) == "x" * 25


def test_driver_options_parses_url() -> None:
    opts = driver_options("http://localhost:8065", "tok")
    assert (opts["scheme"], opts["url"], opts["port"]) == ("http", "localhost", 8065)
    assert opts["token"] == "tok"
    assert opts["keepalive"] is True

    https = driver_options("https://mm.example.com", "tok")
    assert (https["scheme"], https["port"]) == ("https", 443)


def test_driver_options_rejects_bare_host() -> None:
    with pytest.raises(ValueError):
        driver_options("localhost:8065", "tok")


async def test_post_actions_renders_interactive_buttons() -> None:
    from crucible.ports.chat.types import Action
    driver = _Recorder()
    chat = _client(driver)
    ref = ConversationRef(channel_id="ch1", conversation_id="root1", message_id="p1", thread_root_id="root1")
    actions = [Action(id="a_yes", label="Yes", value="yes", context={"interaction": "i1", "token": "t1"})]

    await chat.post_actions(ref, "Pick:", actions, callback_url="http://host.containers.internal:8423/interact")

    name, kwargs = driver.calls[0]
    assert name == "create_post"
    act = kwargs["props"]["attachments"][0]["actions"][0]
    assert act["type"] == "button"  # без type MM выкинет integration
    assert act["integration"]["url"].endswith("/interact")
    assert act["integration"]["context"] == {"interaction": "i1", "token": "t1", "value": "yes"}
    assert kwargs["root_id"] == "root1"


async def test_post_actions_renders_select_menu() -> None:
    from crucible.ports.chat.types import Action, Choice
    driver = _Recorder()
    chat = _client(driver)
    ref = ConversationRef(channel_id="ch1", conversation_id="dm1", message_id="dm1", thread_root_id="")
    actions = [Action(id="sel", label="Select an option", kind="select",
                      options=Choice.of("A", "B", "C"), context={"token": "t1"})]

    await chat.post_actions(ref, "City?", actions, callback_url="http://x/interact")

    act = driver.calls[0][1]["props"]["attachments"][0]["actions"][0]
    assert act["type"] == "select"  # a dropdown, not buttons
    assert act["options"] == [{"text": "A", "value": "A"}, {"text": "B", "value": "B"},
                              {"text": "C", "value": "C"}]
    # no static value: the pick returns via selected_option
    assert act["integration"]["context"] == {"token": "t1"}


async def test_retract_rewrites_message_and_drops_attachments() -> None:
    driver = _Recorder()
    chat = _client(driver)

    await chat.retract("posted-id", "⌛ expired")

    name, kwargs = driver.calls[0]
    assert name == "patch_post"
    assert kwargs["post_id"] == "posted-id"
    assert kwargs["message"] == "⌛ expired"
    assert kwargs["props"] == {"attachments": []}  # buttons gone


async def test_open_dialog_builds_elements_from_form() -> None:
    from crucible.ports.chat.types import Form, FormField
    driver = _Recorder()
    chat = _client(driver)
    form = Form(title="T", submit_label="Go", fields=(
        FormField(name="s", label="Summary", type="text", placeholder="one line"),
        FormField(name="p", label="Prio", type="select", options=("low", "high")),
        FormField(name="u", label="Urgent", type="bool", optional=True),
    ))

    await chat.open_dialog("trg", form, submit_url="http://x/dialog", state="tok")

    name, kwargs = driver.calls[0]
    assert name == "open_dialog"
    assert kwargs["trigger_id"] == "trg" and kwargs["url"] == "http://x/dialog"
    d = kwargs["dialog"]
    assert d["title"] == "T" and d["submit_label"] == "Go" and d["state"] == "tok"
    els = d["elements"]
    assert els[0] == {"display_name": "Summary", "name": "s", "type": "text",
                      "subtype": "text", "optional": False, "placeholder": "one line"}
    assert els[1]["type"] == "select"
    assert els[1]["options"] == [{"text": "low", "value": "low"}, {"text": "high", "value": "high"}]
    # a checkbox's own caption comes from placeholder, which MM renders next to it
    assert els[2] == {"display_name": "Urgent", "name": "u", "type": "bool",
                      "optional": True, "placeholder": "Urgent"}


async def test_post_ephemeral_calls_create_post_ephemeral() -> None:
    driver = _Recorder()
    await _client(driver).post_ephemeral("chan1", "u-42", "only you see this")
    call = next(kw for name, kw in driver.calls if name == "create_post_ephemeral")
    assert call["user_id"] == "u-42"
    assert call["post"] == {"channel_id": "chan1", "message": "only you see this"}


async def test_snippets_carry_the_author_user_id() -> None:
    # The flow needs the author id to tell its own posts apart in a backfill.
    class WithThread(_Recorder):
        def __init__(self):
            super().__init__()
            recorder = self

            class Posts:
                async def get_post_thread(self, root_id):
                    recorder.calls.append(("get_post_thread", {"root_id": root_id}))
                    return {"posts": {"p1": {"id": "p1", "user_id": "u-author",
                                             "message": "hello", "create_at": 1}}}

            self.posts = Posts()

    snippets = await _client(WithThread()).get_thread_posts(REF)
    assert [(s.message_id, s.user_id) for s in snippets] == [("p1", "u-author")]


# --- the full field vocabulary -------------------------------------------------

async def test_dialog_renders_every_field_type() -> None:
    from crucible.ports.chat.types import Form, FormField
    driver = _Recorder()
    types = ("text", "textarea", "number", "email", "url", "tel", "select", "multiselect",
             "radio", "bool", "user", "users", "channel", "channels", "date", "datetime", "time")
    fields = tuple(
        FormField(name=t, label=t.title(), type=t,
                  options=("a", "b") if t in ("select", "multiselect", "radio") else ())
        for t in types
    )
    await _client(driver).open_dialog(
        "trg", Form(title="All", fields=fields), submit_url="http://x/dialog", state="s"
    )

    els = {e["name"]: e for e in driver.calls[0][1]["dialog"]["elements"]}
    assert els["text"]["type"] == "text" and els["text"]["subtype"] == "text"
    assert els["textarea"]["type"] == "textarea"
    assert els["number"]["subtype"] == "number"
    assert els["email"]["subtype"] == "email"
    assert els["url"]["subtype"] == "url"
    assert els["tel"]["subtype"] == "tel"
    assert els["select"]["type"] == "select" and "multiselect" not in els["select"]
    assert els["multiselect"]["multiselect"] is True
    assert els["radio"]["type"] == "radio"
    assert els["bool"]["type"] == "bool"
    # The workspace pickers are selects fed by the server, not by us.
    assert els["user"] == {"display_name": "User", "name": "user", "optional": False,
                           "type": "select", "data_source": "users"}
    assert els["users"]["data_source"] == "users" and els["users"]["multiselect"] is True
    assert els["channel"]["data_source"] == "channels"
    assert els["channels"]["data_source"] == "channels" and els["channels"]["multiselect"] is True
    assert els["date"]["type"] == "date"
    assert els["datetime"]["type"] == "datetime"
    # No time picker in Mattermost: a text field that says what it wants.
    assert els["time"]["type"] == "text" and els["time"]["placeholder"] == "HH:MM"


async def test_label_fields_become_the_dialog_introduction() -> None:
    from crucible.ports.chat.types import Form, FormField
    driver = _Recorder()
    form = Form(title="T", intro="Before we start:", fields=(
        FormField(name="n1", label="**Section one**", type="label"),
        FormField(name="who", label="Who", type="user"),
    ))

    await _client(driver).open_dialog("trg", form, submit_url="http://x", state="s")

    dialog = driver.calls[0][1]["dialog"]
    assert dialog["introduction_text"] == "Before we start:\n\n**Section one**"
    assert [e["name"] for e in dialog["elements"]] == ["who"]  # a label collects nothing


async def test_dialog_failure_names_the_field_types() -> None:
    # The usual cause is a server too old for an element (date/datetime need 11.1).
    from crucible.ports.chat.types import Form, FormField

    class _Refusing:
        async def open_interactive_dialog(self, trigger_id, url, dialog):
            raise RuntimeError("400: invalid element type")

    driver = _Recorder()
    driver.integration_actions = _Refusing()  # type: ignore[assignment]
    form = Form(title="T", fields=(FormField(name="d", label="When", type="date"),))
    with pytest.raises(RuntimeError, match="field types: date"):
        await _client(driver).open_dialog("trg", form, submit_url="http://x", state="s")


async def test_post_actions_renders_the_workspace_pickers() -> None:
    from crucible.ports.chat.types import Action
    driver = _Recorder()
    actions = [Action(id="sel", label="Who?", kind="user_select", context={"token": "T", "pick": "user"})]

    await _client(driver).post_actions(REF, "Assign to", actions, callback_url="http://x/interact")

    posted = next(kw for name, kw in driver.calls if name == "create_post")
    action = posted["props"]["attachments"][0]["actions"][0]
    assert action["type"] == "select" and action["data_source"] == "users"
    assert "options" not in action  # the server supplies the people
    assert action["integration"]["context"]["pick"] == "user"  # marks the id as resolvable


def test_the_dialog_builder_covers_the_whole_vocabulary() -> None:
    # Every neutral field type must map to a real element: an unhandled one would
    # fall through to the select branch and produce an options-less dropdown.
    from crucible.gateways.mattermost.dialogs import build_dialog
    from crucible.ports.chat.types import (
        FIELD_TYPES,
        STATIC_FIELD_TYPES,
        Form,
        FormField,
    )

    fields = tuple(
        FormField(name=t, label=t, type=t,
                  options=("a", "b") if t in ("select", "multiselect", "radio") else ())
        for t in FIELD_TYPES
        if t not in STATIC_FIELD_TYPES
    )
    elements = build_dialog(Form(title="T", fields=fields), state="s")["elements"]
    assert len(elements) == len(fields)
    for el in elements:
        assert el["type"] in ("text", "textarea", "select", "radio", "bool", "date", "datetime")
        # a dropdown must be fed by something: our options or the server
        if el["type"] == "select":
            assert "options" in el or "data_source" in el


async def test_action_ids_are_alphanumeric_and_unique() -> None:
    # Mattermost routes a click as /posts/{post}/actions/{action_id} and its
    # router matches alphanumerics only: a readable id like "open-greek-drill"
    # 404s and the click never reaches the engine (caught live).
    from crucible.ports.chat.types import Action, Card
    driver = _Recorder()
    cards = [
        Card(text="a", actions=(Action(id="open-greek-drill", label="Details"),)),
        Card(text="b", actions=(Action(id="open-greek-drill", label="Details"),)),
    ]

    await _client(driver).post_cards(REF, cards, callback_url="http://x/interact")

    posted = next(kw for name, kw in driver.calls if name == "create_post")
    ids = [a["id"] for att in posted["props"]["attachments"] for a in att["actions"]]
    assert all(i.isalnum() for i in ids), ids
    assert len(set(ids)) == len(ids)  # the same label twice must not collide


async def test_cards_become_one_attachment_each() -> None:
    from crucible.ports.chat.types import Action, Card
    driver = _Recorder()

    await _client(driver).post_cards(
        REF,
        [
            Card(text="**header**", accent="#7a5299"),
            Card(text="skill", actions=(Action(id="open", label="Details"),), accent="#3db887"),
        ],
        callback_url="http://x/interact",
    )

    attachments = next(kw for name, kw in driver.calls if name == "create_post")["props"]["attachments"]
    assert [a["text"] for a in attachments] == ["**header**", "skill"]
    assert [a["color"] for a in attachments] == ["#7a5299", "#3db887"]
    assert "actions" not in attachments[0]  # a card without controls has none
