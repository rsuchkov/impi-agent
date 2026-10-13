"""A reaction as a command on Mattermost (gateways/mattermost/reactions.py and
the reaction_added frame in events.py)."""

import json
from typing import Any, cast

from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.mattermost.events import parse_reaction
from crucible.gateways.mattermost.gateway import MattermostGateway
from crucible.gateways.mattermost.reactions import MattermostReactions
from crucible.gateways.reactions import Reaction, ReactionCommands
from crucible.ports.chat.types import UserProfile
from tests.fakes.slack import FakeDispatcher

ME = "uid-assistant"
HUMAN = "uid-roman"


def _frame(emoji="white_check_mark", user_id=HUMAN, post_id="p1", channel_id="ch1", event="reaction_added") -> dict:
    reaction = {"user_id": user_id, "post_id": post_id, "emoji_name": emoji, "create_at": 1782309760000}
    return {
        "event": event,
        "data": {"reaction": json.dumps(reaction)},
        "broadcast": {"omit_users": None, "user_id": "", "channel_id": channel_id, "team_id": ""},
        "seq": 7,
    }


class FakeDriver:
    """The two reads the adapter makes: the post under the reaction, and the
    channel's type when the post has no thread."""

    def __init__(self, posts: dict[str, dict], channels: dict[str, str] | None = None, *, post_error=False) -> None:
        driver = self
        self.calls: list[tuple[str, str]] = []

        class Posts:
            async def get_post(self, post_id):
                driver.calls.append(("get_post", post_id))
                if post_error:
                    raise RuntimeError("no such post")
                return posts[post_id]

        class Channels:
            async def get_channel(self, channel_id):
                driver.calls.append(("get_channel", channel_id))
                return {"id": channel_id, "type": (channels or {}).get(channel_id, "O")}

            async def get_channel_members(self, channel_id, per_page=200):
                return []

        self.posts = Posts()
        self.channels = Channels()
        self.files: Any = None


class FakeChat:
    async def get_user_profile(self, user_id: str):
        return UserProfile(username="roman")


class Directory:
    def __init__(self, ids: set[str]) -> None:
        self._ids = frozenset(ids)

    def agent_user_ids(self):
        return self._ids

    def list_agents(self):
        return []


def _post(post_id="p1", channel_id="ch1", root_id="", message="fix the build") -> dict:
    return {"id": post_id, "channel_id": channel_id, "root_id": root_id, "message": message, "user_id": "uid-other"}


def _reactions(dispatcher, driver, *, commands=None, directory=None, quote_limit=1500) -> MattermostReactions:
    return MattermostReactions(
        cast(AsyncTypedDriver, driver), dispatcher, FakeChat(), agent="assistant",  # type: ignore[arg-type]
        commands=ReactionCommands(
            commands if commands is not None else {"white_check_mark": "done"}, quote_limit=quote_limit
        ),
        directory=directory,
    )


# --- the frame -------------------------------------------------------------------


def test_a_reaction_frame_is_parsed_with_its_channel_from_the_broadcast() -> None:
    reaction = parse_reaction(_frame(emoji="+1"))
    assert reaction == Reaction(emoji="+1", user_id=HUMAN, channel_id="ch1", message_id="p1")


def test_other_frames_and_incomplete_reactions_are_not() -> None:
    assert parse_reaction(_frame(event="reaction_removed")) is None
    assert parse_reaction(_frame(event="posted")) is None
    assert parse_reaction(_frame(post_id="")) is None
    assert parse_reaction({"event": "reaction_added", "data": {"reaction": "not json"}}) is None


# --- the command -----------------------------------------------------------------


async def test_a_reaction_on_a_reply_runs_the_command_on_its_thread() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post(root_id="root1")})
    await _reactions(dispatcher, driver).handle_reaction(Reaction("white_check_mark", HUMAN, "ch1", "p1"), ME)
    call = dispatcher.calls[0]
    assert call[0] == "invoke_command" and call[1] == "assistant"
    assert call[2] == "ch1" and call[3] == "root1" and call[4] == "thread"
    assert call[5] == "/done\n> fix the build"
    assert call[6] == HUMAN and call[7] == "roman"
    assert driver.calls == [("get_post", "p1")]  # a reply's channel type is not asked


async def test_a_reaction_on_a_top_level_channel_post_answers_in_a_thread_under_it() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post()}, {"ch1": "O"})
    await _reactions(dispatcher, driver).handle_reaction(Reaction("white_check_mark", HUMAN, "ch1", "p1"), ME)
    call = dispatcher.calls[0]
    assert call[3] == "p1" and call[4] == "thread"


async def test_a_reaction_on_a_top_level_direct_message_runs_as_the_dm_conversation() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post(channel_id="dm1")}, {"dm1": "D"})
    reactions = _reactions(dispatcher, driver)
    await reactions.handle_reaction(Reaction("white_check_mark", HUMAN, "dm1", "p1"), ME)
    call = dispatcher.calls[0]
    assert call[2] == "dm1" and call[3] == "dm1" and call[4] == "dm"
    # The channel's type is asked once, then remembered.
    await reactions.handle_reaction(Reaction("white_check_mark", HUMAN, "dm1", "p1"), ME)
    assert driver.calls.count(("get_channel", "dm1")) == 1


async def test_an_unmapped_emoji_reads_nothing_and_does_nothing() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post()})
    await _reactions(dispatcher, driver).handle_reaction(Reaction("tada", HUMAN, "ch1", "p1"), ME)
    assert dispatcher.calls == [] and driver.calls == []


async def test_an_agents_reaction_is_not_a_command() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post()})
    reactions = _reactions(dispatcher, driver, directory=Directory({ME, "uid-developer"}))
    await reactions.handle_reaction(Reaction("white_check_mark", ME, "ch1", "p1"), ME)
    await reactions.handle_reaction(Reaction("white_check_mark", "uid-developer", "ch1", "p1"), ME)
    assert dispatcher.calls == [] and driver.calls == []


async def test_a_post_that_cannot_be_read_runs_no_command() -> None:
    # Unlike Slack, the frame names no conversation at all without the post:
    # nothing to run the command in.
    dispatcher = FakeDispatcher()
    await _reactions(dispatcher, FakeDriver({}, post_error=True)).handle_reaction(
        Reaction("white_check_mark", HUMAN, "ch1", "p1"), ME
    )
    assert dispatcher.calls == []


async def test_the_quote_is_cut_at_the_limit() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post(root_id="root1", message="x" * 40)})
    await _reactions(dispatcher, driver, quote_limit=10).handle_reaction(
        Reaction("white_check_mark", HUMAN, "ch1", "p1"), ME
    )
    assert dispatcher.calls[0][5] == "/done\n> " + "x" * 9 + "…"


# --- the gateway hands the frame over --------------------------------------------


async def test_the_gateway_routes_a_reaction_frame_to_the_adapter_and_not_to_the_sink() -> None:
    dispatcher = FakeDispatcher()
    driver = FakeDriver({"p1": _post(root_id="root1")})
    submitted: list = []

    class Sink:
        def submit(self, msg, chat) -> None:
            submitted.append(msg)

    gw = MattermostGateway(
        cast(AsyncTypedDriver, driver), Sink(), FakeChat(),  # type: ignore[arg-type]
        agent="assistant", dispatcher=dispatcher, reaction_commands=ReactionCommands({"white_check_mark": "done"}),
    )
    gw._own_user_id = ME
    assert gw.reactions is not None
    await gw._on_ws_message(json.dumps(_frame()))
    assert dispatcher.calls[0][3] == "root1" and submitted == []


def test_the_gateway_has_reactions_only_with_a_map_and_a_dispatcher() -> None:
    def gateway(**kwargs):
        return MattermostGateway(cast(AsyncTypedDriver, FakeDriver({})), object(), FakeChat(), **kwargs)  # type: ignore[arg-type]

    assert gateway().reactions is None
    assert gateway(reaction_commands=ReactionCommands({"eyes": "summarize"})).reactions is None
    assert gateway(dispatcher=FakeDispatcher()).reactions is None
    assert gateway(dispatcher=FakeDispatcher(), reaction_commands=ReactionCommands()).reactions is None
    assert gateway(dispatcher=FakeDispatcher(), reaction_commands=ReactionCommands({"eyes": "summarize"})).reactions is not None
