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
