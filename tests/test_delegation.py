"""Handing a task to another agent: the service posts it in the turn's place
as the asking agent, addressed to the other; the tool asks first."""

from pathlib import Path

import pytest

from crucible.builtin_tools import Delegate
from crucible.interactions.delegation import DELEGATION_HOP, ChatDelegationService
from crucible.interactions.files import ChatFileService
from crucible.ports.chat.delegation import DelegationError
from crucible.ports.chat.directory import AgentInfo, StaticDirectory
from crucible.ports.chat.types import KIND_THREAD, CannotRun, PreviewRow
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools.base import ToolContext, ToolError
from tests.fakes.fake_chat import FakeChat
from tests.fakes.presence import presence_of

AGENTS = StaticDirectory([
    AgentInfo("assistant", "assistant", "knows the people", "corp-assistant", "uid-a", model="corp/private-llm"),
    AgentInfo("coder", "coder", "writes code", "corp-coder", "uid-c", model="astra/large"),
])


def _service(store, chat, *, files=None, places=None) -> ChatDelegationService:
    return ChatDelegationService(presence_of(chat), store, AGENTS, files=files, places=places)


async def test_the_task_is_posted_in_the_thread_as_the_asker_addressed_to_the_other_agent(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        await _service(store, chat).delegate("assistant", record.runtime_session_id, "coder", "Write the migration.")
        (ref, text) = chat.replies[0]
        assert (ref.channel_id, ref.thread_root_id) == ("ch1", "root1")
        assert text == "@corp-coder Write the migration."  # the mention the other's gateway recognizes
        assert chat.reply_hops == [DELEGATION_HOP]  # one hop past the person, for the loop guard
    finally:
        await store.close()


async def test_files_travel_with_the_task_in_one_message(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        spec = tmp_path / "spec.md"
        spec.write_text("# spec")
        files = ChatFileService(presence_of(chat), store, {"assistant": (tmp_path,)}, max_bytes=1024)
        await _service(store, chat, files=files).delegate(
            "assistant", record.runtime_session_id, "coder", "Implement this.", [str(spec)],
        )
        (ref, posted, text) = chat.posted_files[0]
        assert text == "@corp-coder Implement this." and [f.name for f in posted] == ["spec.md"]
        assert chat.replies == []  # one message, not a mention and then the files
        # A file the asker may not send is refused in the model's words.
        with pytest.raises(DelegationError, match="outside the directories"):
            await _service(store, chat, files=files).delegate(
                "assistant", record.runtime_session_id, "coder", "x", ["/etc/passwd"],
            )
        with pytest.raises(DelegationError, match="turned off"):
            await _service(store, chat).delegate("assistant", record.runtime_session_id, "coder", "x", [str(spec)])
    finally:
        await store.close()


async def test_no_such_agent_oneself_and_an_unresolvable_conversation_are_refused(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    chat = FakeChat()
    try:
        record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
        svc = _service(store, chat)
        with pytest.raises(DelegationError, match="no agent named 'nobody'"):
            await svc.delegate("assistant", record.runtime_session_id, "nobody", "x")
        with pytest.raises(DelegationError, match="that is you"):
            await svc.delegate("assistant", record.runtime_session_id, "assistant", "x")
        with pytest.raises(DelegationError, match="could not be resolved"):
            await svc.delegate("assistant", "assistant--never-seen", "coder", "x")
        assert chat.replies == []
    finally:
        await store.close()


# --- the tool ---------------------------------------------------------------------


class _Delegation:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._error = error

    async def delegate(self, agent, runtime_session_id, to, task, paths=()):
        self.calls.append((agent, runtime_session_id, to, task, list(paths)))
        if self._error is not None:
            raise self._error


def _ctx(svc) -> ToolContext:
    return ToolContext(agent_name="assistant", directory=AGENTS, runtime_session_id="assistant--conv", delegation_svc=svc)


async def test_the_card_names_the_agent_its_model_the_task_whole_and_the_files() -> None:
    preview = await Delegate().describe(_ctx(_Delegation()), {
        "agent": "coder", "task": "Write the migration.\nKeep it reversible.", "files": ["/tmp/spec.md", "/tmp/schema.sql"],
    })
    assert preview is not None and not isinstance(preview, CannotRun)
    assert preview.title == "Hand a task to coder" and preview.danger is False
    assert preview.rows == (
        PreviewRow("Agent", "coder — writes code"),
        PreviewRow("Model", "astra/large"),
        PreviewRow("Task", "Write the migration.\nKeep it reversible.", passage=True),
        PreviewRow("Files", "spec.md, schema.sql"),
    )
    # Nothing to ask about: no such agent, or oneself.
    assert isinstance(await Delegate().describe(_ctx(_Delegation()), {"agent": "nobody", "task": "x"}), CannotRun)
    assert isinstance(await Delegate().describe(_ctx(_Delegation()), {"agent": "assistant", "task": "x"}), CannotRun)
    assert Delegate().caption({"agent": "coder"}) == "Handing a task to coder"


async def test_the_tool_hands_the_task_to_the_service_and_reports_its_refusal() -> None:
    svc = _Delegation()
    result = await Delegate().execute(_ctx(svc), {"agent": "coder", "task": "Write it.", "files": ["/tmp/a.md"]})
    assert svc.calls == [("assistant", "assistant--conv", "coder", "Write it.", ["/tmp/a.md"])]
    assert result["handed_to"] == "coder" and "message from it" in result["note"]
    with pytest.raises(ToolError, match="no agent named"):
        await Delegate().execute(_ctx(_Delegation(DelegationError("no agent named 'x'"))), {"agent": "x", "task": "y"})
    with pytest.raises(ToolError, match="list of absolute paths"):
        await Delegate().execute(_ctx(svc), {"agent": "coder", "task": "y", "files": "/tmp/a"})
    with pytest.raises(ToolError, match="not available in this deployment"):
        await Delegate().execute(_ctx(None), {"agent": "coder", "task": "y"})
    assert Delegate.requires_confirmation and Delegate.posts_to_user and not Delegate.destructive
