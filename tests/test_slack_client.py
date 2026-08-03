"""SlackChatClient over a fake AsyncWebClient (offline; no network)."""

from slack_sdk.errors import SlackApiError

from crucible.gateways.slack.client import SlackChatClient
from crucible.ports.chat.types import Action, ConversationRef, Form, FormField


class FakeWeb:
    """Records calls; returns canned responses. Duck-types AsyncWebClient."""

    def __init__(self, **canned) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._canned = canned
        self.raise_on: dict[str, SlackApiError] = {}

    def __getattr__(self, method_name: str):
        async def method(**kwargs):
            self.calls.append((method_name, kwargs))
            if method_name in self.raise_on:
                raise self.raise_on[method_name]
            return self._canned.get(method_name, {})
        return method

    def last(self, name: str) -> dict:
        return next(kw for n, kw in reversed(self.calls) if n == name)


def _sc(web) -> SlackChatClient:
    return SlackChatClient(web)  # type: ignore[arg-type]  # FakeWeb duck-types AsyncWebClient


def _ref(channel="C1", conv="100.1", msg="100.1", root="100.1") -> ConversationRef:
    return ConversationRef(channel_id=channel, conversation_id=conv, message_id=msg, thread_root_id=root)


async def test_post_reply_into_thread() -> None:
    web = FakeWeb()
    await _sc(web).post_reply(_ref(root="100.1"), "hello")
    kw = web.last("chat_postMessage")
    assert kw["channel"] == "C1" and kw["thread_ts"] == "100.1" and kw["text"] == "hello"


async def test_post_reply_top_level_has_no_thread_ts() -> None:
    web = FakeWeb()
    await _sc(web).post_reply(_ref(root=""), "hi")  # DM/channel session
    assert web.last("chat_postMessage")["thread_ts"] is None


async def test_add_reaction_targets_the_message_and_swallows_dupes() -> None:
    web = FakeWeb()
    web.raise_on["reactions_add"] = SlackApiError("x", {"error": "already_reacted"})
    await _sc(web).add_reaction(_ref(msg="105.2"), "eyes")  # must not raise
    kw = web.last("reactions_add")
    assert kw["timestamp"] == "105.2" and kw["name"] == "eyes"


async def test_post_actions_returns_channel_and_ts_and_retract_updates() -> None:
    web = FakeWeb(chat_postMessage={"channel": "C1", "ts": "200.5"})
    client = _sc(web)
    action = Action(id="o0", label="Yes", value="Yes", context={"token": "T"})
    post_id = await client.post_actions(_ref(), "Choose", [action], callback_url="unused")
    assert post_id == "C1\x1f200.5"
    kw = web.last("chat_postMessage")
    assert kw["blocks"][0]["type"] == "section"

    await client.retract(post_id, "expired")
    up = web.last("chat_update")
    assert up["channel"] == "C1" and up["ts"] == "200.5" and up["blocks"] == []


async def test_open_dialog_opens_a_view_with_the_form_token() -> None:
    web = FakeWeb()
    form = Form(title="Bug", fields=(FormField(name="s", label="Summary"),))
    await _sc(web).open_dialog("TRIG", form, submit_url="unused", state="FTOK")
    kw = web.last("views_open")
    assert kw["trigger_id"] == "TRIG"
    assert kw["view"]["private_metadata"] == "FTOK"


async def test_get_recent_posts_is_chronological() -> None:
    web = FakeWeb(
        conversations_history={
            "messages": [  # Slack returns newest-first
                {"ts": "3", "user": "U", "text": "third"},
                {"ts": "2", "user": "U", "text": "second"},
                {"ts": "1", "user": "U", "text": "first"},
            ]
        },
        users_info={"user": {"name": "roman", "profile": {}}},
    )
    posts = await _sc(web).get_recent_posts("C1", limit=10)
    assert [p.text for p in posts] == ["first", "second", "third"]
    assert posts[0].username == "roman"


async def test_get_thread_posts_uses_channel_and_root() -> None:
    web = FakeWeb(
        conversations_replies={"messages": [{"ts": "1", "user": "U", "text": "root"}]},
        users_info={"user": {"name": "u", "profile": {}}},
    )
    await _sc(web).get_thread_posts(_ref(channel="C9", root="50.1"))
    kw = web.last("conversations_replies")
    assert kw["channel"] == "C9" and kw["ts"] == "50.1"


# --- ChatAdmin port ---------------------------------------------------------


async def test_create_channel_slugifies_and_sets_purpose() -> None:
    web = FakeWeb(conversations_create={"channel": {"id": "C9"}})
    cid = await _sc(web).create_channel("War Room!", "War Room", private=True, purpose="the plan")
    assert cid == "C9"
    create = web.last("conversations_create")
    assert create["name"] == "war-room" and create["is_private"] is True  # slugified
    assert web.last("conversations_setPurpose")["purpose"] == "the plan"


async def test_invite_and_get_members() -> None:
    web = FakeWeb(
        conversations_members={"members": ["U1", "U2"]},
        users_info={"user": {"name": "roman", "profile": {}}},
    )
    client = _sc(web)
    await client.invite_to_channel("C1", "U9")
    assert web.last("conversations_invite") == {"channel": "C1", "users": "U9"}
    members = await client.get_channel_members("C1")
    assert [m.user_id for m in members] == ["U1", "U2"]


async def test_post_message_returns_ts() -> None:
    web = FakeWeb(chat_postMessage={"ts": "3.3"})
    assert await _sc(web).post_message("C1", "hello") == "3.3"
    assert web.last("chat_postMessage") == {"channel": "C1", "text": "hello"}


async def test_resolve_username_scans_the_workspace() -> None:
    web = FakeWeb(users_list={"members": [{"name": "roman", "id": "U1"}, {"name": "dev", "id": "U2"}]})
    assert await _sc(web).resolve_username("@dev") == "U2"
    assert await _sc(web).resolve_username("nobody") is None


# --- outgoing formatting (markdown -> mrkdwn at the adapter boundary) ---------


async def test_post_reply_converts_markdown_to_mrkdwn() -> None:
    web = FakeWeb()
    await _sc(web).post_reply(_ref(), "# Report\n**done**, see [docs](https://x.com)")
    text = web.last("chat_postMessage")["text"]
    assert text == "*Report*\n*done*, see <https://x.com|docs>"


async def test_post_notice_is_verbatim() -> None:
    # Port contract: notices are fixed system strings, never reformatted.
    web = FakeWeb()
    await _sc(web).post_notice(_ref(), "**not** rendered [x](y)")
    assert web.last("chat_postMessage")["text"] == "**not** rendered [x](y)"


async def test_post_actions_section_text_is_converted() -> None:
    web = FakeWeb(chat_postMessage={"channel": "C1", "ts": "1.2"})
    await _sc(web).post_actions(
        _ref(), "Deploy **prod**?", [Action(id="a1", label="Go")], callback_url="unused"
    )
    kw = web.last("chat_postMessage")
    assert kw["text"] == "Deploy *prod*?"
    section = kw["blocks"][0]
    assert section["text"]["text"] == "Deploy *prod*?"


async def test_post_message_converts_markdown() -> None:
    web = FakeWeb(chat_postMessage={"ts": "3.4"})
    await _sc(web).post_message("C9", "**ping** [here](https://a.b)")
    assert web.last("chat_postMessage")["text"] == "*ping* <https://a.b|here>"


async def test_long_reply_formats_before_chunking_fence_not_split() -> None:
    # A fence longer than the chunk limit: conversion must happen before the
    # splitter, and chunk boundaries must not fall mid-token (paragraph cuts).
    web = FakeWeb()
    client = SlackChatClient(web, max_post_chars=200)  # type: ignore[arg-type]
    code = "\n".join(f"line_{i} = '**not bold**'" for i in range(20))
    text = f"**intro**\n\n```python\n{code}\n```\n\ntail **bold**"
    await client.post_reply(_ref(), text)
    chunks = [kw["text"] for n, kw in web.calls if n == "chat_postMessage"]
    joined = "\n\n".join(chunks)
    assert "**not bold**" in joined          # fence content untouched
    assert "*intro*" in joined and "tail *bold*" in joined
    assert all(len(c) <= 200 for c in chunks)


async def test_post_ephemeral_converts_markdown_and_targets_user() -> None:
    web = FakeWeb()
    await _sc(web).post_ephemeral("C1", "U9", "**secret** [x](https://a.b)")
    kw = web.last("chat_postEphemeral")
    assert kw["channel"] == "C1" and kw["user"] == "U9"
    assert kw["text"] == "*secret* <https://a.b|x>"


async def test_snippets_carry_the_author_user_id() -> None:
    web = FakeWeb(conversations_replies={"messages": [
        {"ts": "100.1", "user": "U-author", "text": "hello"},
    ]})
    snippets = await _sc(web).get_thread_posts(_ref())
    assert [(s.message_id, s.user_id) for s in snippets] == [("100.1", "U-author")]
