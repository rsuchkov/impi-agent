"""StaticDirectory: the AgentDirectory for a roster fixed at composition."""

from crucible.ports.chat.directory import AgentDirectory, AgentInfo, StaticDirectory


def test_static_directory_serves_what_it_was_given() -> None:
    one = AgentInfo("one", "r", "d", "one-bot", "U1")
    headless = AgentInfo("two", "r", "d", "", "")  # no platform account at all
    directory: AgentDirectory = StaticDirectory([one, headless])

    assert directory.list_agents() == [one, headless]
    assert directory.agent_user_ids() == frozenset({"U1"})  # an empty id is not an id
