"""Conversation keys: the platform's id, or a shared conversation per person."""

from crucible.ports.chat.keying import AuthorKey, PlatformKey
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)


def _msg(conversation: str, kind: str, user_id: str, *, is_dm: bool = False) -> IncomingMessage:
    return IncomingMessage(
        ref=ConversationRef(channel_id="C1", conversation_id=conversation, message_id="m1", thread_root_id=conversation if kind == KIND_THREAD else ""),
        text="hi", user_id=user_id, kind=kind, is_dm=is_dm, gateway="slack",
    )


def test_the_platform_key_is_the_platforms_id_whoever_speaks() -> None:
    assert PlatformKey().key("assistant", _msg("1.5", KIND_THREAD, "U1")) == "1.5"
    assert PlatformKey().key("assistant", _msg("1.5", KIND_THREAD, "U2")) == "1.5"


def test_the_author_key_gives_each_person_a_shared_conversation_of_their_own() -> None:
    key = AuthorKey()
    # A thread in a channel, a channel: the person's own.
    assert key.key("assistant", _msg("1.5", KIND_THREAD, "U1")) == "1.5:U1"
    assert key.key("assistant", _msg("1.5", KIND_THREAD, "U2")) == "1.5:U2"
    assert key.key("assistant", _msg("C1", KIND_CHANNEL, "U1")) == "C1:U1"
    # A direct message, and a thread answering one, is the person's already.
    assert key.key("assistant", _msg("D1", KIND_DM, "U1", is_dm=True)) == "D1"
    assert key.key("assistant", _msg("D1", KIND_DM, "U1")) == "D1"
    # Nobody's message — a timer's — keeps the platform's id.
    assert key.key("assistant", _msg("1.5", KIND_THREAD, "")) == "1.5"
