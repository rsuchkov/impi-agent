"""GatewayFactory (gateways/factory.py): the seam between a neutral
GatewayConfig and the gateway it builds — what the config says reaches the
gateway, offline (nothing connects at construction)."""

import pytest

from crucible.gateways.factory import GatewayConfig, GatewayFactory
from crucible.gateways.mattermost import MattermostGateway
from crucible.gateways.reactions import ReactionCommands
from crucible.gateways.slack import SlackGateway
from crucible.loopguard import LoopGuard
from tests.fakes.dispatch import FakeDispatcher, FakeSink


class Directory:
    def agent_user_ids(self):
        return frozenset()

    def list_agents(self):
        return []


def _factory(dispatcher=None) -> GatewayFactory:
    return GatewayFactory(directory=Directory(), loop_guard=LoopGuard(), dispatcher=dispatcher)  # type: ignore[arg-type]


REACTIONS = ReactionCommands({"white_check_mark": "done"})


@pytest.mark.parametrize("with_dispatcher", [True, False])
def test_the_reaction_map_reaches_the_mattermost_gateway(with_dispatcher: bool) -> None:
    factory = _factory(FakeDispatcher() if with_dispatcher else None)
    handle = factory.create("assistant", GatewayConfig(
        kind="mattermost", mattermost_url="http://mm.example.com", mm_token="tok",
        reaction_commands=REACTIONS,
    ))
    assert handle is not None
    gateway = handle.create_gateway(FakeSink())
    assert isinstance(gateway, MattermostGateway)
    # With a dispatcher the map is live; without one there is nobody to run a
    # command, as for a shortcut.
    assert (gateway.reactions is not None) is with_dispatcher


@pytest.mark.parametrize("with_dispatcher", [True, False])
async def test_the_reaction_map_reaches_the_slack_gateway(with_dispatcher: bool) -> None:
    factory = _factory(FakeDispatcher() if with_dispatcher else None)
    handle = factory.create("assistant", GatewayConfig(
        kind="slack", slack_bot_token="xoxb-fake", slack_app_token="xapp-fake",
        reaction_commands=REACTIONS,
    ))
    assert handle is not None
    gateway = handle.create_gateway(FakeSink())
    try:
        assert isinstance(gateway, SlackGateway)
        assert (gateway.reactions is not None) is with_dispatcher
        assert gateway._dm_thread is False  # answered at the top unless the config says thread
    finally:
        await gateway.stop()  # close the socket handler's aiohttp session


async def test_an_applications_own_slack_app_and_home_reach_the_gateway() -> None:
    from slack_bolt.async_app import AsyncApp

    class Home:
        async def opened(self, user_id: str, tab: str, publish) -> None:
            return None

        async def context_changed(self, user_id: str, context: dict) -> None:
            pass

    own, home = AsyncApp(token="xoxb-fake", signing_secret="x" * 16), Home()
    factory = GatewayFactory(
        directory=Directory(), loop_guard=LoopGuard(), dispatcher=FakeDispatcher(),  # type: ignore[arg-type]
        slack_apps={"assistant": own}, slack_home=home,
    )
    handle = factory.create("assistant", GatewayConfig(kind="slack", slack_bot_token="xoxb-other", slack_app_token="xapp-fake"))
    assert handle is not None
    gateway = handle.create_gateway(FakeSink())
    try:
        assert isinstance(gateway, SlackGateway)
        assert gateway._app is own and gateway._home is home  # the application's app, not one built from the token
        other = factory.create("scribe", GatewayConfig(kind="slack", slack_bot_token="xoxb-scribe", slack_app_token="xapp-fake"))
        assert other is not None
        built = other.create_gateway(FakeSink())
        try:
            assert isinstance(built, SlackGateway) and built._app is not own  # no app of its own: built here
        finally:
            await built.stop()
    finally:
        await gateway.stop()


async def test_where_a_direct_message_is_answered_reaches_both_gateways_and_the_slack_client_knows_its_sessions() -> None:
    factory = _factory(FakeDispatcher())
    mm = factory.create("assistant", GatewayConfig(
        kind="mattermost", mattermost_url="http://mm.example.com", mm_token="tok", dm_replies="thread",
    ))
    assert mm is not None
    mm_gateway = mm.create_gateway(FakeSink())
    assert isinstance(mm_gateway, MattermostGateway) and mm_gateway._dm_thread is True
    slack = factory.create("assistant", GatewayConfig(
        kind="slack", slack_bot_token="xoxb-fake", slack_app_token="xapp-fake", dm_replies="thread", slack_agent_sessions=True,
    ))
    assert slack is not None
    gateway = slack.create_gateway(FakeSink())
    try:
        assert isinstance(gateway, SlackGateway) and gateway._dm_thread is True
        assert slack.chat._agent_sessions is True  # type: ignore[attr-defined]  # the client marks the session
    finally:
        await gateway.stop()


def test_no_other_agent_reads_an_http_or_ws_conversation_so_a_task_cannot_be_handed_on_there() -> None:
    from crucible.gateways.http import HttpHub, TokenCallers
    from crucible.gateways.ws import WsHub
    from crucible.tools.base import CAP_DELEGATION

    ws = WsHub("127.0.0.1", 8498, {"one": ("tok", None)}, directory=Directory())  # type: ignore[arg-type]
    http = HttpHub("127.0.0.1", 8499, TokenCallers({"portal": ("tok", None)}), directory=Directory())  # type: ignore[arg-type]
    factory = GatewayFactory(directory=Directory(), loop_guard=LoopGuard(), dispatcher=None, ws_hub=ws, http_hub=http)  # type: ignore[arg-type]
    for kind in ("ws", "http"):
        handle = factory.create("assistant", GatewayConfig(kind=kind))
        assert handle is not None and CAP_DELEGATION in handle.denied_caps, kind
    chat = factory.create("assistant", GatewayConfig(kind="mattermost", mattermost_url="http://mm", mm_token="t"))
    assert chat is not None and CAP_DELEGATION not in chat.denied_caps
