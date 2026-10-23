"""An agent's presence, by gateway."""

from crucible.interactions import AgentSink, MappingPresence
from tests.fakes.fake_chat import FakeChat


class _Sink:
    def submit(self, msg, chat) -> None:
        pass


def test_a_sink_answers_with_the_gateways_chat_or_the_defaults() -> None:
    mm, slack = FakeChat(), FakeChat()
    sink = AgentSink(sink=_Sink(), gateways={"mattermost": mm, "slack": slack}, default="mattermost")
    assert sink.chat is mm
    assert sink.chat_for("slack") is slack
    assert sink.chat_for("mattermost") is mm
    assert sink.chat_for("") is mm and sink.chat_for("http") is mm  # none named, or none such: the default

    presence = MappingPresence({"assistant": sink})
    assert presence.poster("assistant") is mm
    assert presence.poster("assistant", "slack") is slack
    assert presence.poster("nobody", "slack") is None
