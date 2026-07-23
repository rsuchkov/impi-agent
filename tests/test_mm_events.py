import json

from crucible.gateways.mattermost.events import PROPS_KEY, parse_posted, should_respond
from crucible.ports.chat.types import KIND_DM, KIND_THREAD

ME = "botuserid0000000000000000"
HUMAN = "humanuserid00000000000000"


def _frame(
    *,
    message: str = "hi",
    channel_type: str = "D",
    channel_id: str = "dmchannel0000000000000000",
    post_id: str = "post00000000000000000001",
    root_id: str = "",
    user_id: str = HUMAN,
    mentions: list[str] | None = None,
    props: dict | None = None,
    post_type: str = "",
    event: str = "posted",
) -> dict:
    post = {
        "id": post_id,
        "create_at": 1783371725003,
        "user_id": user_id,
        "channel_id": channel_id,
        "root_id": root_id,
        "message": message,
        "type": post_type,
        "props": props or {},
    }
    data = {
        "channel_display_name": "test",
        "channel_name": "test",
        "channel_type": channel_type,
        "post": json.dumps(post),  # JSON string inside the frame — as real MM sends it
        "sender_name": "@roman",
        "team_id": "team000000000000000000000",
    }
    if mentions is not None:
        data["mentions"] = json.dumps(mentions)
    return {"event": event, "data": data, "broadcast": {}, "seq": 2}


def test_dm_top_level_is_dm_channel_session() -> None:
    msg = parse_posted(_frame(), ME)

    assert msg is not None
    assert msg.kind == KIND_DM
    assert msg.is_dm
    assert msg.conversation_id == "dmchannel0000000000000000"  # the channel, not the post
    assert msg.ref.thread_root_id == ""  # reply top-level
    assert msg.ref.message_id == "post00000000000000000001"
    assert msg.username == "roman"
    assert should_respond(msg)  # DMs are always answered, no mention needed


def test_dm_thread_wins_over_dm_channel_session() -> None:
    # A thread in a DM is a full separate session (decision of 2026-07-06).
    msg = parse_posted(_frame(root_id="dmroot000000000000000001"), ME)

    assert msg is not None
    assert msg.kind == KIND_THREAD
    assert msg.conversation_id == "dmroot000000000000000001"
    assert msg.ref.thread_root_id == "dmroot000000000000000001"
    assert should_respond(msg)


def test_channel_mention_top_level_starts_thread() -> None:
    msg = parse_posted(
        _frame(channel_type="O", channel_id="town0000000000000000000", mentions=[ME]),
        ME,
    )

    assert msg is not None
    assert msg.kind == KIND_THREAD
    assert not msg.is_dm
    assert msg.conversation_id == "post00000000000000000001"  # new thread off the post
    assert msg.ref.thread_root_id == "post00000000000000000001"
    assert msg.mentioned
    assert should_respond(msg)


def test_channel_reply_in_thread_keys_by_root() -> None:
    msg = parse_posted(
        _frame(
            channel_type="O",
            root_id="root00000000000000000001",
            post_id="post00000000000000000002",
            mentions=[ME],
        ),
        ME,
    )

    assert msg is not None
    assert msg.conversation_id == "root00000000000000000001"
    assert msg.ref.thread_root_id == "root00000000000000000001"
    assert msg.ref.message_id == "post00000000000000000002"


def test_channel_without_mention_is_ignored() -> None:
    msg = parse_posted(_frame(channel_type="O"), ME)

    assert msg is not None
    assert not msg.mentioned
    assert not should_respond(msg)


def test_own_post_is_dropped() -> None:
    assert parse_posted(_frame(user_id=ME), ME) is None


def test_other_bot_post_never_answered_in_stage_1() -> None:
    msg = parse_posted(_frame(props={"from_bot": "true"}, mentions=[ME]), ME)

    assert msg is not None
    assert msg.is_from_bot
    assert not should_respond(msg)


def test_system_post_is_dropped() -> None:
    assert parse_posted(_frame(post_type="system_join_channel"), ME) is None


def test_non_posted_event_is_dropped() -> None:
    assert parse_posted(_frame(event="typing"), ME) is None


def test_unparsable_post_payload_is_dropped() -> None:
    frame = _frame()
    frame["data"]["post"] = "{broken json"
    assert parse_posted(frame, ME) is None


def test_to_channel_session_rewrites_top_level_post() -> None:
    from crucible.gateways.mattermost.events import is_top_level, to_channel_session

    msg = parse_posted(_frame(channel_type="O", mentions=[ME]), ME)
    assert msg is not None
    assert is_top_level(msg)

    rewritten = to_channel_session(msg)
    assert rewritten.kind == "channel"
    assert rewritten.conversation_id == msg.channel_id
    assert rewritten.ref.thread_root_id == ""
    assert rewritten.ref.message_id == msg.ref.message_id

    threaded = parse_posted(_frame(channel_type="O", root_id="root1"), ME)
    assert threaded is not None
    assert not is_top_level(threaded)


def test_hop_depth_parsed_from_props() -> None:
    frame = _frame(props={PROPS_KEY: {"depth": 3}})
    msg = parse_posted(frame, ME)
    assert msg is not None
    assert msg.hop_depth == 3


def test_hop_depth_defaults_to_zero() -> None:
    msg = parse_posted(_frame(), ME)
    assert msg is not None
    assert msg.hop_depth == 0
