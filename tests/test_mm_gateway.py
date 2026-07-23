"""Gateway dispatch decisions: channel residency + base rules."""

from typing import cast

from mattermostautodriver import AsyncTypedDriver

from crucible.gateways.mattermost.gateway import MattermostGateway
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)
from tests.fakes.fake_chat import FakeChat

ME = "uid-assistant"
OTHER_AGENT = "uid-developer"
HUMAN = "uid-roman"


class FakeDirectory:
    def __init__(self, ids: set[str]) -> None:
        self._ids = frozenset(ids)

    def agent_user_ids(self) -> frozenset[str]:
        return self._ids

    def list_agents(self):
        return []


class FakeDriver:
    """Only the surface the gateway touches: channel members."""

    def __init__(self, members: dict[str, list[str]]) -> None:
        driver = self

        class Channels:
            async def get_channel_members(self, channel_id, per_page=200):
                return [{"user_id": uid} for uid in driver._members.get(channel_id, [])]

        self._members = members
        self.channels = Channels()


class SinkSpy:
    def __init__(self) -> None:
        self.submitted: list[IncomingMessage] = []

    def submit(self, msg, chat) -> None:
        self.submitted.append(msg)


def _gateway(
    members: dict[str, list[str]],
    agent_ids: set[str],
    *,
    loop_guard=None,
    reply_to_agents: bool = True,
) -> MattermostGateway:
    gw = MattermostGateway(
        cast(AsyncTypedDriver, FakeDriver(members)),
        SinkSpy(),
        FakeChat(),
        directory=FakeDirectory(agent_ids),
        loop_guard=loop_guard,
        reply_to_agents=reply_to_agents,
    )
    gw._own_user_id = ME
    return gw


def _agent_msg(
    *, author: str, mentioned: bool, hop_depth: int = 0
) -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(
            channel_id="ch1", conversation_id="root1", message_id="p9", thread_root_id="root1"
        ),
        text="hi",
        user_id=author,
        kind=KIND_THREAD,
        mentioned=mentioned,
        is_from_bot=True,
        hop_depth=hop_depth,
    )


def _channel_msg(*, mentioned: bool = False, top_level: bool = True) -> IncomingMessage:
    ref = ConversationRef(
        channel_id="ch1",
        conversation_id="p1" if top_level else "root1",
        message_id="p1" if top_level else "p2",
        thread_root_id="p1" if top_level else "root1",
    )
    return IncomingMessage(
        ref=ref, text="hi", user_id=HUMAN, kind=KIND_THREAD, mentioned=mentioned
    )


async def test_sole_resident_answers_top_level_as_channel_session() -> None:
    gw = _gateway({"ch1": [HUMAN, ME]}, {ME, OTHER_AGENT})

    decided = await gw._decide(_channel_msg())

    assert decided is not None
    assert decided.kind == KIND_CHANNEL
    assert decided.conversation_id == "ch1"  # the channel is the session
    assert decided.ref.thread_root_id == ""  # replies go top-level


async def test_sole_resident_answers_thread_replies_without_mention() -> None:
    gw = _gateway({"ch1": [HUMAN, ME]}, {ME, OTHER_AGENT})

    decided = await gw._decide(_channel_msg(top_level=False))

    assert decided is not None
    assert decided.kind == KIND_THREAD  # thread still wins inside threads
    assert decided.conversation_id == "root1"


async def test_multi_agent_channel_requires_mention() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT})

    assert await gw._decide(_channel_msg()) is None
    decided = await gw._decide(_channel_msg(mentioned=True))
    assert decided is not None
    assert decided.kind == KIND_THREAD  # mention flow: reply in a thread


async def test_dm_always_answered_even_with_directory() -> None:
    gw = _gateway({}, {ME})
    msg = IncomingMessage(
        ref=ConversationRef(
            channel_id="dm1", conversation_id="dm1", message_id="p1", thread_root_id=""
        ),
        text="hi",
        user_id=HUMAN,
        kind=KIND_DM,
        is_dm=True,
    )

    decided = await gw._decide(msg)
    assert decided is msg


async def test_agent_post_with_mention_is_answered() -> None:
    from crucible.loopguard import LoopGuard

    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, loop_guard=LoopGuard())

    decided = await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True))
    assert decided is not None
    assert decided.is_from_bot


async def test_agent_post_without_mention_is_ignored() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT})
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=False)) is None


async def test_foreign_bot_is_never_answered() -> None:
    # A bot that is not in our registry, even mentioning us.
    gw = _gateway({"ch1": [HUMAN, ME, "uid-foreign"]}, {ME, OTHER_AGENT})
    assert await gw._decide(_agent_msg(author="uid-foreign", mentioned=True)) is None


async def test_agent_replies_disabled_drops_agent_posts() -> None:
    gw = _gateway({"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, reply_to_agents=False)
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True)) is None


async def test_hop_cap_stops_agent_cascade() -> None:
    from crucible.loopguard import LoopGuard

    gw = _gateway(
        {"ch1": [HUMAN, ME, OTHER_AGENT]}, {ME, OTHER_AGENT}, loop_guard=LoopGuard(max_hops=4)
    )
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True, hop_depth=3)) is not None
    assert await gw._decide(_agent_msg(author=OTHER_AGENT, mentioned=True, hop_depth=4)) is None


async def test_members_lookup_failure_falls_back_to_mention_rule() -> None:
    class ExplodingDriver(FakeDriver):
        def __init__(self):
            super().__init__({})

            class Channels:
                async def get_channel_members(self, channel_id, per_page=200):
                    raise RuntimeError("api down")

            self.channels = Channels()

    gw = MattermostGateway(
        cast(AsyncTypedDriver, ExplodingDriver()),
        SinkSpy(),
        FakeChat(),
        directory=FakeDirectory({ME}),
    )
    gw._own_user_id = ME

    assert await gw._decide(_channel_msg()) is None  # not sole -> mention required
    assert await gw._decide(_channel_msg(mentioned=True)) is not None
