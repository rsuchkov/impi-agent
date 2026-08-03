"""Pure frame normalization for the ws gateway."""

from crucible.gateways.ws.events import (
    frame_error,
    frame_to_incoming,
    internal_conversation,
    split_conversation,
)
from crucible.ports.chat.types import KIND_CHANNEL, KIND_DM, KIND_THREAD


def test_conversation_namespacing_round_trips() -> None:
    internal = internal_conversation("probe", "user-42")
    assert internal == "probe:user-42"
    assert split_conversation(internal) == ("probe", "user-42")
    # Client ids may contain the separator themselves — only the first splits.
    assert split_conversation(internal_conversation("s", "a:b")) == ("s", "a:b")


def test_message_frame_maps_to_incoming_with_dm_defaults() -> None:
    msg = frame_to_incoming("probe", {
        "type": "message", "agent": "helper",
        "conversation_id": "user-42", "text": "привет",
        "user_id": "u1", "username": "vasya",
    })
    assert msg.conversation_id == "probe:user-42"
    assert msg.channel_id == "probe:user-42"
    assert msg.ref.message_id.startswith("probe:")
    assert msg.text == "привет"
    assert msg.kind == KIND_DM and msg.is_dm and msg.mentioned
    assert msg.username == "vasya" and not msg.is_from_bot


def test_kinds_map_and_default() -> None:
    base = {"agent": "a", "conversation_id": "c", "text": "t"}
    assert frame_to_incoming("s", base).kind == KIND_DM
    assert frame_to_incoming("s", {**base, "kind": "thread"}).kind == KIND_THREAD
    assert frame_to_incoming("s", {**base, "kind": "channel"}).kind == KIND_CHANNEL


def test_client_message_id_is_namespaced_for_dedupe() -> None:
    msg = frame_to_incoming("probe", {
        "agent": "a", "conversation_id": "c", "text": "t", "message_id": "m-1",
    })
    assert msg.ref.message_id == "probe:m-1"
    # Absent id -> generated, still namespaced, unique per frame.
    a = frame_to_incoming("probe", {"agent": "a", "conversation_id": "c", "text": "t"})
    b = frame_to_incoming("probe", {"agent": "a", "conversation_id": "c", "text": "t"})
    assert a.ref.message_id != b.ref.message_id


def test_frame_error_names_the_broken_field() -> None:
    assert frame_error({"agent": "a", "conversation_id": "c", "text": "t"}) is None
    assert "agent" in str(frame_error({"conversation_id": "c", "text": "t"}))
    assert "conversation_id" in str(frame_error({"agent": "a", "text": "t"}))
    assert "text" in str(frame_error({"agent": "a", "conversation_id": "c", "text": "  "}))
    assert "kind" in str(frame_error(
        {"agent": "a", "conversation_id": "c", "text": "t", "kind": "carrier-pigeon"}
    ))
