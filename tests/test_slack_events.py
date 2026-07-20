"""Slack event normalization -> neutral IncomingMessage (pure, offline)."""

from datetime import datetime, timezone

from crucible.gateways.slack.events import event_to_incoming, should_respond
from crucible.ports.chat.types import KIND_DM, KIND_THREAD

OWN = "UBOT"
OWN_BOT = "BBOT"


def _msg(**over):
    event = {"type": "message", "channel": "C1", "ts": "100.1", "user": "UHUMAN", "text": "hi"}
    event.update(over)
    return event


def test_top_level_channel_post_starts_a_thread() -> None:
    msg = event_to_incoming(_msg(channel_type="channel"), OWN)
    assert msg is not None
    assert msg.kind == KIND_THREAD
    assert msg.conversation_id == "100.1"  # keyed by its own ts
    assert msg.ref.thread_root_id == "100.1"
    assert msg.ref.message_id == "100.1"
    assert msg.is_dm is False


def test_reply_in_thread_uses_thread_key() -> None:
    msg = event_to_incoming(_msg(ts="105.2", thread_ts="100.1", channel_type="channel"), OWN)
    assert msg is not None
    assert msg.kind == KIND_THREAD
    assert msg.conversation_id == "100.1"  # the parent thread wins
    assert msg.ref.thread_root_id == "100.1"
    assert msg.ref.message_id == "105.2"  # reactions target this message


def test_top_level_dm_is_dm_session() -> None:
    msg = event_to_incoming(_msg(channel_type="im"), OWN)
    assert msg is not None
    assert msg.kind == KIND_DM
    assert msg.is_dm is True
    assert msg.conversation_id == "C1"  # the DM channel
    assert msg.ref.thread_root_id == ""  # reply top-level


def test_mention_detected_in_channel() -> None:
    plain = event_to_incoming(_msg(channel_type="channel", text="hello there"), OWN)
    assert plain is not None and plain.mentioned is False
    at = event_to_incoming(_msg(channel_type="channel", text=f"<@{OWN}> help"), OWN)
    assert at is not None and at.mentioned is True


def test_own_echo_is_dropped() -> None:
    assert event_to_incoming(_msg(user=OWN), OWN) is None
    assert event_to_incoming(_msg(user="", bot_id=OWN_BOT, subtype="bot_message"), OWN, OWN_BOT) is None


def test_another_bot_is_from_bot_but_not_dropped() -> None:
    msg = event_to_incoming(
        _msg(user="", bot_id="BOTHER", subtype="bot_message", channel_type="channel"), OWN, OWN_BOT
    )
    assert msg is not None
    assert msg.is_from_bot is True
    assert msg.user_id == "BOTHER"


def test_non_conversational_subtype_dropped() -> None:
    assert event_to_incoming(_msg(subtype="message_changed"), OWN) is None
    assert event_to_incoming(_msg(subtype="channel_join"), OWN) is None


def test_missing_channel_dropped() -> None:
    assert event_to_incoming(_msg(channel=""), OWN) is None


def test_message_carries_a_utc_timestamp() -> None:
    # Slack ts is epoch seconds -> a UTC-aware datetime on the message.
    msg = event_to_incoming(_msg(ts="1700000000", channel_type="im"), OWN)
    assert msg is not None
    assert msg.timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)


def test_should_respond_rule() -> None:
    dm = event_to_incoming(_msg(channel_type="im"), OWN)
    assert dm is not None and should_respond(dm) is True
    plain = event_to_incoming(_msg(channel_type="channel", text="hi"), OWN)
    assert plain is not None and should_respond(plain) is False  # channel, no mention
    at = event_to_incoming(_msg(channel_type="channel", text=f"<@{OWN}>"), OWN)
    assert at is not None and should_respond(at) is True
    bot = event_to_incoming(_msg(user="", bot_id="BX", subtype="bot_message", channel_type="im"), OWN)
    assert bot is not None and should_respond(bot) is False  # bots never via base rule
