"""Handing a task to another agent through the engine: a turn of that agent
on the task alone, its answer back as a turn of the asker's; the tools ask,
ask after and call off."""

import asyncio
import contextlib
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from crucible.attachments import AttachmentStore
from crucible.builtin_tools import (
    AskAgent,
    AwaitAgents,
    CancelDelegation,
    Delegate,
    DelegationStatus,
)
from crucible.gateways.delegation import (
    ANSWER_MAX,
    DelegationChatClient,
    TrustedDelegationChatClient,
)
from crucible.interactions import AgentSink, MappingPresence
from crucible.interactions.delegation import (
    DELEGATION_HOP,
    NOTICE_ENGINE_RESTARTED,
    NOTICE_UNDELIVERED,
    EngineDelegationService,
    TrustedLinkRegistry,
    TrustedTurnTracer,
)
from crucible.ports.agent.errors import TURN_END_NOTICES
from crucible.ports.agent.events import (
    CallAwaiting,
    TextProgress,
    ToolFinished,
    ToolStarted,
)
from crucible.ports.agent.runtime import RuntimeStats
from crucible.ports.agent.spec import TrustedAgent
from crucible.ports.chat.client import ConversationView, NobodyToAnswer
from crucible.ports.chat.directory import AgentInfo, StaticDirectory
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.gateway import (
    GATEWAY_DELEGATION,
    GATEWAY_HTTP,
    GATEWAY_MATTERMOST,
    GATEWAY_SLACK,
    GATEWAY_TRUSTED_DELEGATION,
)
from crucible.ports.chat.types import (
    KIND_DM,
    KIND_THREAD,
    ApprovalCard,
    CannotRun,
    ConversationRef,
    OutgoingFile,
    PostSnippet,
    PreviewRow,
)
from crucible.ports.delegation import (
    ACCOUNTS_HANDED_ATTRIBUTE,
    DELEGATED_BY_ATTRIBUTE,
    DELEGATED_FROM_TURN_ATTRIBUTE,
    DELEGATION_ANSWERED,
    DELEGATION_CANCELLED,
    DELEGATION_FAILED,
    DELEGATION_RUNNING,
    DELEGATION_TASK_ATTRIBUTE,
    DELEGATION_TIMED_OUT,
    NOTICE_ASKER_TURN_ENDED,
    NOTICE_CONVERSATION_FORGOTTEN,
    ON_BEHALF_OF_GATEWAY_ATTRIBUTE,
    DelegationError,
    DelegationView,
    FilePreview,
    TrustedLink,
    delegated_conversation_key,
    per_conversation_key,
)
from crucible.ports.engine_turn import ConversationBusy, DispatchError, TurnRequest
from crucible.ports.turn import (
    TurnPlace,
    TurnScope,
    TurnSecrets,
)
from crucible.store.base import (
    DelegationRecord,
    GatewayAddress,
    SessionRecord,
    derive_runtime_session_id,
)
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools.base import ToolContext, ToolError
from crucible.turns import TurnRegistry
from tests.fakes.fake_chat import FakeChat

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
    key = delegated_conversation_key(task_id)
    assert message.ref == ConversationRef(channel_id=key, conversation_id=key, message_id=task_id, thread_root_id="")
    assert (message.text, message.kind, message.is_dm, message.gateway) == ("Write the migration.", KIND_DM, True, GATEWAY_DELEGATION)
    assert (message.username, message.is_from_bot, message.user_id) == ("assistant", True, "u-alice")
    assert message.hop_depth == DELEGATION_HOP and message.synthetic and message.engine_started and message.mentioned
    assert message.turn is not None and message.turn.attributes == {
        "realm": "acme",  # the asker's own facts travel with the task
        DELEGATED_BY_ATTRIBUTE: "assistant", DELEGATION_TASK_ATTRIBUTE: task_id, ON_BEHALF_OF_GATEWAY_ATTRIBUTE: GATEWAY_SLACK,
        DELEGATED_FROM_TURN_ATTRIBUTE: "t1",  # the asker's turn: the parent of the task's
    }
    assert isinstance(chat, DelegationChatClient) and message.attachments == ()

    # The record, as it ended.
    kept = await store.get_delegation(task_id)
    assert kept is not None
    assert (kept.agent, kept.to, kept.status, kept.user_id, kept.gateway) == ("assistant", "coder", DELEGATION_ANSWERED, "u-alice", GATEWAY_SLACK)
    assert (kept.channel_id, kept.conversation_id, kept.kind) == ("ch1", "root1", KIND_THREAD)
    assert kept.answer == "Done: the migration is in `0042_add_index.py`." and kept.finished_at and kept.delivered_at
    assert json.loads(kept.attributes) == {"realm": "acme", DELEGATED_FROM_TURN_ATTRIBUTE: "t1"}

    # The asker's turn on the answer: in its conversation, as the person's,
    # the answer quoted as the agent's words.
    request, = dispatcher.requests
    assert (request.agent, request.channel_id, request.conversation_id, request.kind) == ("assistant", "ch1", "root1", KIND_THREAD)
    assert (request.user_id, request.username, request.is_from_bot) == ("u-alice", "coder", True)
    assert request.text == f"coder finished task {task_id}" and request.message_id == f"{task_id}-answer"
    assert request.event is not None and request.event.source == "coder (agent)"
    assert request.event.text == "Done: the migration is in `0042_add_index.py`."
    assert request.turn is not None and request.turn.attributes == {"realm": "acme", DELEGATION_TASK_ATTRIBUTE: task_id}


async def test_files_travel_as_copies_in_the_tasks_own_conversation_held_to_what_the_card_showed(store, tmp_path: Path) -> None:
    sink = ScriptedSink()
    spec = tmp_path / "spec.md"
    spec.write_text("# spec")
    roots = {"assistant": (tmp_path.resolve(),)}
    saver = AttachmentStore(tmp_path / "attachments", max_bytes=1024)
    svc = EngineDelegationService(
        _presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient,
        file_roots=roots, saver=saver, max_file_bytes=1024,
    )
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD)
    shown, = await svc.preview_files("assistant", [str(spec)])
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "Implement this.", [str(spec)], digests={str(spec): shown.digest})
    await _settled(svc)
    (message, _), = sink.submitted
    attachment, = message.attachments
    # A copy in the asked agent's conversation for this task — readable to its process, out of the asker's reach.
    copy = Path(attachment.path)
    assert copy.is_relative_to(saver.conversation_dir("coder", delegated_conversation_key(task_id))) and copy.read_text() == "# spec"
    assert (attachment.name, attachment.mime, attachment.size) == ("spec.md", "text/markdown", 6)
    kept = await store.get_delegation(task_id)
    assert kept is not None and json.loads(kept.files) == [{"name": "spec.md", "sha256": shown.digest}]
    # The asker rewriting its file afterwards changes nothing the asked agent reads.
    spec.write_text("# something else")
    assert copy.read_text() == "# spec"
    # A file that is no longer what the card showed does not go.
    with pytest.raises(DelegationError, match="spec.md changed after the card was shown"):
        await svc.delegate("assistant", record.runtime_session_id, "coder", "x", [str(spec)], digests={str(spec): shown.digest})
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
    off = EngineDelegationService(_presence(sink), store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient, file_roots=roots)
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
        file_roots={"assistant": (tmp_path.resolve(),)}, saver=AttachmentStore(tmp_path / "att", max_bytes=10_000), max_file_bytes=10_000,
    )
    previews = await svc.preview_files("assistant", [str(spec), str(patch), str(picture), str(odd)])
    assert previews[0] == FilePreview(name="spec.md", size=13, text="# spec\n\n- one", language="markdown", digest=previews[0].digest)
    assert len(previews[0].digest) == 64
    assert (previews[1].name, previews[1].language, previews[1].truncated, len(previews[1].text or "")) == ("fix.diff", "diff", True, 6000)
    assert previews[2] == FilePreview(name="shot.png", size=10, digest=previews[2].digest)  # NUL bytes: not text, whatever decodes
    assert previews[3] == FilePreview(name="data.bin", size=3, digest=previews[3].digest)  # not UTF-8
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


@pytest.mark.parametrize(("outcome", "status", "verb"), [
    (TurnOutcome.TIMEOUT, DELEGATION_TIMED_OUT, "ran out of time on"),
    (TurnOutcome.ERROR, DELEGATION_FAILED, "failed at"),
    (TurnOutcome.NOT_ADMITTED, DELEGATION_FAILED, "failed at"),
    (TurnOutcome.EMPTY, DELEGATION_ANSWERED, "finished"),
    (TurnOutcome.STOPPED, DELEGATION_CANCELLED, "ended"),  # stopped by something other than cancel: told as such
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
    assert sink.stopped == [delegated_conversation_key(task_id)]  # the asked agent's turn is told to end
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
    assert kept.notice == NOTICE_UNDELIVERED  # the record says so, for delegation_status
    assert never.requests == []
    # An asker that cannot be reached at all: the same.
    gone = FakeTurnDispatcher(error=DispatchError("agent is not running"))
    svc = EngineDelegationService(_presence(ScriptedSink(await _said("ok"))), store, AGENTS, store, gone, DelegationChatClient)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.answer == "ok" and kept.delivered_at == "" and kept.notice == NOTICE_UNDELIVERED
    view = await svc.status("assistant", record.runtime_session_id, task_id)
    assert view.answer == "ok" and view.status == DELEGATION_ANSWERED  # which is what status is for


async def test_a_restart_ends_what_it_finds_running_and_tells_the_asker(store) -> None:
    running = DelegationRecord(
        id="lost1", agent="assistant", to="coder", channel_id="d1", conversation_id="d1", kind=KIND_DM,
        gateway=GATEWAY_SLACK, task="t", user_id="u1", status=DELEGATION_RUNNING, started_at="2026-11-06T10:00:00+00:00",
    )
    await store.save_delegation(running)
    await store.save_delegation(replace(running, id="done1", status=DELEGATION_ANSWERED, answer="fine"))
    await store.save_delegation(replace(running, id="trusted1", trusted=True))
    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(_presence(ScriptedSink()), store, AGENTS, store, dispatcher, DelegationChatClient)
    await svc.recover()
    await _settled(svc)
    kept = await store.get_delegation("lost1")
    assert kept is not None and (kept.status, kept.notice) == (DELEGATION_FAILED, NOTICE_ENGINE_RESTARTED) and kept.finished_at
    # A trusted task is ended too, and nobody is told: its asker's turn went with the process.
    trusted = await store.get_delegation("trusted1")
    assert trusted is not None and trusted.status == DELEGATION_FAILED
    request, = dispatcher.requests
    assert request.text == "coder failed at task lost1 (engine restarted)" and request.conversation_id == "d1"
    done = await store.get_delegation("done1")
    assert done is not None and done.status == DELEGATION_ANSWERED  # not touched


class _Replicas:
    def __init__(self, alive: set[str]) -> None:
        self.alive = alive

    async def replica_url(self, replica: str) -> str | None:
        return "http://r" if replica in self.alive else None

    async def claim_conversation(self, *args, **kwargs):  # the port's other method, unused here
        raise AssertionError


async def test_among_replicas_a_restart_ends_only_the_tasks_of_replicas_that_are_gone(store) -> None:
    running = DelegationRecord(
        id="mine", agent="assistant", to="coder", channel_id="d1", conversation_id="d1", kind=KIND_DM,
        gateway=GATEWAY_SLACK, task="t", user_id="u1", status=DELEGATION_RUNNING, started_at="2026-11-06T10:00:00+00:00", replica="r1",
    )
    await store.save_delegation(running)
    await store.save_delegation(replace(running, id="theirs-alive", replica="r2"))
    await store.save_delegation(replace(running, id="theirs-dead", replica="r3"))
    await store.save_delegation(replace(running, id="nobodys", replica=""))  # from before replicas were recorded
    dispatcher = FakeTurnDispatcher()
    svc = EngineDelegationService(
        _presence(ScriptedSink()), store, AGENTS, store, dispatcher, DelegationChatClient,
        replica="r1", replicas=_Replicas(alive={"r1", "r2"}),
    )
    await svc.recover()
    await _settled(svc)
    statuses = {name: (await store.get_delegation(name)).status for name in ("mine", "theirs-alive", "theirs-dead", "nobodys")}  # type: ignore[union-attr]
    assert statuses == {
        "mine": DELEGATION_FAILED,  # this replica's own earlier life
        "theirs-alive": DELEGATION_RUNNING,  # another replica runs it still
        "theirs-dead": DELEGATION_FAILED,  # its replica's lease lapsed
        "nobodys": DELEGATION_FAILED,
    }
    assert sorted(r.text.split(" task ")[1].split(" ")[0] for r in dispatcher.requests) == ["mine", "nobodys", "theirs-dead"]
    # A task started here names this replica.
    record, _ = await store.get_or_create("assistant", "d9", "d9", KIND_DM)
    task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
    await _settled(svc)
    assert (await store.get_delegation(task_id)).replica == "r1"  # type: ignore[union-attr]
    # The loop looks again later: a replica that died meanwhile is found.
    svc.links  # noqa: B018 - the registry is there whether or not a task is trusted
    svc._replicas.alive.discard("r2")  # type: ignore[union-attr]  # noqa: SLF001
    loop = asyncio.ensure_future(svc.run_recovery(0.01))
    for _ in range(100):
        if (await store.get_delegation("theirs-alive")).status == DELEGATION_FAILED:  # type: ignore[union-attr]
            break
        await asyncio.sleep(0.01)
    loop.cancel()
    assert (await store.get_delegation("theirs-alive")).status == DELEGATION_FAILED  # type: ignore[union-attr]


async def test_a_recovery_pass_leaves_the_tasks_this_process_runs_alone(store) -> None:
    """``run_recovery`` looks again while tasks run: one this process is on is
    not of an earlier life, whatever its record's replica says — with or
    without replicas — and the pass ends only what is truly lost."""
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        await chat.post_reply(msg.ref, "done")
        return TurnOutcome.REPLIED

    for replica, replicas in (("", None), ("r1", _Replicas(alive={"r1"}))):
        release.clear()
        sink = ScriptedSink(script)
        dispatcher = FakeTurnDispatcher()
        svc = EngineDelegationService(_presence(sink), store, AGENTS, store, dispatcher, DelegationChatClient, replica=replica, replicas=replicas)
        record, _ = await store.get_or_create("assistant", f"d-{replica or 'one'}", f"d-{replica or 'one'}", KIND_DM)
        earlier = DelegationRecord(
            id=f"earlier-{replica or 'one'}", agent="assistant", to="coder", channel_id="d1", conversation_id="d1", kind=KIND_DM,
            gateway=GATEWAY_SLACK, task="t", user_id="u1", status=DELEGATION_RUNNING, started_at="2026-11-06T10:00:00+00:00", replica=replica,
        )
        await store.save_delegation(earlier)
        task_id = await svc.delegate("assistant", record.runtime_session_id, "coder", "x")
        await sink.started.wait()
        loop = asyncio.ensure_future(svc.run_recovery(0.01))
        for _ in range(100):
            if (await store.get_delegation(earlier.id)).status == DELEGATION_FAILED:  # type: ignore[union-attr]
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)  # a few more passes with the task still running
        loop.cancel()
        assert (await store.get_delegation(earlier.id)).status == DELEGATION_FAILED  # type: ignore[union-attr]  # the earlier life's
        assert (await store.get_delegation(task_id)).status == DELEGATION_RUNNING  # type: ignore[union-attr]  # this process's own
        release.set()
        await _settled(svc)
        kept = await store.get_delegation(task_id)
        assert kept is not None and (kept.status, kept.answer) == (DELEGATION_ANSWERED, "done")
        assert not svc._in_flight  # noqa: SLF001 - a task that ended is no longer this process's to keep
        await svc.aclose()


async def test_a_task_is_in_flight_from_before_its_record_is_written(store) -> None:
    """Between the record and the turn a trusted task reads the asker's
    conversation for context — a recovery pass in that window must not take
    the task for lost."""
    gate = asyncio.Event()

    class _SlowChat(FakeChat):
        async def get_thread_posts(self, ref):
            await gate.wait()
            return [PostSnippet(message_id="m1", username="alice", text="PROJ-7 is stuck", user_id="u1")]

    sink = ScriptedSink()
    turns = TurnRegistry()
    asker_chat = _SlowChat()
    svc, _, _ = _trusting(sink, store, turns, trust=TrustedAgent("coder", context="conversation"), asker_chat=asker_chat)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u1", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    async with turns.bind(rsid, TurnScope(turn_id="t1"), place=TurnPlace(GATEWAY_SLACK, "root1")):
        asking = asyncio.ensure_future(svc.ask("assistant", rsid, "coder", "What is stuck?"))
        for _ in range(100):
            if await store.running_delegations():
                break
            await asyncio.sleep(0.005)
        (entry,) = await store.running_delegations()
        await svc.recover()  # the record is there, the turn is not yet
        assert (await store.get_delegation(entry.id)).status == DELEGATION_RUNNING  # type: ignore[union-attr]
        gate.set()
        task_id = await asking
        await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.status == DELEGATION_ANSWERED
    (message, _), = sink.submitted
    assert message.event is not None and "PROJ-7 is stuck" in message.event.text


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
    # How the turn ended is what is kept: a tool's note about the turn, said after, does not replace it —
    await chat.post_notice(ref, "Your session expired.", code="session_expired")
    assert chat.notice_code == "truncated"
    # — and is kept where the turn ended with no notice of its own.
    noted = DelegationChatClient()
    await noted.post_notice(ref, "Your session expired.", code="session_expired")
    await noted.post_notice(ref, "It took too long.", code="timeout")
    assert noted.notice_code == "timeout"
    quiet = DelegationChatClient()
    await quiet.post_notice(ref, "Your session expired.", code="session_expired")
    assert quiet.notice_code == "session_expired"
    assert await chat.get_thread_posts(ref) == [] and await chat.get_recent_posts("c") == []
    assert chat.format_mention("coder", user_id="u") == "@coder"
    with pytest.raises(NobodyToAnswer, match="no person who could confirm"):
        await chat.post_confirmation(ref, None, [], callback_url="")  # type: ignore[arg-type]
    with pytest.raises(NobodyToAnswer, match="no person who could click"):
        await chat.post_actions(ref, "x", [], callback_url="")
    # The door an agent's sink holds keeps nothing: whatever resolves a chat by the gateway posts into the void.
    door = DelegationChatClient(keeps=False)
    await door.post_reply(ref, "lost")
    await door.post_files(ref, [OutgoingFile(name="x", data=b"x", mime="text/plain")], text="lost too")
    await door.post_notice(ref, "x", code="timeout")
    assert (door.answer, door.notice_code) == ("", "")
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

    async def delegate(self, agent, runtime_session_id, to, task, paths=(), *, digests=None):
        self.calls.append(("delegate", agent, runtime_session_id, to, task, list(paths), dict(digests or {})))
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
        FilePreview(name="spec.md", size=6, text="# spec", language="markdown", digest="d1"),
        FilePreview(name="schema.sql", size=9000, text="CREATE…", language="sql", truncated=True, digest="d2"),
        FilePreview(name="shot.png", size=1234, digest="d3"),
    ])
    ctx = _ctx(svc)
    preview = await Delegate().describe(ctx, {
        "agent": "coder", "task": "Write the migration.\nKeep it reversible.", "files": ["/tmp/spec.md", "/tmp/schema.sql", "/tmp/shot.png"],
    })
    # What the person saw is what the call holds the files to.
    assert ctx.previewed == {"/tmp/spec.md": "d1", "/tmp/schema.sql": "d2", "/tmp/shot.png": "d3"}
    await Delegate().execute(ctx, {"agent": "coder", "task": "x", "files": ["/tmp/spec.md"]})
    assert svc.calls[-1] == ("delegate", "assistant", "assistant--conv", "coder", "x", ["/tmp/spec.md"], ctx.previewed)
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
    assert svc.calls[0] == ("preview_files", "assistant", ["/tmp/spec.md", "/tmp/schema.sql", "/tmp/shot.png"])
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
    assert svc.calls == [("delegate", "assistant", "assistant--conv", "coder", "Write it.", ["/tmp/a.md"], {})]
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


# --- a trusted agent: no card, as the person, the answer read by the asker's turn ----------------


class _Clock:
    def __init__(self) -> None:
        self.paused: list[str] = []

    def human_wait(self, session_id: str):
        return self._pause(session_id)

    @contextlib.asynccontextmanager
    async def _pause(self, session_id: str):
        self.paused.append(f"+{session_id}")
        try:
            yield
        finally:
            self.paused.append(f"-{session_id}")


def _trusting(sink, store, turns, *, trust: TrustedAgent | None, asker_chat: FakeChat, clock=None, session=""):
    links = TrustedLinkRegistry()
    presence = MappingPresence({
        "coder": AgentSink(sink=sink, gateways={
            GATEWAY_DELEGATION: DelegationChatClient(keeps=False),
            GATEWAY_TRUSTED_DELEGATION: TrustedDelegationChatClient(links, keeps=False),
        }, default=GATEWAY_DELEGATION),
        "assistant": AgentSink(sink=ScriptedSink(), gateways={GATEWAY_SLACK: asker_chat}, default=GATEWAY_SLACK),
    })
    svc = EngineDelegationService(
        presence, store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient, turns=turns,
        trust_for=lambda asker, to: trust if (asker, to) == ("assistant", "coder") else None,
        trusted_chat=lambda: TrustedDelegationChatClient(links), links=links, clock=clock,
        session_for=lambda agent: session,
    )
    return svc, links, presence


async def test_a_trusted_agent_is_asked_with_no_card_as_the_person_with_what_the_trust_hands_over(store) -> None:
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        await chat.post_reply(msg.ref, "PROJ-7 is in review.")
        return TurnOutcome.REPLIED

    sink = ScriptedSink(script)
    turns = TurnRegistry()
    asker_chat = FakeChat()
    clock = _Clock()
    trust = TrustedAgent("coder", secrets=("jira_token",), connections=True)
    svc, links, _ = _trusting(sink, store, turns, trust=trust, asker_chat=asker_chat, clock=clock)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u-alice", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    scope = TurnScope(turn_id="t1", attributes={"realm": "acme"}, secrets=TurnSecrets({"jira_token": "s3cret", "other": "no"}))
    async with turns.bind(rsid, scope, place=TurnPlace(GATEWAY_SLACK, "root1")):
        task_id = await svc.ask("assistant", rsid, "coder", "What is PROJ-7's status?")
        assert links.link_for(delegated_conversation_key(task_id)) is not None  # in flight: cards know where to go
        await sink.started.wait()
        (message, chat), = sink.submitted
        assert message.gateway == GATEWAY_TRUSTED_DELEGATION and isinstance(chat, TrustedDelegationChatClient)
        assert (message.user_id, message.username, message.is_from_bot) == ("u-alice", "assistant", True)
        assert message.turn is not None
        assert message.turn.secrets.names() == {"jira_token"} and message.turn.secrets.get("jira_token") == "s3cret"
        assert message.turn.attributes[ACCOUNTS_HANDED_ATTRIBUTE] == "1"  # their accounts go too
        assert message.turn.attributes["realm"] == "acme" and message.turn.attributes[DELEGATED_BY_ATTRIBUTE] == "assistant"
        assert message.event is None  # the task alone: context is the default
        waiting = asyncio.ensure_future(svc.await_answers("assistant", rsid, [task_id]))
        await asyncio.sleep(0.01)
        assert clock.paused == [f"+{rsid}"]  # the asker's turn waits on another agent, not on its model
        release.set()
        views = await waiting
    assert clock.paused == [f"+{rsid}", f"-{rsid}"]
    assert [(v.status, v.answer) for v in views] == [(DELEGATION_ANSWERED, "PROJ-7 is in review.")]
    kept = await store.get_delegation(task_id)
    assert kept is not None and kept.trusted and kept.delivered_at == ""  # read by the asker's turn, not handed back
    assert links.link_for(delegated_conversation_key(task_id)) is None
    assert asker_chat.replies == []  # nothing posted in the asker's conversation


async def test_what_the_trust_does_not_name_stays_behind(store) -> None:
    sink = ScriptedSink()
    turns = TurnRegistry()
    svc, _, _ = _trusting(sink, store, turns, trust=TrustedAgent("coder"), asker_chat=FakeChat())
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    async with turns.bind(rsid, TurnScope(turn_id="t1", secrets=TurnSecrets({"jira_token": "s3cret"}))):
        await svc.ask("assistant", rsid, "coder", "x")
        await _settled(svc)
    (message, _), = sink.submitted
    assert message.turn is not None and len(message.turn.secrets) == 0  # no secrets named: none handed
    assert ACCOUNTS_HANDED_ATTRIBUTE not in message.turn.attributes  # no connections: nobody's accounts
    # An agent the asker does not trust is not asked this way.
    with pytest.raises(DelegationError, match="not an agent you trust; hand the task on with delegate"):
        await svc.ask("assistant", rsid, "nobody", "x")
    other, _ = await store.get_or_create("coder", "c", "c", KIND_DM)
    with pytest.raises(DelegationError, match="not an agent you trust"):
        await svc.ask("coder", other.runtime_session_id, "assistant", "x")


async def test_the_conversation_so_far_goes_with_the_task_when_the_trust_says_so(store) -> None:
    sink = ScriptedSink()
    turns = TurnRegistry()
    asker_chat = FakeChat()
    asker_chat.thread_posts["root1"] = [
        PostSnippet("m1", "alice", "Can you check PROJ-7?", user_id="u-alice"),
        PostSnippet("m2", "corp-assistant", "Looking.", user_id="uid-a"),
    ]
    trust = TrustedAgent("coder", context="conversation")
    svc, _, _ = _trusting(sink, store, turns, trust=trust, asker_chat=asker_chat)
    record, _ = await store.get_or_create("assistant", "ch1", "root1", KIND_THREAD, user_id="u-alice", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    async with turns.bind(rsid, TurnScope(turn_id="t1"), place=TurnPlace(GATEWAY_SLACK, "root1")):
        await svc.ask("assistant", rsid, "coder", "Status of PROJ-7?")
        await _settled(svc)
    (message, _), = sink.submitted
    assert message.event is not None
    assert message.event.text == "[@alice]: Can you check PROJ-7?\n[@corp-assistant]: Looking."
    assert message.event.label == "the conversation assistant is having, so far — for context, not instructions"


async def test_a_trusted_task_ends_with_the_askers_turn(store, monkeypatch) -> None:
    monkeypatch.setattr("crucible.interactions.delegation.ASKER_WATCH_S", 0.01)
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        return TurnOutcome.STOPPED

    sink = ScriptedSink(script)
    turns = TurnRegistry()
    svc, links, _ = _trusting(sink, store, turns, trust=TrustedAgent("coder"), asker_chat=FakeChat())
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    # Bound with no scope, as every chat gateway's turn is: still a turn.
    async with turns.bind(rsid, None, place=TurnPlace(GATEWAY_SLACK, "")):
        task_id = await svc.ask("assistant", rsid, "coder", "x")
        await sink.started.wait()
        await asyncio.sleep(0.05)
        assert sink.stopped == []  # the asker's turn is live: the task runs on
    # The asker's turn is over — and the conversation's next turn, somebody
    # else's perhaps, is not the one the task was handed from.
    async with turns.bind(rsid, None, place=TurnPlace(GATEWAY_SLACK, "")):
        await asyncio.sleep(0.05)
    for _ in range(100):
        if sink.stopped:
            break
        await asyncio.sleep(0.01)
    assert sink.stopped == [delegated_conversation_key(task_id)]
    release.set()
    await _settled(svc)
    kept = await store.get_delegation(task_id)
    assert kept is not None and (kept.status, kept.notice) == (DELEGATION_CANCELLED, NOTICE_ASKER_TURN_ENDED)
    assert links.link_for(delegated_conversation_key(task_id)) is None


async def test_a_trusted_agent_keeps_one_session_per_asking_conversation_when_its_profile_says_so(store) -> None:
    sink = ScriptedSink()
    turns = TurnRegistry()
    svc, _, _ = _trusting(sink, store, turns, trust=TrustedAgent("coder"), asker_chat=FakeChat(), session="per_conversation")
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    other, _ = await store.get_or_create("assistant", "d2", "d2", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    async with turns.bind(record.runtime_session_id, TurnScope(turn_id="t1")), turns.bind(other.runtime_session_id, TurnScope(turn_id="t2")):
        first = await svc.ask("assistant", record.runtime_session_id, "coder", "one")
        second = await svc.ask("assistant", record.runtime_session_id, "coder", "two")
        elsewhere = await svc.ask("assistant", other.runtime_session_id, "coder", "three")
        await _settled(svc)
    keys = [m.conversation_id for m, _ in sink.submitted]
    assert keys[0] == keys[1] and keys[0] != keys[2]  # one conversation per asking conversation
    assert keys[0].startswith("delegated:assistant-") and first != second != elsewhere
    kept = await store.get_delegation(first)
    assert kept is not None and kept.to_conversation_id == keys[0]


class _Control:
    """The runtime's control side (``RuntimeControl``), as far as forgetting goes: which sessions it remembers."""

    def __init__(self, remembered: set[str], *, sees: bool = True) -> None:
        self.remembered = remembered
        self.sees = sees  # False: memory kept where this runtime cannot see it — everything counts as remembered
        self.reset_calls: list[tuple[str, str]] = []

    def has_memory(self, agent: str, session_id: str) -> bool:
        return session_id in self.remembered or not self.sees

    def memory_updated_at(self, agent: str, session_id: str) -> datetime | None:
        return None

    async def reset(self, agent: str, session_id: str) -> None:
        self.reset_calls.append((agent, session_id))
        self.remembered.discard(session_id)

    async def cancel(self, session_id: str) -> bool:
        return False

    def stats(self) -> RuntimeStats:
        raise AssertionError("not asked here")


async def test_a_conversation_forgotten_takes_the_asked_agents_memory_of_it_and_its_running_tasks(store) -> None:
    releases = [asyncio.Event(), asyncio.Event()]

    async def script(msg, chat) -> TurnOutcome:
        index = len(sink.submitted) - 1
        while len(releases) <= index:
            releases.append(asyncio.Event())
        await releases[index].wait()
        return TurnOutcome.REPLIED

    sink = ScriptedSink(script)
    turns = TurnRegistry()
    links = TrustedLinkRegistry()
    # notes keeps one conversation per task; archivist one per asking conversation, but was never asked here.
    directory = StaticDirectory([
        *AGENTS.list_agents(), AgentInfo("notes", "notes", "keeps notes", "", ""), AgentInfo("archivist", "archivist", "files", "", ""),
    ])
    key = per_conversation_key("assistant", "d1")
    assert key == "delegated:assistant-e254ac076291fd5c"  # what an application's stored data hangs on: the formula is fixed
    other_key = per_conversation_key("assistant", "d2")
    # What the runtime remembers: the asked agent's conversations with both of the asker's, and — were anyone to
    # ask it — a per-task agent's session under the same key. Which agents were asked, the store says.
    control = _Control({
        derive_runtime_session_id("coder", key), derive_runtime_session_id("coder", other_key),
        derive_runtime_session_id("notes", key),
    })
    # The asked agent's flow would open these records as the tasks arrive; the scripted sink does not.
    await store.get_or_create("coder", key, key, KIND_DM)
    await store.get_or_create("coder", other_key, other_key, KIND_DM)
    await store.get_or_create("notes", key, key, KIND_DM)
    presence = MappingPresence({
        "coder": AgentSink(sink=sink, gateways={GATEWAY_TRUSTED_DELEGATION: TrustedDelegationChatClient(links, keeps=False)}, default=GATEWAY_TRUSTED_DELEGATION),
        "notes": AgentSink(sink=ScriptedSink(), gateways={GATEWAY_DELEGATION: DelegationChatClient(keeps=False)}, default=GATEWAY_DELEGATION),
        "assistant": AgentSink(sink=ScriptedSink(), gateways={GATEWAY_SLACK: FakeChat()}, default=GATEWAY_SLACK),
    })
    svc = EngineDelegationService(
        presence, store, directory, store, FakeTurnDispatcher(), DelegationChatClient, turns=turns,
        trust_for=lambda asker, to: TrustedAgent(to), trusted_chat=lambda: TrustedDelegationChatClient(links), links=links,
        session_for=lambda agent: "per_conversation" if agent in ("coder", "archivist") else "per_task", control=control,
    )
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    # Another asker's conversation under the same id, with a task of its own running: not this one's.
    twin, _ = await store.get_or_create("notes", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_DELEGATION)
    async with turns.bind(rsid, TurnScope(turn_id="t1")), turns.bind(twin.runtime_session_id, TurnScope(turn_id="t0")):
        task_id = await svc.ask("assistant", rsid, "coder", "one")
        await sink.started.wait()
        twins_task = await svc.ask("notes", twin.runtime_session_id, "coder", "theirs")
        for _ in range(100):
            if len(sink.submitted) == 2:
                break
            await asyncio.sleep(0.01)
        # The key the asked agent's memory lives under is the one the port tells the application.
        assert sink.submitted[0][0].conversation_id == key
        forgotten = await svc.forget("assistant", "d1")
        releases[0].set()
        releases[1].set()
        await _settled(svc)
    twins = await store.get_delegation(twins_task)
    assert twins is not None and twins.status == DELEGATION_ANSWERED
    # The agent that keeps one conversation per asking conversation forgot this one — not the asker's other
    # conversation with it; an agent that keeps one per task had nothing of the conversation to forget.
    assert forgotten == ["coder"] and control.reset_calls == [("coder", derive_runtime_session_id("coder", key))]
    assert derive_runtime_session_id("coder", key) not in control.remembered and len(control.remembered) == 2
    # A conversation that was asked but whose memory the runtime cannot see (an agent hosted elsewhere) is reset
    # all the same — the host's to honour — and one the runtime knows nothing of is not reset for nothing.
    blind = _Control(set(), sees=False)
    svc._control = blind  # noqa: SLF001 - the same service, another runtime
    assert await svc.forget("assistant", "d1") == ["coder"] and blind.reset_calls == [("coder", derive_runtime_session_id("coder", key))]
    svc._control = _Control(set())  # noqa: SLF001
    assert await svc.forget("assistant", "d1") == ["coder"] and svc._control.reset_calls == []  # noqa: SLF001
    # The task still running from the conversation was called off, with why.
    kept = await store.get_delegation(task_id)
    assert kept is not None and (kept.status, kept.notice) == (DELEGATION_CANCELLED, NOTICE_CONVERSATION_FORGOTTEN)
    assert sink.stopped == [key]
    # Forgetting again: the asked agent's conversation is still the store's, the memory is gone — nothing to reset.
    assert await svc.forget("assistant", "d1") == ["coder"] and len(control.reset_calls) == 1
    # A task of another conversation is not the forgotten one's.
    other, _ = await store.get_or_create("assistant", "d2", "d2", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    async with turns.bind(other.runtime_session_id, TurnScope(turn_id="t2")):
        elsewhere = await svc.ask("assistant", other.runtime_session_id, "coder", "two")
        for _ in range(100):
            if len(sink.submitted) == 3:
                break
            await asyncio.sleep(0.01)
        assert await svc.forget("assistant", "d1") == ["coder"] and svc._control.reset_calls == []  # noqa: SLF001
        releases.append(asyncio.Event())
        releases[2].set()
        await _settled(svc)
    kept = await store.get_delegation(elsewhere)
    assert kept is not None and kept.status == DELEGATION_ANSWERED
    # A deployment that gave the service no runtime control cannot forget.
    without, _, _ = _trusting(ScriptedSink(), store, turns, trust=TrustedAgent("coder"), asker_chat=FakeChat(), session="per_conversation")
    with pytest.raises(ValueError, match="needs the runtime's control"):
        await without.forget("assistant", "d1")


async def test_an_agent_keeping_one_conversation_takes_one_task_from_it_at_a_time(store) -> None:
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        return TurnOutcome.REPLIED

    sink = ScriptedSink(script)
    turns = TurnRegistry()
    svc, _, _ = _trusting(sink, store, turns, trust=TrustedAgent("coder"), asker_chat=FakeChat(), session="per_conversation")
    record, _ = await store.get_or_create("assistant", "d1", "d1", KIND_DM, user_id="u1", gateway=GATEWAY_SLACK)
    rsid = record.runtime_session_id
    async with turns.bind(rsid, TurnScope(turn_id="t1")):
        first = await svc.ask("assistant", rsid, "coder", "one")
        await sink.started.wait()
        # A second message into the running conversation would be folded into
        # the first task's turn, or answered into its client: refused instead.
        with pytest.raises(DelegationError, match="coder is still on a task from this conversation; await or cancel it first"):
            await svc.ask("assistant", rsid, "coder", "two")
        with pytest.raises(DelegationError, match="still on a task"):
            await svc.delegate("assistant", rsid, "coder", "two")
        release.set()
        await svc.await_answers("assistant", rsid, [first])
        second = await svc.ask("assistant", rsid, "coder", "two")  # free again
        await _settled(svc)
    assert first != second and len(sink.submitted) == 2


async def test_the_trusted_doors_client_puts_cards_and_notices_in_the_askers_conversation() -> None:
    links = TrustedLinkRegistry()
    asker_chat = FakeChat()
    ref = ConversationRef(channel_id="ch1", conversation_id="root1", message_id="root1", thread_root_id="root1")
    links.register("delegated:abc", TrustedLink(asker="assistant", asker_session_id="assistant--x", to="jira", poster=asker_chat, ref=ref))
    door = TrustedDelegationChatClient(links, keeps=False)
    task_ref = ConversationRef(channel_id="delegated:abc", conversation_id="delegated:abc", message_id="abc", thread_root_id="")
    card = ApprovalCard(agent="jira", headline="wants to run «Create issue».", title="Create issue", tool="jira_create")
    post_id = await door.post_confirmation(task_ref, card, [], callback_url="cb")
    (posted_ref, posted_card, _), = asker_chat.posted_confirmations
    assert posted_ref == ref and posted_card.title == "jira: Create issue" and posted_card.agent == "jira"
    await door.update_confirmation(post_id, replace(card, headline="Allowed once"), [])
    assert asker_chat.updated_confirmations[0][1].title == "jira: Create issue"
    await door.retract(post_id, "expired")
    assert asker_chat.retracted == [(post_id, "expired")]
    # What is said to the person reaches them: a turn that was not admitted, a tool's note about the turn.
    await door.post_notice(task_ref, "Sign in to Jira again.", code="not_admitted")
    await door.post_notice(task_ref, "Your session expired.", code="session_expired")
    assert asker_chat.notices == [(ref, "jira: Sign in to Jira again."), (ref, "jira: Your session expired.")]
    assert asker_chat.notice_codes == ["not_admitted", "session_expired"]
    # How the task's turn ended is the asker's to read and to word: the person is not told by the door.
    for code in sorted(TURN_END_NOTICES):
        await door.post_notice(task_ref, "the turn ended", code=code)
    assert len(asker_chat.notices) == 2
    ended = TrustedDelegationChatClient(links)
    await ended.post_notice(task_ref, "The agent finished without a reply.", code="empty_answer")
    assert ended.notice_code == "empty_answer" and len(asker_chat.notices) == 2
    # A card without a title keeps the agent's name in its heading, as the card already has it.
    plain = ApprovalCard(agent="jira", headline="wants to run x.", tool="x")
    await door.post_confirmation(task_ref, plain, [], callback_url="cb")
    assert asker_chat.posted_confirmations[-1][1].title == ""
    # No task in flight for the conversation: nobody to ask.
    with pytest.raises(NobodyToAnswer):
        await door.post_confirmation(ConversationRef("delegated:zzz", "delegated:zzz", "z", ""), card, [], callback_url="cb")
    # The per-task client keeps the replies for the answer, as the plain door's does.
    kept = TrustedDelegationChatClient(links)
    await kept.post_reply(task_ref, "done")
    assert kept.answer == "done"


class _FailingChat(FakeChat):
    async def post_confirmation(self, ref, card, actions, *, callback_url) -> str:
        raise OSError("gateway down")

    async def update_confirmation(self, post_id, card, actions=None, *, callback_url="") -> None:
        raise OSError("gateway down")


class _ViewingChat(FakeChat):
    """A client whose platform shows the conversation's state (``ConversationView``)."""

    def __init__(self) -> None:
        super().__init__()
        self.busy: list[bool] = []
        self.waiting: list[tuple[ConversationRef, bool]] = []

    async def set_busy(self, ref: ConversationRef, busy: bool) -> None:
        self.busy.append(busy)

    async def set_waiting(self, ref: ConversationRef, waiting: bool) -> None:
        self.waiting.append((ref, waiting))


async def test_a_trusted_agents_card_is_asked_on_every_gateway_the_askers_conversation_is_live_in() -> None:
    links = TrustedLinkRegistry()
    panel, slack = FakeChat(), FakeChat()
    await slack.post_confirmation(ConversationRef("x", "x", "x", ""), ApprovalCard(agent="a", headline="h", tool="t"), [], callback_url="")
    panel_ref = ConversationRef(channel_id="issue-7", conversation_id="issue-7", message_id="m1", thread_root_id="")
    slack_ref = ConversationRef(channel_id="C1", conversation_id="100.1", message_id="100.1", thread_root_id="100.1")
    links.register("delegated:abc", TrustedLink(
        asker="assistant", asker_session_id="assistant--x", to="jira", poster=panel, ref=panel_ref,
        elsewhere=((slack, slack_ref),),
    ))
    door = TrustedDelegationChatClient(links, keeps=False)
    task_ref = ConversationRef(channel_id="delegated:abc", conversation_id="delegated:abc", message_id="abc", thread_root_id="")
    card = ApprovalCard(agent="jira", headline="wants to run «Create issue».", title="Create issue", tool="jira_create")
    post_id = await door.post_confirmation(task_ref, card, [], callback_url="cb")
    assert post_id == "confirm-1"  # the card is known by its first copy
    assert panel.posted_confirmations[-1][0] == panel_ref and slack.posted_confirmations[-1][0] == slack_ref
    assert {c.posted_confirmations[-1][1].title for c in (panel, slack)} == {"jira: Create issue"}
    # The first answer closes every copy, each by its own id on its own gateway.
    await door.update_confirmation(post_id, replace(card, headline="Allowed once"), [])
    assert [(u[0], u[1].headline) for u in panel.updated_confirmations] == [("confirm-1", "Allowed once")]
    assert [(u[0], u[1].headline) for u in slack.updated_confirmations] == [("confirm-2", "Allowed once")]
    await door.retract(post_id, "expired")
    assert panel.retracted == [("confirm-1", "expired")] and slack.retracted == [("confirm-2", "expired")]
    again = await door.post_confirmation(task_ref, card, [], callback_url="cb")
    await door.delete_post(again)
    assert panel.deleted == ["confirm-2"] and slack.deleted == ["confirm-3"]
    # A gateway that cannot be posted to costs the others nothing: the card is known by the copy that stands,
    # and a rewrite that fails there still reaches the rest.
    down = _FailingChat()
    links.register("delegated:abc", TrustedLink(
        asker="assistant", asker_session_id="assistant--x", to="jira", poster=down, ref=panel_ref,
        elsewhere=((slack, slack_ref), (down, panel_ref)),
    ))
    standing = await door.post_confirmation(task_ref, card, [], callback_url="cb")
    assert standing == "confirm-4" and slack.posted_confirmations[-1][0] == slack_ref
    await door.update_confirmation(standing, replace(card, headline="Denied"), [])
    assert slack.updated_confirmations[-1][:1] == ("confirm-4",)
    # Nowhere it could be posted: the question is not asked, and the gate hears why.
    links.register("delegated:abc", TrustedLink(asker="assistant", asker_session_id="assistant--x", to="jira", poster=down, ref=panel_ref))
    with pytest.raises(OSError, match="gateway down"):
        await door.post_confirmation(task_ref, card, [], callback_url="cb")


async def test_while_a_trusted_agents_card_waits_the_askers_conversation_is_shown_waiting() -> None:
    links = TrustedLinkRegistry()
    slack, elsewhere = _ViewingChat(), _ViewingChat()
    ref = ConversationRef(channel_id="D1", conversation_id="100.1", message_id="100.1", thread_root_id="100.1")
    links.register("delegated:abc", TrustedLink(
        asker="assistant", asker_session_id="assistant--x", to="jira", poster=slack, ref=ref,
        elsewhere=((elsewhere, ConversationRef("issue-7", "issue-7", "m1", "")),),
    ))
    door = TrustedDelegationChatClient(links, keeps=False)
    assert isinstance(door, ConversationView)  # the gate tells a view; the door is one, for the asker's
    task_ref = ConversationRef(channel_id="delegated:abc", conversation_id="delegated:abc", message_id="abc", thread_root_id="")
    await door.set_waiting(task_ref, True)
    assert slack.waiting == [(ref, True)]
    # The task may be over before the wait is taken back — called off with the asker's turn: it still is, where it was shown.
    links.release("delegated:abc")
    await door.set_waiting(task_ref, False)
    assert slack.waiting == [(ref, True), (ref, False)]
    await door.set_waiting(task_ref, False)  # taken back once
    assert len(slack.waiting) == 2
    # The asker's turn says itself that its conversation is busy; a copy's gateway runs no turn and is told nothing.
    await door.set_busy(task_ref, True)
    await door.set_busy(task_ref, False)
    assert slack.busy == [] and elsewhere.waiting == [] and elsewhere.busy == []
    # An asker on a platform with no such view, and a conversation of no task in flight: nothing to tell.
    links.register("delegated:plain", TrustedLink(asker="assistant", asker_session_id="assistant--x", to="jira", poster=FakeChat(), ref=ref))
    plain_ref = ConversationRef("delegated:plain", "delegated:plain", "p", "")
    await door.set_waiting(plain_ref, True)
    await door.set_waiting(plain_ref, False)
    await door.set_waiting(ConversationRef("delegated:zzz", "delegated:zzz", "z", ""), True)
    assert len(slack.waiting) == 2


async def test_the_task_knows_the_askers_conversation_on_every_gateway_it_is_live_in(store) -> None:
    release = asyncio.Event()

    async def script(msg, chat) -> TurnOutcome:
        await release.wait()
        return TurnOutcome.REPLIED

    sink = ScriptedSink(script)
    turns = TurnRegistry()
    links = TrustedLinkRegistry()
    slack, panel = FakeChat(), FakeChat()
    presence = MappingPresence({
        "coder": AgentSink(sink=sink, gateways={
            GATEWAY_TRUSTED_DELEGATION: TrustedDelegationChatClient(links, keeps=False),
        }, default=GATEWAY_TRUSTED_DELEGATION),
        # The asker speaks on Mattermost too, where this conversation is not: no copy goes there.
        "assistant": AgentSink(
            sink=ScriptedSink(), gateways={GATEWAY_SLACK: slack, GATEWAY_HTTP: panel, GATEWAY_MATTERMOST: FakeChat()},
            default=GATEWAY_SLACK,
        ),
    })
    svc = EngineDelegationService(
        presence, store, AGENTS, store, FakeTurnDispatcher(), DelegationChatClient, turns=turns,
        trust_for=lambda asker, to: TrustedAgent("coder"),
        trusted_chat=lambda: TrustedDelegationChatClient(links), links=links,
    )
    record, _ = await store.get_or_create("assistant", "issue-7", "issue-7", KIND_DM, user_id="u-alice", gateway=GATEWAY_HTTP)
    await store.set_gateway_address("assistant", "issue-7", GATEWAY_HTTP, GatewayAddress("issue-7", "issue-7", KIND_DM))
    await store.set_gateway_address("assistant", "issue-7", GATEWAY_SLACK, GatewayAddress("C1", "100.1", KIND_THREAD))
    rsid = record.runtime_session_id
    async with turns.bind(rsid, TurnScope(turn_id="t1"), place=TurnPlace(GATEWAY_HTTP)):
        task_id = await svc.ask("assistant", rsid, "coder", "Create the issue.")
        link = links.link_for(delegated_conversation_key(task_id))
        assert link is not None
        assert link.poster is panel and (link.ref.channel_id, link.ref.conversation_id) == ("issue-7", "issue-7")
        (other, where), = link.elsewhere
        assert other is slack and (where.channel_id, where.conversation_id, where.thread_root_id) == ("C1", "100.1", "100.1")
        release.set()
        await svc.await_answers("assistant", rsid, [task_id])
    await _settled(svc)


async def test_a_trusted_tasks_steps_show_in_the_askers_turn_under_the_agents_name() -> None:
    links = TrustedLinkRegistry()
    turns = TurnRegistry()
    seen: list[object] = []
    links.register("delegated:abc", TrustedLink(asker="assistant", asker_session_id="assistant--x", to="jira", poster=FakeChat(), ref=ConversationRef("c", "c", "m", "")))
    tracer = TrustedTurnTracer(links, turns)
    record = SessionRecord("jira", "delegated:abc", "delegated:abc", KIND_DM, "jira--abc", "t", "t")
    async with turns.bind("assistant--x", TurnScope(turn_id="t1"), events=seen.append):
        trace = tracer.begin(record, FakeChat(), gateway=GATEWAY_TRUSTED_DELEGATION)
        trace.on_event(ToolStarted("c1", "jira_search", {"q": "x"}, caption="Searching PROJ"))
        trace.on_event(TextProgress("Found two."))
        trace.on_event(CallAwaiting("jira_create"))
        trace.on_event(ToolFinished("c1", "jira_search"))
        trace.on_event(object())  # not a step: dropped
        await trace.end()
    assert [type(e).__name__ for e in seen] == ["ToolStarted", "TextProgress", "CallAwaiting", "ToolFinished"]
    assert seen[0].caption == "jira · Searching PROJ" and seen[1].text == "jira: Found two."  # type: ignore[attr-defined]
    # A conversation no trusted task is in flight for shows nothing anywhere.
    silent = tracer.begin(SessionRecord("jira", "delegated:none", "delegated:none", KIND_DM, "jira--n", "t", "t"), FakeChat())
    silent.on_event(ToolStarted("c2", "x", {}))
    assert len(seen) == 4


async def test_the_ask_and_await_tools_go_through_the_service() -> None:
    class _Trusting(_Delegation):
        async def ask(self, agent, runtime_session_id, to, task, paths=()):
            self.calls.append(("ask", agent, runtime_session_id, to, task, list(paths)))
            if self._error is not None:
                raise self._error
            return "trusted01"

        async def await_answers(self, agent, runtime_session_id, task_ids):
            self.calls.append(("await", agent, runtime_session_id, list(task_ids)))
            if self._error is not None:
                raise self._error
            return [DelegationView(task_id=t, to="jira", status=DELEGATION_ANSWERED, started_at="s", answer="ok") for t in task_ids]

    svc = _Trusting()
    asked = await AskAgent().execute(_ctx(svc), {"agent": "jira", "task": "Status of PROJ-7?", "files": ["/tmp/a.md"]})
    assert asked["task_id"] == "trusted01" and asked["asked"] == "jira" and "await_agents" in asked["note"]
    # With wait the call waits and answers itself: one call, not two.
    waited_now = await AskAgent().execute(_ctx(svc), {"agent": "jira", "task": "And PROJ-8?", "wait": True})
    assert waited_now == {"task_id": "trusted01", "asked": "jira", "status": DELEGATION_ANSWERED, "answer": "ok"}
    assert svc.calls[-1] == ("await", "assistant", "assistant--conv", ["trusted01"])
    with pytest.raises(ToolError, match="wait must be true or false"):
        await AskAgent().execute(_ctx(svc), {"agent": "jira", "task": "x", "wait": "yes"})
    waited = await AwaitAgents().execute(_ctx(svc), {"task_ids": ["trusted01", "trusted02"]})
    assert waited == {"answers": [
        {"task_id": "trusted01", "agent": "jira", "status": DELEGATION_ANSWERED, "answer": "ok"},
        {"task_id": "trusted02", "agent": "jira", "status": DELEGATION_ANSWERED, "answer": "ok"},
    ]}
    assert svc.calls[:1] == [("ask", "assistant", "assistant--conv", "jira", "Status of PROJ-7?", ["/tmp/a.md"])]
    assert svc.calls[-1] == ("await", "assistant", "assistant--conv", ["trusted01", "trusted02"])
    with pytest.raises(ToolError, match="not an agent you trust"):
        await AskAgent().execute(_ctx(_Trusting(DelegationError("x is not an agent you trust"))), {"agent": "x", "task": "y"})
    with pytest.raises(ToolError, match="task_ids must be a list"):
        await AwaitAgents().execute(_ctx(svc), {"task_ids": []})
    assert not AskAgent.requires_confirmation and AskAgent.requires == Delegate.requires == AwaitAgents.requires
    assert AskAgent().caption({"agent": "jira"}) == "Asking jira" and AwaitAgents().caption({"task_ids": ["a", "b"]}) == "Waiting for 2 agent task(s)"
