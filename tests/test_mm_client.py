from typing import cast

import pytest
from mattermostautodriver import AsyncTypedDriver

from crucible.ports.chat.types import ConversationRef
from crucible.gateways.mattermost.client import MattermostChatClient, chunk_text
from crucible.gateways.mattermost.options import driver_options


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
    from crucible.ports.chat.types import Action
    driver = _Recorder()
    chat = _client(driver)
    ref = ConversationRef(channel_id="ch1", conversation_id="dm1", message_id="dm1", thread_root_id="")
    actions = [Action(id="sel", label="Select an option", kind="select",
                      options=("A", "B", "C"), context={"token": "t1"})]

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
                      "optional": False, "placeholder": "one line"}
    assert els[1]["type"] == "select"
    assert els[1]["options"] == [{"text": "low", "value": "low"}, {"text": "high", "value": "high"}]
    assert els[2] == {"display_name": "Urgent", "name": "u", "type": "bool", "optional": True}
