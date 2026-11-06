"""Handing a task to another agent through the engine: a turn of that agent
on the task alone, its answer back as a turn of the asker's; the tools ask,
ask after and call off."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from crucible.builtin_tools import CancelDelegation, Delegate, DelegationStatus
from crucible.gateways.delegation import ANSWER_MAX, DelegationChatClient
from crucible.interactions import AgentSink, MappingPresence
from crucible.interactions.delegation import (
    DELEGATION_HOP,
    DELEGATION_MAX_DEPTH,
    NOTICE_ENGINE_RESTARTED,
    EngineDelegationService,
)
from crucible.ports.chat.directory import AgentInfo, StaticDirectory
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.gateway import GATEWAY_DELEGATION, GATEWAY_SLACK
from crucible.ports.chat.types import (
    KIND_DM,
    KIND_THREAD,
    CannotRun,
    ConversationRef,
    OutgoingFile,
    PreviewRow,
)
from crucible.ports.delegation import (
    DELEGATED_BY_ATTRIBUTE,
    DELEGATION_ANSWERED,
    DELEGATION_CANCELLED,
    DELEGATION_FAILED,
    DELEGATION_RUNNING,
    DELEGATION_TASK_ATTRIBUTE,
    DELEGATION_TIMED_OUT,
    ON_BEHALF_OF_GATEWAY_ATTRIBUTE,
    DelegationError,
    DelegationView,
    FilePreview,
    delegated_conversation,
)
from crucible.ports.engine_turn import ConversationBusy, DispatchError, TurnRequest
from crucible.ports.turn import TurnScope
from crucible.store.base import DelegationRecord
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools.base import ToolContext, ToolError
from crucible.turns import TurnRegistry

AGENTS = StaticDirectory([
    AgentInfo("assistant", "assistant", "knows the people", "corp-assistant", "uid-a", model="corp/private-llm"),
    AgentInfo("coder", "coder", "writes code", "", "", model="astra/large"),
])


class ScriptedSink:
    """The asked agent's sink: hands each task's message to a script that plays
    the turn — posts through the chat it is given, answers an outcome."""

    def __init__(self, script=None) -> None:
        self.submitted: list = []  # (message, chat)
        self.stopped: list[str] = []
        self._script = script
        self.started = asyncio.Event()  # set when a turn is handed in

    def submit(self, msg, chat) -> None:
        self.submitted.append((msg, chat))

    def submit_tracked(self, msg, chat):
        self.submitted.append((msg, chat))
        self.started.set()
        future: asyncio.Future = asyncio.get_running_loop().create_future()

        async def play() -> None:
            try:
                outcome = await self._script(msg, chat) if self._script is not None else TurnOutcome.REPLIED
            except BaseException as exc:  # noqa: BLE001 - the test's script decides
                future.set_exception(exc)
            else:
                future.set_result(outcome)

        asyncio.ensure_future(play())
        return future

    async def stop(self, conversation_id: str, *, gateway: str = "") -> bool:
        self.stopped.append(conversation_id)
        return True


class FakeTurnDispatcher:
    def __init__(self, *, busy_times: int = 0, error: Exception | None = None) -> None:
        self.requests: list[TurnRequest] = []
        self.busy_times = busy_times
        self._error = error

    async def run_turn(self, request: TurnRequest) -> TurnOutcome:
        if self.busy_times:
            self.busy_times -= 1
            raise ConversationBusy("busy")
        if self._error is not None:
            raise self._error
        self.requests.append(request)
        return TurnOutcome.REPLIED


def _presence(sink) -> MappingPresence:
    return MappingPresence({"coder": AgentSink(sink=sink, gateways={GATEWAY_DELEGATION: DelegationChatClient()}, default=GATEWAY_DELEGATION)})


async def _said(text: str):
    async def script(msg, chat) -> TurnOutcome:
        await chat.post_reply(msg.ref, text)
        return TurnOutcome.REPLIED
    return script


async def _settled(svc: EngineDelegationService) -> None:
    """Wait for the background work the service spawned to end."""
    for _ in range(200):
        if not svc._running:  # noqa: SLF001 - the test waits on the service's own set
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the service's background work did not end")


@pytest.fixture
async def store(tmp_path: Path):
    opened = SqliteSessionStore(tmp_path / "db.sqlite")
    try:
        yield opened
    finally:
        await opened.close()


async def test_the_task_opens_the_other_agents_turn_alone_and_its_answer_comes_back_as_a_turn_of_the_askers(
    store, monkeypatch,
) -> None:
    sink = ScriptedSink(await _said("Done: the migration is in `0042_add_index.py`."))
    dispatcher = FakeTurnDispatcher()
    turns = TurnRegistry()
    svc = EngineDelegationService(_presence(sink), store, AGENTS, store, dispatcher, DelegationChatClient, turns=turns)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u-alice", gateway=GATEWAY_SLACK)
    async with turns.bind(record.runtime_session_id, TurnScope(turn_id="t1", attributes={"realm": "acme"})):
        task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "Write the migration.")
    assert len(task_id) == 12
    await _settled(svc)

    # The asked agent's turn: a message of the engine's, in a conversation
    # that exists for this task alone, run as the person's.
    (message, chat), = sink.submitted
    key = delegated_conversation(task_id)
    assert message.ref == ConversationRef(channel_id=key, conversation_id=key, message_id=task_id, thread_root_id="")
    assert (message.text, message.kind, message.is_dm, message.gateway) == ("Write the migration.", KIND_DM, True, GATEWAY_DELEGATION)
    assert (message.username, message.is_from_bot, message.user_id) == ("assistant", True, "u-alice")
    assert message.hop_depth == DELEGATION_HOP and message.synthetic and message.engine_started and message.mentioned
    assert message.turn is not None and message.turn.attributes == {
        "realm": "acme",  # the asker's own facts travel with the task
        DELEGATED_BY_ATTRIBUTE: "assistant", DELEGATION_TASK_ATTRIBUTE: task_id, ON_BEHALF_OF_GATEWAY_ATTRIBUTE: GATEWAY_SLACK,
    }
    assert isinstance(chat, DelegationChatClient) and message.attachments == ()

    # The record, as it ended.
    kept = await store.get_delegation(task_id)
    assert kept is not None
    assert (kept.agent, kept.to, kept.status, kept.user_id, kept.gateway) == ("assistant", "coder", DELEGATION_ANSWERED, "u-alice", GATEWAY_SLACK)
    assert (kept.channel_id, kept.conversation_id, kept.kind) == ("ch1", "root1", KIND_THREAD)
    assert kept.answer == "Done: the migration is in `0042_add_index.py`." and kept.finished_at and kept.delivered_at
    assert json.loads(kept.attributes) == {"realm": "acme"} and kept.hop_depth == DELEGATION_HOP

    # The asker's turn on the answer: in its conversation, as the person's,
    # the answer quoted as the agent's words.
    request, = dispatcher.requests
    assert (request.agent, request.channel_id, request.conversation_id, request.kind) == ("assistant", "ch1", "root1", KIND_THREAD)
    assert (request.user_id, request.username, request.is_from_bot) == ("u-alice", "coder", True)
    assert request.text == f"coder finished task {task_id}" and request.message_id == f"{task_id}-answer"
    assert request.event is not None and request.event.source == "coder (agent)"
    assert request.event.text == "Done: the migration is in `0042_add_index.py`."
    assert request.turn is not None and request.turn.attributes == {"realm": "acme", DELEGATION_TASK_ATTRIBUTE: task_id}


async def test_files_travel_as_the_messages_attachments_under_send_files_rules(store, tmp_path: Path) -> None:
    sink = ScriptedSink()
    spec = tmp_path / "spec.md"
    spec.write_text("# spec")
    roots = {"assistant": (tmp_path.resolve(),)}
    svc = EngineDelegationService(_presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient, file_roots=roots, max_file_bytes=1024)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "Implement this.", [str(spec)])
    await _settled(svc)
    (message, _), = sink.submitted
    attachment, = message.attachments
    assert (attachment.name, attachment.path, attachment.mime, attachment.size) == ("spec.md", str(spec.resolve()), "text/markdown", 6)
    kept = await store.get_delegation(task_id)
    assert kept is not None and json.loads(kept.files) == ["spec.md"]
    # A file the asker may not send is refused in the model's words, before anything starts.
    with pytest.raises(DelegationError, match="outside the directories"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "x", ["/etc/passwd"])
    with pytest.raises(DelegationError, match="no such file"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "x", [str(tmp_path / "missing.md")])
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(DelegationError, match="over the 1024-byte limit"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "x", [str(big)])
    with pytest.raises(DelegationError, match="at most 5 files"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "x", [str(spec)] * 6)
    off = EngineDelegationService(_presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient)
    with pytest.raises(DelegationError, match="turned off"):
        await off.delegate("assistant", record.runtime_session_id, "coder", "x", [str(spec)])
    assert len(sink.submitted) == 1


async def test_the_files_are_previewed_as_written_or_by_name_and_size_under_send_files_rules(store, tmp_path: Path) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n\n- one")
    patch = tmp_path / "fix.diff"
    patch.write_text("x" * 7000)
    picture = tmp_path / "shot.png"
    picture.write_bytes(b"PNG\x00\x00\x00\x00\x00\x00\x00")  # decodes, yet holds NUL: not text
    odd = tmp_path / "data.bin"
    odd.write_bytes(b"\xff\xfe\xfd")
    svc = EngineDelegationService(
        _presence(ScriptedSink()), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient,
        file_roots={"assistant": (tmp_path.resolve(),)}, max_file_bytes=10_000,
    )
    previews = await svc.preview_files("assistant", [str(spec), str(patch), str(picture), str(odd)])
    assert previews[0] == FilePreview(name="spec.md", size=13, text="# spec\n\n- one", language="markdown")
    assert (previews[1].name, previews[1].language, previews[1].truncated, len(previews[1].text or "")) == ("fix.diff", "diff", True, 6000)
    assert previews[2] == FilePreview(name="shot.png", size=10)  # NUL bytes: not text, whatever decodes
    assert previews[3] == FilePreview(name="data.bin", size=3)  # not UTF-8
    with pytest.raises(DelegationError, match="outside the directories"):
        await svc.preview_files("assistant", ["/etc/hostname"])
    assert await svc.preview_files("assistant", []) == []


async def test_no_such_agent_oneself_an_unresolvable_conversation_and_an_agent_without_a_sink_are_refused(store) -> None:
    sink = ScriptedSink()
    svc = EngineDelegationService(_presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    with pytest.raises(DelegationError, match="no agent named 'nobody'"):
        await svc.delegate("assistant", record.runtime_session_id, "nobody", "x")
    with pytest.raises(DelegationError, match="that is you"):
        await svc.delegate("assistant", record.runtime_session_id, "assistant", "x")
    with pytest.raises(DelegationError, match="could not be resolved"):
        await svc.delegate("assistant", "assistant--never-seen", "coder", "x")
    # An agent whose sink cannot report a turn's outcome (or that has none) cannot take a task.
    other, _ = await store.get_or_create("coder", "c", "c", KIND_DM)
    with pytest.raises(DelegationError, match="assistant cannot take a task here"):
        await svc.delegate("coder", other.runtime_session_id, "assistant", "x")
    assert sink.submitted == []


async def test_a_task_handed_on_too_many_times_is_refused(store) -> None:
    sink = ScriptedSink()
    svc = EngineDelegationService(_presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient)
    # A chain: the asker's conversation is itself a task's, three deep already.
    above = DelegationRecord(
        id="abc", agent="assistant", to="coder", channel_id="x", conversation_id="x", kind=KIND_DM,
        gateway=GATEWAY_DELEGATION, task="t", user_id="u", status=DELEGATION_RUNNING, started_at="2026-11-06T10:00:00+00:00",
        hop_depth=DELEGATION_MAX_DEPTH,
    )
    await store.save_delegation(above)
    key = delegated_conversation("abc")
    record, _ = await store.get_or_create("assistant", key, key, KIND_DM, gateway=GATEWAY_DELEGATION)
    with pytest.raises(DelegationError, match="already been handed on 3 times"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "again")
    # Two deep: allowed, and the count travels with the task.
    await store.save_delegation(replace(above, hop_depth=DELEGATION_MAX_DEPTH - 1))
    await svc.delegate("assistant", record.runtime_session_id, "coder", "again")
    await _settled(svc)
    assert sink.submitted[0][0].hop_depth == DELEGATION_MAX_DEPTH


@pytest.mark.parametrize(("outcome", "status", "verb"), [
    (TurnOutcome.TIMEOUT, DELEGATION_TIMED_OUT, "ran out of time on"),
    (TurnOutcome.ERROR, DELEGATION_FAILED, "failed at"),
    (TurnOutcome.NOT_ADMITTED, DELEGATION_FAILED, "failed at"),
    (TurnOutcome.EMPTY, DELEGATION_ANSWERED, "finished"),
])
async def test_how_the_turn_ended_is_how_the_task_ended_and_the_asker_is_told(store, outcome, status, verb) -> None:
    async def script(msg, chat) -> TurnOutcome:
        await chat.post_notice(msg.ref, "the turn ended", code=outcome.name.lower())
        return outcome

    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(_presence(ScriptedSink(script)), store, AGENTS, store, dispatcher, DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1")
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and (kept.status, kept.notice, kept.answer) == (status, outcome.name.lower(), "")
    request, = dispatcher.requests
    assert request.text == f"coder {verb} task {task_id} ({outcome.name.lower().replace('_', ' ')})"
    assert request.event is not None and request.event.text == f"(no answer: the turn ended with {outcome.name.lower()})"


async def test_a_turn_that_raises_ends_the_task_as_failed(store) -> None:
    async def script(msg, chat) -> TurnOutcome:
        raise RuntimeError("the sink broke")

    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(_presence(ScriptedSink(script)), store, AGENTS, store, dispatcher, DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.status == DELEGATION_FAILED and len(dispatcher.requests) == 1


async def test_the_asker_cancels_a_running_task_and_no_answer_comes(store) -> None:
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        await chat.post_reply(msg.ref, "too late")
        return TurnOutcome.REPLIED

    sink = ScriptedSink(script)
    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(_presence(sink), store, AGENTS, store, dispatcher, DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await sink.started.wait()
    view = await svc.status("assistant", record.runtime_session_id, task_id)
    assert view == DelegationView(task_id=task_id, to="coder", status=DELEGATION_RUNNING, started_at=view.started_at)
    assert await svc.cancel("assistant", record.runtime_session_id, task_id) is True
    assert sink.stopped == [delegated_conversation(task_id)]  # the asked agent's turn is told to end
    assert await svc.cancel("assistant", record.runtime_session_id, task_id) is False  # already over
    release.set()
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.status == DELEGATION_CANCELLED and kept.answer == "" and kept.finished_at
    assert dispatcher.requests == []  # what came of it is nobody's
    # A task is the asker's conversation's: another conversation, or another agent, cannot see it.
    other, _ = await store.get_or_create("assistant", "d2", "d2", KIND_DM)
    with pytest.raises(DelegationError, match="no task"):
        await svc.status("assistant", other.runtime_session_id, task_id)
    with pytest.raises(DelegationError, match="no task"):
        await svc.cancel("coder", record.runtime_session_id, task_id)
    with pytest.raises(DelegationError, match="no task 'nope'"):
        await svc.status("assistant", record.runtime_session_id, "nope")


async def test_the_answer_waits_for_a_busy_conversation_and_is_kept_when_it_cannot_be_handed_over(store, monkeypatch) -> None:
    monkeypatch.setattr("crucible.interactions.delegation.ANSWER_RETRY_S", 0.0)
    busy = FakeTurnDispatcher(busy_times=2)
    svc = EngineDelegationService(_presence(ScriptedSink(await _said("ok"))), store, AGENTS, store, busy, DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    assert len(busy.requests) == 1 and busy.busy_times == 0
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.delivered_at
    # Busy past the wait: the record holds the answer, delivered to nobody.
    monkeypatch.setattr("crucible.interactions.delegation.ANSWER_WAIT_S", 0.0)
    never = FakeTurnDispatcher(busy_times=10)
    svc = EngineDelegationService(_presence(ScriptedSink(await _said("ok"))), store, AGENTS, store, never, DelegationChatClient)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.status == DELEGATION_ANSWERED and kept.answer == "ok" and kept.delivered_at == ""
    assert never.requests == []
    # An asker that cannot be reached at all: the same.
    gone = FakeTurnDispatcher(error=DispatchError("agent is not running"))
    svc = EngineDelegationService(_presence(ScriptedSink(await _said("ok"))), store, AGENTS, store, gone, DelegationChatClient)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.answer == "ok" and kept.delivered_at == ""
    view = await svc.status("assistant", record.runtime_session_id, task_id)
    assert view.answer == "ok" and view.status == DELEGATION_ANSWERED  # which is what status is for


async def test_a_restart_ends_what_it_finds_running_and_tells_the_asker(store) -> None:
    running = DelegationRecord(
        id="lost1", agent="assistant", to="coder", channel_id="d1", conversation_id="d1", kind=KIND_DM,
        gateway=GATEWAY_SLACK, task="t", user_id="u1", status=DELEGATION_RUNNING, started_at="2026-11-06T10:00:00+00:00",
    )
    await store.save_delegation(running)
    await store.save_delegation(replace(running, id="done1", status=DELEGATION_ANSWERED, answer="fine"))
    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(_presence(ScriptedSink()), store, AGENTS, store, dispatcher, DelegationChatClient)
    await svc.recover()
    await _settled(svc)
    kept = await store.get_delegation("lost1")
    assert kept is not None and (kept.status, kept.notice) == (DELEGATION_FAILED, NOTICE_ENGINE_RESTARTED) and kept.finished_at
    request, = dispatcher.requests
    assert request.text == "coder failed at task lost1 (engine restarted)" and request.conversation_id == "d1"
    done = await store.get_delegation("done1")
    assert done is not None and done.status == DELEGATION_ANSWERED  # not touched


async def test_closing_the_service_lets_its_background_work_go(store) -> None:
    async def script(msg, chat) -> TurnOutcome:
        await asyncio.sleep(60)
        return TurnOutcome.REPLIED

    svc = EngineDelegationService(_presence(ScriptedSink(script)), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient)
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM)
    await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await svc.aclose()
    assert not svc._running  # noqa: SLF001


async def test_the_task_turns_chat_collects_the_answer_and_has_nobody_to_ask() -> None:
    chat = DelegationChatClient()
    ref = ConversationRef(channel_id="c", conversation_id="c", message_id="m", thread_root_id="")
    await chat.post_reply(ref, "First part.")
    await chat.post_files(ref, [OutgoingFile(name="patch.diff", data=b"x", mime="text/plain")], text="The patch:")
    await chat.post_notice(ref, "truncated", code="truncated")
    assert chat.answer == "First part.\n\nThe patch:\n\nFiles the agent produced (kept in its own conversation): patch.diff"
    assert chat.notice_code == "truncated"
    assert await chat.get_thread_posts(ref) == [] and await chat.get_recent_posts("c") == []
    assert chat.format_mention("coder", user_id="u") == "@coder"
    with pytest.raises(DelegationError, match="no person who could confirm"):
        await chat.post_confirmation(ref, None, [], callback_url="")  # type: ignore[arg-type]
    with pytest.raises(DelegationError, match="no person who could click"):
        await chat.post_actions(ref, "x", [], callback_url="")
    assert await chat.post_cards(ref, [], callback_url="") == ""  # the turn's steps, drawn for nobody
    # The answer is bounded.
    long = DelegationChatClient()
    await long.post_reply(ref, "x" * (ANSWER_MAX + 10))
    assert len(long.answer) == ANSWER_MAX


# --- the tools ---------------------------------------------------------------------


class _Delegation:
    def __init__(self, error: Exception | None = None, *, cancelled: bool = True, previews=()) -> None:
        self.calls: list[tuple] = []
        self._error = error
        self._cancelled = cancelled
        self._previews = list(previews)

    async def preview_files(self, agent, paths):
        self.calls.append(("preview_files", agent, list(paths)))
        if self._error is not None:
            raise self._error
        return self._previews

    async def delegate(self, agent, runtime_session_id, to, task, paths=()):
        self.calls.append(("delegate", agent, runtime_session_id, to, task, list(paths)))
        if self._error is not None:
            raise self._error
        return "task0001"

    async def status(self, agent, runtime_session_id, task_id):
        self.calls.append(("status", agent, runtime_session_id, task_id))
        if self._error is not None:
            raise self._error
        return DelegationView(task_id=task_id, to="coder", status=DELEGATION_ANSWERED, started_at="s", finished_at="f", answer="ok", notice="")

    async def cancel(self, agent, runtime_session_id, task_id):
        self.calls.append(("cancel", agent, runtime_session_id, task_id))
        if self._error is not None:
            raise self._error
        return self._cancelled


def _ctx(svc) -> ToolContext:
    return ToolContext(agent_name="assistant", directory=AGENTS, runtime_session_id="assistant--conv", delegation_svc=svc)


async def test_the_card_names_the_agent_its_model_the_task_whole_and_the_files_as_written() -> None:
    svc = _Delegation(previews=[
        FilePreview(name="spec.md", size=6, text="# spec", language="markdown"),
        FilePreview(name="schema.sql", size=9000, text="CREATE…", language="sql", truncated=True),
        FilePreview(name="shot.png", size=1234),
    ])
    preview = await Delegate().describe(_ctx(svc), {
        "agent": "coder", "task": "Write the migration.\nKeep it reversible.", "files": ["/tmp/spec.md", "/tmp/schema.sql", "/tmp/shot.png"],
    })
    assert preview is not None and not isinstance(preview, CannotRun)
    assert preview.title == "Hand a task to coder" and preview.danger is False
    assert preview.rows == (
        PreviewRow("Agent", "coder — writes code"),
        PreviewRow("Model", "astra/large"),
        PreviewRow("Task", "Write the migration.\nKeep it reversible.", passage=True),
        PreviewRow("Files", "/tmp/spec.md, /tmp/schema.sql, /tmp/shot.png"),
        PreviewRow("Not text", "shot.png (1234 bytes)"),
    )
    # The text files as written, each under its name and size; one language only when they share it.
    assert preview.block_label == "Files as written" and preview.block_language == ""
    assert preview.block == "── spec.md (6 bytes)\n# spec\n\n── schema.sql (9000 bytes, cut at 7 chars)\nCREATE…"
    assert svc.calls == [("preview_files", "assistant", ["/tmp/spec.md", "/tmp/schema.sql", "/tmp/shot.png"])]
    one = _Delegation(previews=[FilePreview(name="a.py", size=1, text="1", language="python")])
    preview = await Delegate().describe(_ctx(one), {"agent": "coder", "task": "x", "files": ["/tmp/a.py"]})
    assert not isinstance(preview, CannotRun) and preview is not None and preview.block_language == "python"
    # Without files there is no block; a file the agent may not send is nothing to ask about.
    preview = await Delegate().describe(_ctx(_Delegation()), {"agent": "coder", "task": "x"})
    assert not isinstance(preview, CannotRun) and preview is not None and preview.block == "" and preview.block_label == ""
    refused = await Delegate().describe(_ctx(_Delegation(DelegationError("/etc/x: outside the directories"))), {"agent": "coder", "task": "x", "files": ["/etc/x"]})
    assert isinstance(refused, CannotRun) and "outside the directories" in refused.reason
    # Nothing to ask about: no such agent, or oneself.
    assert isinstance(await Delegate().describe(_ctx(_Delegation()), {"agent": "nobody", "task": "x"}), CannotRun)
    assert isinstance(await Delegate().describe(_ctx(_Delegation()), {"agent": "assistant", "task": "x"}), CannotRun)
    assert Delegate().caption({"agent": "coder"}) == "Handing a task to coder"


async def test_the_tool_hands_the_task_to_the_service_and_answers_with_the_task_id() -> None:
    svc = _Delegation()
    result = await Delegate().execute(_ctx(svc), {"agent": "coder", "task": "Write it.", "files": ["/tmp/a.md"]})
    assert svc.calls == [("delegate", "assistant", "assistant--conv", "coder", "Write it.", ["/tmp/a.md"])]
    assert result["task_id"] == "task0001" and result["handed_to"] == "coder" and "task task0001" in result["note"]
    with pytest.raises(ToolError, match="no agent named"):
        await Delegate().execute(_ctx(_Delegation(DelegationError("no agent named 'x'"))), {"agent": "x", "task": "y"})
    with pytest.raises(ToolError, match="list of absolute paths"):
        await Delegate().execute(_ctx(svc), {"agent": "coder", "task": "y", "files": "/tmp/a"})
    with pytest.raises(ToolError, match="not available in this deployment"):
        await Delegate().execute(_ctx(None), {"agent": "coder", "task": "y"})
    assert Delegate.requires_confirmation and not Delegate.grantable and not Delegate.destructive
    assert not Delegate.posts_to_user  # nothing is posted where the person reads: the turn goes on to its own reply


async def test_asking_after_and_calling_off_go_by_the_task_id() -> None:
    svc = _Delegation()
    status = await DelegationStatus().execute(_ctx(svc), {"task_id": "task0001"})
    assert status == {
        "task_id": "task0001", "agent": "coder", "status": DELEGATION_ANSWERED,
        "started_at": "s", "finished_at": "f", "answer": "ok",
    }
    assert await CancelDelegation().execute(_ctx(svc), {"task_id": "task0001"}) == {
        "task_id": "task0001", "cancelled": True, "note": "its turn is stopped; no answer will come",
    }
    assert (await CancelDelegation().execute(_ctx(_Delegation(cancelled=False)), {"task_id": "task0001"}))["note"] == "it had already ended"
    assert svc.calls == [("status", "assistant", "assistant--conv", "task0001"), ("cancel", "assistant", "assistant--conv", "task0001")]
    with pytest.raises(ToolError, match="no task"):
        await DelegationStatus().execute(_ctx(_Delegation(DelegationError("no task 'x'"))), {"task_id": "x"})
    with pytest.raises(ToolError, match="missing required string argument 'task_id'"):
        await CancelDelegation().execute(_ctx(svc), {})
    assert DelegationStatus().caption({"task_id": "t1"}) == "Asking after task t1"
    assert CancelDelegation().caption({"task_id": "t1"}) == "Calling task t1 off"
    for tool in (DelegationStatus, CancelDelegation):
        assert not tool.requires_confirmation and not tool.destructive and tool.requires == Delegate.requires
