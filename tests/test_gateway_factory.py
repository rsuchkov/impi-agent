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
    finally:
        await gateway.stop()  # close the socket handler's aiohttp session


async def test_an_applications_own_slack_app_and_home_reach_the_gateway() -> None:
    from slack_bolt.async_app import AsyncApp

    class Home:
        async def opened(self, user_id: str, tab: str):
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
