"""Which files directory belongs to the conversation a runtime session serves."""

from pathlib import Path

from crucible.attachments import AttachmentStore
from crucible.conversation_files import conversation_files_dir_lookup
from crucible.ports.chat.types import KIND_DM
from crucible.store.sqlite import SqliteSessionStore


async def test_a_session_maps_to_its_own_conversations_directory_and_nobody_elses(tmp_path: Path) -> None:
    attachments = AttachmentStore(tmp_path / "files", max_bytes=1024, retention_days=1)
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        # An id the store has to rename on disk: the lookup gives the name it
        # chose, which a caller deriving it would get wrong.
        record, _ = await store.get_or_create("assistant", "realm:dialog", "realm:dialog", KIND_DM)
        lookup = conversation_files_dir_lookup(attachments, store)

        found = await lookup("assistant", record.runtime_session_id)
        assert found == attachments.conversation_dir("assistant", "realm:dialog")
        assert found is not None and found.parent == tmp_path / "files" / "assistant"
        # Another agent naming this session, or a session nobody knows: nothing.
        assert await lookup("intruder", record.runtime_session_id) is None
        assert await lookup("assistant", "assistant--nobody") is None
        # Without attachments there is no such directory.
        assert await conversation_files_dir_lookup(None, store)("assistant", record.runtime_session_id) is None
    finally:
        await store.close()



async def test_one_lookup_says_where_a_conversation_happens_and_for_whom(tmp_path: Path) -> None:
    from crucible.conversation_files import conversation_lookup

    attachments = AttachmentStore(tmp_path / "files", max_bytes=1024, retention_days=1)
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "ch-9", "c1", KIND_DM, user_id="u-3")
        found = await conversation_lookup(attachments, store)("assistant", record.runtime_session_id)
        assert found is not None
        assert (found.channel_id, found.user_id) == ("ch-9", "u-3")
        assert found.files_dir == attachments.conversation_dir("assistant", "c1")
        # Another agent naming this session learns nothing about it.
        assert await conversation_lookup(attachments, store)("intruder", record.runtime_session_id) is None
        without = await conversation_lookup(None, store)("assistant", record.runtime_session_id)
        assert without is not None and without.files_dir is None and without.channel_id == "ch-9"
    finally:
        await store.close()


async def test_the_lookup_says_what_kind_of_conversation_it_is(tmp_path: Path) -> None:
    from crucible.conversation_files import conversation_lookup

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "ch-9", "c1", KIND_DM, user_id="u-3")
        found = await conversation_lookup(None, store)("assistant", record.runtime_session_id)
        assert found is not None and found.kind == KIND_DM
    finally:
        await store.close()


async def test_a_conversation_is_addressed_on_the_gateway_asked_for(tmp_path: Path) -> None:
    from crucible.conversation_files import conversation_lookup
    from crucible.interactions.service import conversation_ref
    from crucible.ports.chat.types import KIND_THREAD
    from crucible.store.base import GatewayAddress

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "portal:u1", "task-7", KIND_DM, user_id="u1")
        await store.set_gateway_address("assistant", "task-7", "slack", GatewayAddress("C1", "1700.1", KIND_THREAD))
        await store.get_or_create("assistant", "C1", "task-7", KIND_DM, gateway="slack")
        record = (await store.list())[0]
        on_slack = conversation_ref(record, "slack")
        assert (on_slack.channel_id, on_slack.conversation_id, on_slack.thread_root_id) == ("C1", "1700.1", "1700.1")
        # A gateway the record has no address for, or none asked: the record's own fields, as before.
        plain = conversation_ref(record)
        assert (plain.channel_id, plain.conversation_id, plain.thread_root_id) == ("portal:u1", "task-7", "")
        assert conversation_ref(record, "mattermost") == plain
        found = await conversation_lookup(None, store)("assistant", record.runtime_session_id)
        assert found is not None and (found.gateway, found.gateways) == ("slack", ("slack",))
    finally:
        await store.close()


async def test_the_tool_server_sees_the_place_the_turn_is_in_not_where_the_conversation_began(tmp_path: Path) -> None:
    """One record keyed across gateways began as a direct message on the panel
    and is now a thread on Slack: a tool that acts only in a direct message
    judges the thread, and the channel is the thread's."""
    from crucible.conversation_files import conversation_lookup
    from crucible.ports.chat.types import KIND_THREAD
    from crucible.store.base import GatewayAddress

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "portal:u1", "task-7", KIND_DM, user_id="u1", gateway="http")
        await store.set_gateway_address("assistant", "task-7", "http", GatewayAddress("portal:u1", "portal:task-7", KIND_DM))
        lookup = conversation_lookup(None, store)
        begun = await lookup("assistant", record.runtime_session_id)
        assert begun is not None and (begun.kind, begun.channel_id) == (KIND_DM, "portal:u1")
        await store.set_gateway_address("assistant", "task-7", "slack", GatewayAddress("C1", "1700.1", KIND_THREAD))
        await store.get_or_create("assistant", "C1", "task-7", KIND_THREAD, user_id="U1", gateway="slack")
        now = await lookup("assistant", record.runtime_session_id)
        assert now is not None and (now.kind, now.channel_id, now.gateway) == (KIND_THREAD, "C1", "slack")
        # Back on the panel: a direct message again.
        await store.get_or_create("assistant", "portal:u1", "task-7", KIND_DM, user_id="u1", gateway="http")
        back = await lookup("assistant", record.runtime_session_id)
        assert back is not None and back.kind == KIND_DM
    finally:
        await store.close()


async def test_a_thread_inside_a_direct_message_is_answered_in_the_thread_and_acted_in_as_private(tmp_path: Path) -> None:
    """``kind`` says where the reply goes, ``direct`` who reads it: a thread in
    a direct message with the bot is a thread to answer in and a private place
    to act as the person in. An address recorded before the flag existed is
    direct when its kind says so."""
    from crucible.conversation_files import conversation_lookup
    from crucible.ports.chat.types import KIND_THREAD
    from crucible.store.base import GatewayAddress, SessionRecord, gateway_addresses

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        record, _ = await store.get_or_create("assistant", "D1", "1700.1", KIND_THREAD, user_id="U1", gateway="slack")
        await store.set_gateway_address("assistant", "1700.1", "slack", GatewayAddress("D1", "1700.1", KIND_THREAD, direct=True))
        lookup = conversation_lookup(None, store)
        place = await lookup("assistant", record.runtime_session_id)
        assert place is not None and (place.kind, place.direct) == (KIND_THREAD, True)
        # A shared thread stays shared; a record without an address: by its kind.
        await store.set_gateway_address("assistant", "1700.1", "slack", GatewayAddress("C1", "1700.1", KIND_THREAD))
        shared = await lookup("assistant", record.runtime_session_id)
        assert shared is not None and (shared.kind, shared.direct) == (KIND_THREAD, False)
        old = SessionRecord(agent="a", channel_id="c", conversation_id="x", kind=KIND_DM, runtime_session_id="r", created_at="t", last_active="t",
                            gateways='{"slack": {"channel_id": "D1", "conversation_id": "D1", "kind": "dm"}}')
        assert gateway_addresses(old)["slack"].direct is True
        bare = await conversation_lookup(None, store)("assistant", (await store.get_or_create("assistant", "D9", "D9", KIND_DM))[0].runtime_session_id)
        assert bare is not None and bare.direct is True
    finally:
        await store.close()
