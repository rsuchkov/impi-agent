"""A reaction as a command on Slack (gateways/slack/reactions.py), and what the
two adapters share (gateways/reactions.py): the emoji name as the map spells
it, the command's text, and who may not trigger one."""

import logging

import pytest
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from crucible.gateways.reactions import (
    ReactionCommands,
    command_text,
    emoji_name,
    reacted_by_an_agent,
)
from crucible.gateways.slack.gateway import SlackGateway
from crucible.gateways.slack.reactions import SlackReactions, parse_reaction
from crucible.ports.chat.types import UserProfile
from tests.fakes.slack import FakeDispatcher, FakeSink

OWN = "UBOT"
ROMAN = "U0HNU8P60"
_APPS: list[AsyncApp] = []


@pytest.fixture(autouse=True)
async def _close_apps():
    yield
    for app in _APPS:
        try:
            await app.client.session.close()  # type: ignore[union-attr]
        except Exception:
            pass
    _APPS.clear()


# The shape Slack sends for reaction_added, as bolt hands the handler its `event`.
def _event(reaction="white_check_mark", user=ROMAN, channel="C0BC51KQWTX", ts="1782309753.848289", item_type="message"):
    return {
        "type": "reaction_added", "user": user, "reaction": reaction,
        "item": {"type": item_type, "channel": channel, "ts": ts},
        "item_user": "U0OTHER", "event_ts": "1782309760.000100",
    }


class FakeChat:
    def __init__(self, username: str = "roman.suchkov") -> None:
        self._username = username

    async def get_user_profile(self, user_id: str):
        return UserProfile(username=self._username) if self._username else None


class Directory:
    def __init__(self, ids: set[str]) -> None:
        self._ids = frozenset(ids)

    def agent_user_ids(self):
        return self._ids

    def list_agents(self):
        return []


def _reactions(dispatcher, *, commands=None, messages=None, fail=None, chat=None, directory=None, **kwargs):
    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
    _APPS.append(app)
    calls: list[dict] = []

    async def conversations_replies(**kw):
        calls.append(kw)
        if fail is not None:
            raise fail
        return {"ok": True, "messages": messages or []}

    app.client.conversations_replies = conversations_replies  # type: ignore[method-assign]
    reactions = SlackReactions(
        app, dispatcher, chat or FakeChat(), agent="assistant",  # type: ignore[arg-type]
        commands=ReactionCommands(
            commands if commands is not None else {"white_check_mark": "done", "eyes": "summarize"}, **kwargs
        ),
        own_user_id=lambda: OWN, directory=directory,
    )
    reactions.register()
    reactions.replies_calls = calls  # type: ignore[attr-defined]
    return reactions


def _message(ts="1782309753.848289", text="a reply in the thread", thread_ts=""):
    message = {"type": "message", "ts": ts, "text": text, "user": "U0OTHER"}
    if thread_ts:
        message["thread_ts"] = thread_ts
    return message


# --- the shared pieces ---------------------------------------------------------


def test_the_emoji_name_is_spelled_like_the_map() -> None:
    assert emoji_name(":+1::skin-tone-2:") == "+1"
    assert emoji_name("White_Check_Mark") == "white_check_mark"
    assert emoji_name("") == ""


def test_the_command_text_is_the_word_then_the_message_quoted() -> None:
    assert command_text("done", "", 100) == "/done"
    assert command_text("done", "fix the build\nplease", 100) == "/done\n> fix the build\n> please"


def test_the_quote_is_cut_at_the_limit() -> None:
    text = command_text("done", "x" * 50, 10)
    assert text == "/done\n> " + "x" * 9 + "…"


def test_an_agents_own_reaction_and_another_agents_are_not_a_command() -> None:
    directory = Directory({OWN, "UDEV"})
    assert reacted_by_an_agent(OWN, OWN, None)
    assert reacted_by_an_agent("UDEV", OWN, directory)
    assert not reacted_by_an_agent(ROMAN, OWN, directory)
    assert not reacted_by_an_agent("UDEV", OWN, None)  # nobody to say it is an agent


# --- parsing ---------------------------------------------------------------------


def test_a_reaction_on_a_message_is_parsed() -> None:
    reaction = parse_reaction(_event(reaction="+1::skin-tone-3"))
    assert reaction is not None
    assert (reaction.emoji, reaction.user_id, reaction.channel_id, reaction.message_id) == (
        "+1", ROMAN, "C0BC51KQWTX", "1782309753.848289",
    )


def test_a_reaction_on_a_file_or_an_incomplete_event_is_not() -> None:
    assert parse_reaction(_event(item_type="file")) is None
    assert parse_reaction(_event(user="")) is None
    assert parse_reaction(_event(ts="")) is None
    assert parse_reaction(_event(reaction="")) is None


# --- the command ---------------------------------------------------------------


async def test_a_mapped_reaction_in_a_thread_runs_the_command_on_the_thread() -> None:
    dispatcher = FakeDispatcher()
    reactions = _reactions(dispatcher, messages=[_message(thread_ts="1782309611.465749")])
    await reactions.handle_reaction(_event())
    call = dispatcher.calls[0]
    assert call[0] == "invoke_command" and call[1] == "assistant"
    assert call[2] == "C0BC51KQWTX"
    assert call[3] == "1782309611.465749" and call[4] == "thread"  # the thread root
    assert call[5] == "/done\n> a reply in the thread"
    assert call[6] == ROMAN and call[7] == "roman.suchkov"
    # The message was read back by its own ts, once.
    assert reactions.replies_calls == [{  # type: ignore[attr-defined]
        "channel": "C0BC51KQWTX", "ts": "1782309753.848289", "oldest": "1782309753.848289",
        "latest": "1782309753.848289", "inclusive": True, "limit": 1,
    }]


async def test_a_reaction_on_a_top_level_message_answers_in_a_thread_under_it() -> None:
    dispatcher = FakeDispatcher()
    await _reactions(dispatcher, messages=[_message(text="top level")]).handle_reaction(_event())
    call = dispatcher.calls[0]
    assert call[3] == "1782309753.848289" and call[4] == "thread"


async def test_a_reaction_in_a_dm_runs_as_the_dm_conversation() -> None:
    dispatcher = FakeDispatcher()
    await _reactions(dispatcher, messages=[_message()]).handle_reaction(_event(channel="D0123"))
    call = dispatcher.calls[0]
    assert call[2] == "D0123" and call[3] == "D0123" and call[4] == "dm"


async def test_an_unmapped_emoji_does_nothing() -> None:
    dispatcher = FakeDispatcher()
    reactions = _reactions(dispatcher, messages=[_message()])
    await reactions.handle_reaction(_event(reaction="tada"))
    assert dispatcher.calls == [] and reactions.replies_calls == []  # type: ignore[attr-defined]


async def test_the_agents_own_reaction_and_another_agents_do_nothing() -> None:
    # The agent marks a message it works on with :eyes: itself; another agent
    # in the channel does the same. Neither may start a command.
    dispatcher = FakeDispatcher()
    reactions = _reactions(dispatcher, messages=[_message()], directory=Directory({OWN, "UDEV"}))
    await reactions.handle_reaction(_event(reaction="eyes", user=OWN))
    await reactions.handle_reaction(_event(reaction="eyes", user="UDEV"))
    assert dispatcher.calls == []
    await reactions.handle_reaction(_event(reaction="eyes", user=ROMAN))
    assert dispatcher.calls[0][5] == "/summarize\n> a reply in the thread"


async def test_without_the_message_the_command_still_runs_in_the_thread_the_event_names(caplog) -> None:
    dispatcher = FakeDispatcher()
    missing = SlackApiError("missing", {"ok": False, "error": "missing_scope"})
    reactions = _reactions(dispatcher, fail=missing)
    with caplog.at_level(logging.WARNING):
        await reactions.handle_reaction(_event())
    call = dispatcher.calls[0]
    assert call[3] == "1782309753.848289" and call[5] == "/done"  # no quote, no thread
    assert "history scope" in caplog.text


async def test_the_message_is_picked_by_its_ts_among_what_slack_returns() -> None:
    # conversations.replies on a reply may answer with the thread; the quote
    # is the message the reaction was put on, not the parent.
    dispatcher = FakeDispatcher()
    reactions = _reactions(dispatcher, messages=[
        _message(ts="1782309611.465749", text="the parent"),
        _message(text="the reply", thread_ts="1782309611.465749"),
    ])
    await reactions.handle_reaction(_event())
    assert dispatcher.calls[0][5] == "/done\n> the reply"


async def test_a_person_without_a_profile_is_named_by_id_only() -> None:
    dispatcher = FakeDispatcher()
    await _reactions(dispatcher, messages=[_message()], chat=FakeChat(username="")).handle_reaction(_event())
    assert dispatcher.calls[0][6] == ROMAN and dispatcher.calls[0][7] == ""


# --- the gateway builds it only with a map and a dispatcher ----------------------


async def test_the_gateway_has_reactions_only_with_a_map_and_a_dispatcher() -> None:
    handlers = []

    def gateway(**kwargs):
        app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
        _APPS.append(app)
        gw = SlackGateway(app, "xapp-fake", FakeSink(), FakeChat(), **kwargs)  # type: ignore[arg-type]
        handlers.append(gw._handler)
        return gw

    try:
        _check_gateways(gateway)
    finally:
        for handler in handlers:
            await handler.close_async()


def _check_gateways(gateway) -> None:

    assert gateway().reactions is None
    assert gateway(reaction_commands=ReactionCommands({"eyes": "summarize"})).reactions is None  # no dispatcher
    assert gateway(dispatcher=FakeDispatcher()).reactions is None  # no map
    assert gateway(dispatcher=FakeDispatcher(), reaction_commands=ReactionCommands()).reactions is None  # empty map
    assert gateway(dispatcher=FakeDispatcher(), reaction_commands=ReactionCommands({"eyes": "summarize"})).reactions is not None
