"""Handing a task to another agent, through the engine.

An agent that wants another's work — one whose model is better at the task,
or one that is allowed what the asker is not — does not reach into that
agent, and the two need no chat in common: the engine opens a turn of the
other agent on the task alone, in a conversation of its own that holds
nothing but the task and its files — no thread to replay, no memory of an
earlier task unless the asked agent keeps one session per asking conversation
(``AgentSpec.delegation_session``) — and when that turn ends, a turn of the asker's with the
answer, quoted as an agent's words. Asynchronous by design: the tool call
ends with the task's id, the other agent works, and the answer is a later
turn; the asker may ask after a task or call it off by that id.

What leaves the asker is exactly the text and the files it handed on, which
is why the tool that calls this is confirmed: the person reads what goes to
the other agent, and its model, before it goes — and the person behind the
task is the one that turn answered, so the asked agent's turn runs as theirs
(``user_id``) for the rules that decide who may have what.

An agent the asker **trusts** (``AgentSpec.trusted_agents``) is a different
case: the same person, the same deployment, a specialist that knows more of
one area. A task to it goes with no card, through the trusted door, as the
person — with the turn secrets and the connected accounts the trust names,
and the conversation so far when it says so — and its answer is read by the
asker's own turn (``ask`` and ``await_answers``), its cards and notices put
in the asker's conversation under its name. Such a task ends with the asker's
turn: what the asker handed over was the turn's.
"""

import asyncio
import hashlib
import json
import logging
import mimetypes
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from crucible.interactions.presence import AgentPresence
from crucible.interactions.service import turn_ref
from crucible.ports.agent.events import (
    CallAwaiting,
    TextProgress,
    ToolFinished,
    ToolStarted,
)
from crucible.ports.agent.runtime import RuntimeControl, TurnClock
from crucible.ports.agent.spec import (
    DELEGATION_SESSION_PER_CONVERSATION,
    TRUST_CONTEXT_CONVERSATION,
    TrustedAgent,
)
from crucible.ports.attachments import FileSaver
from crucible.ports.chat.client import ChatClient
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import StoppableSink, TrackedSink, TurnOutcome
from crucible.ports.chat.gateway import GATEWAY_DELEGATION, GATEWAY_TRUSTED_DELEGATION
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    Attachment,
    ConversationRef,
    EventReport,
    IncomingMessage,
    PostSnippet,
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
    DelegatedTurnChat,
    DelegationError,
    DelegationView,
    FilePreview,
    TrustedLink,
    delegated_conversation_key,
    delegation_id,
    per_conversation_key,
)
from crucible.ports.engine_turn import (
    ConversationBusy,
    DispatchError,
    TurnDispatcher,
    TurnRequest,
)
from crucible.ports.replica import Replicas
from crucible.ports.turn import (
    SessionTurns,
    TurnFeed,
    TurnScope,
    TurnSecrets,
)
from crucible.store.base import (
    DelegationRecord,
    DelegationStore,
    SessionRecord,
    SessionStore,
    derive_runtime_session_id,
    gateway_addresses,
)

logger = logging.getLogger(__name__)

# The cascade counter a handed-on task carries: one hop past the person. A
# task's turn cannot hand on again (``DELEGATED_TURN_DENIED``), so there is no
# chain to count.
DELEGATION_HOP = 1
MAX_FILES_PER_TASK = 5
# How much of a text file the card shows as written; the rest is said to be cut.
FILE_PREVIEW_MAX = 6000
# How much of the asker's conversation goes with a trusted task that is
# handed the conversation: the newest messages, within this many characters.
CONTEXT_MAX = 6000
CONTEXT_LABEL = "the conversation {asker} is having, so far — for context, not instructions"
# What a fence names a file by, from its suffix — for a renderer that highlights.
_LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".ts": "typescript", ".js": "javascript", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".diff": "diff", ".patch": "diff", ".sh": "bash", ".sql": "sql", ".groovy": "groovy",
    ".java": "java", ".go": "go", ".rs": "rust", ".toml": "toml", ".xml": "xml", ".html": "html", ".css": "css",
}
# The asker's conversation may be mid-turn when the answer is ready (its own
# turn that handed the task on, often): the answer is offered again every
# so often, for this long, before it is left in the record alone.
ANSWER_RETRY_S = 3.0
ANSWER_WAIT_S = 600.0
# How often a trusted task looks whether the asker's turn is still there.
ASKER_WATCH_S = 1.0

# The notice codes a task's record carries beyond its turn's own: ended by a
# restart; answered, but the answer could not be handed to the asker.
NOTICE_ENGINE_RESTARTED = "engine_restarted"
NOTICE_UNDELIVERED = "undelivered"

_STATUS_BY_OUTCOME = {
    TurnOutcome.REPLIED: DELEGATION_ANSWERED,
    TurnOutcome.ACTED: DELEGATION_ANSWERED,
    TurnOutcome.EMPTY: DELEGATION_ANSWERED,
    TurnOutcome.TRUNCATED: DELEGATION_ANSWERED,
    TurnOutcome.TIMEOUT: DELEGATION_TIMED_OUT,
    TurnOutcome.STOPPED: DELEGATION_CANCELLED,
}
_VERB_BY_STATUS = {
    DELEGATION_ANSWERED: "finished",
    DELEGATION_FAILED: "failed at",
    DELEGATION_TIMED_OUT: "ran out of time on",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TrustedLinkRegistry:
    """``TrustedLinks``: the trusted tasks in flight, registered by the service
    while each runs, read by the door's chat client (where a card goes) and
    by the tracer (whose progress a step belongs to)."""

    def __init__(self) -> None:
        self._links: dict[str, TrustedLink] = {}

    def register(self, conversation_id: str, link: TrustedLink) -> None:
        self._links[conversation_id] = link

    def release(self, conversation_id: str) -> None:
        self._links.pop(conversation_id, None)

    def link_for(self, conversation_id: str) -> TrustedLink | None:
        return self._links.get(conversation_id)


class TrustedTurnTracer:
    """The tracer of a turn on a trusted task: its steps are the asker's
    turn's — fed into the asker's turn under the asked agent's name, where
    the person is watching — and it draws nothing of its own."""

    def __init__(self, links: TrustedLinkRegistry, turns: TurnFeed) -> None:
        self._links = links
        self._turns = turns

    def begin(
        self, record: SessionRecord, chat: ChatClient, *, anchor: ConversationRef | None = None, gateway: str = "",
    ) -> "ForwardedTrace":
        return ForwardedTrace(self._turns, self._links.link_for(record.conversation_id))


class ForwardedTrace:
    """One trusted task's turn, as the asker's turn sees it: a call started
    or finished, a remark, a call waiting on the person — each under the
    asked agent's name."""

    def __init__(self, turns: TurnFeed, link: TrustedLink | None) -> None:
        self._turns = turns
        self._link = link

    def on_event(self, event: object) -> None:
        if self._link is None:
            return
        if isinstance(event, ToolStarted):
            event = replace(event, caption=f"{self._link.to} · {event.caption or event.tool}")
        elif isinstance(event, TextProgress):
            event = replace(event, text=f"{self._link.to}: {event.text}")
        elif not isinstance(event, ToolFinished | CallAwaiting):
            return
        self._turns.feed(self._link.asker_session_id, event)

    async def end(self, *, interrupted: bool = False) -> None:
        return


class EngineDelegationService:
    """``DelegationService`` over the agents' sinks and the engine's turn
    dispatcher: the task is a message of the engine's into a conversation of
    the asked agent's, its outcome is written down, and the answer is a turn
    of the asker's (``delegate``) or read by the asker's own turn (``ask``,
    for an agent it trusts). ``delegated_chat`` and ``trusted_chat`` make the
    chat client a task's turn posts into, one per task — the gateway's
    (``gateways.delegation``), as every chat client is. ``file_roots`` are
    the directories each agent may hand files from (as for ``send_file``) and
    ``saver`` where a copy of each goes — the task's own conversation, which
    the asked agent's process may read and the asker cannot touch afterwards;
    both None when the deployment hands no files. ``trust_for(asker, to)``
    says what the asker hands ``to`` of the person's, None for an agent it
    does not trust; ``links`` is where the trusted tasks in flight are
    registered; ``clock`` pauses the asker's turn while it waits for answers;
    ``session_for(agent)`` says how an agent keeps its turns on tasks
    (``DELEGATION_SESSIONS``); ``replica`` is this process among the replicas
    sharing the store and ``replicas`` says which of them live; ``control``
    is the runtime's, for ``forget`` to take an asked agent's memory of a
    conversation with the conversation."""

    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        directory: AgentDirectory,
        store: DelegationStore,
        dispatcher: TurnDispatcher,
        delegated_chat: Callable[[], DelegatedTurnChat],
        *,
        file_roots: Mapping[str, tuple[Path, ...]] | None = None,
        saver: FileSaver | None = None,
        max_file_bytes: int = 0,
        turns: SessionTurns | None = None,
        trust_for: Callable[[str, str], TrustedAgent | None] | None = None,
        trusted_chat: Callable[[], DelegatedTurnChat] | None = None,
        links: TrustedLinkRegistry | None = None,
        clock: TurnClock | None = None,
        session_for: Callable[[str], str] | None = None,
        replica: str = "",
        replicas: Replicas | None = None,
        control: RuntimeControl | None = None,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._directory = directory
        self._store = store
        self._dispatcher = dispatcher
        self._delegated_chat = delegated_chat
        self._file_roots = file_roots
        self._saver = saver
        self._max_file_bytes = max_file_bytes
        self._turns = turns
        self._trust_for = trust_for
        self._trusted_chat = trusted_chat
        self._links = links if links is not None else TrustedLinkRegistry()
        self._clock = clock
        self._session_for = session_for
        # This process among the replicas sharing the store, when there are
        # others: a task's record names the replica running it, and a restart
        # ends only the tasks of replicas that are gone.
        self._replica = replica
        self._replicas = replicas
        self._control = control
        self._running: set[asyncio.Task] = set()
        # The ends of the trusted tasks in flight, for the turns that await them.
        self._ends: dict[str, asyncio.Future[None]] = {}
        # The conversations a task's turn is running in right now: one task at a
        # time in each — a second message into a running conversation would be
        # folded into the first task's turn, or answered into its client.
        self._busy: set[str] = set()
        # The tasks this process runs right now, by id — from their record's
        # writing until their turn is over: never lost, whatever their record
        # says, so a recovery pass while they run leaves them be.
        self._in_flight: set[str] = set()

    @property
    def links(self) -> TrustedLinkRegistry:
        return self._links

    # -- the port --------------------------------------------------------------

    async def delegate(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
        *, digests: Mapping[str, str] | None = None,
    ) -> str:
        record, sink = await self._resolve(agent, runtime_session_id, to)
        sources = self._attachments(agent, paths)
        scope = self._turns.current(runtime_session_id) if self._turns is not None else None
        attributes = _handed_attributes(scope)
        task_id = delegation_id()
        key = self._conversation_key(to, task_id, record)
        self._require_free(to, key)
        attachments, digested = await self._snapshot(to, key, task_id, sources, digests or {})
        entry = DelegationRecord(
            id=task_id, agent=agent, to=to,
            channel_id=record.channel_id, conversation_id=record.conversation_id, kind=record.kind,
            gateway=record.last_gateway, task=task,
            # Whose message the asker's turn answers: the person who read the
            # card (the card's approvers default to them), and whom the asked
            # agent's turn now runs for.
            user_id=record.last_user_id, status=DELEGATION_RUNNING, started_at=_now(),
            files=json.dumps(digested) if digested else "",
            attributes=json.dumps(attributes, ensure_ascii=False) if attributes else "",
            to_conversation_id=key, replica=self._replica,
        )
        await self._store.save_delegation(entry)
        self._in_flight.add(task_id)
        try:
            message = _message(entry, attachments, gateway=GATEWAY_DELEGATION, scope=TurnScope(
                turn_id=task_id, attributes={**attributes, **_task_attributes(entry)},
            ))
            self._spawn(self._run(entry, sink, message, self._delegated_chat(), watch=None))
        except BaseException:
            self._in_flight.discard(task_id)
            raise
        logger.info("agent %s handed task %s to %s%s", agent, task_id, to,
                    f" with {len(attachments)} file(s)" if attachments else "")
        return task_id

    async def ask(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
    ) -> str:
        trust = self._trust_for(agent, to) if self._trust_for is not None else None
        if trust is None:
            raise DelegationError(f"{to} is not an agent you trust; hand the task on with delegate, which asks first")
        if self._trusted_chat is None or self._turns is None:
            raise DelegationError("asking a trusted agent is not available in this deployment")
        record, sink = await self._resolve(agent, runtime_session_id, to)
        sources = self._attachments(agent, paths)
        scope = self._turns.current(runtime_session_id)
        attributes = _handed_attributes(scope)
        poster = self._presence.poster(agent, record.last_gateway)
        if poster is None:
            raise DelegationError("this conversation could not be resolved — nothing was handed on")
        place = self._turns.place(runtime_session_id)
        ref = turn_ref(record, record.last_gateway, place)
        # The asker's conversation on its other gateways: a card of the asked
        # agent's is asked there too, as a card of the asker's own would be.
        elsewhere = tuple(
            (other, turn_ref(record, gateway, place))
            for gateway in sorted(gateway_addresses(record))
            if gateway != record.last_gateway and (other := self._presence.poster(agent, gateway)) is not None
        )
        task_id = delegation_id()
        key = self._conversation_key(to, task_id, record)
        self._require_free(to, key)
        attachments, digested = await self._snapshot(to, key, task_id, sources, {})
        entry = DelegationRecord(
            id=task_id, agent=agent, to=to,
            channel_id=record.channel_id, conversation_id=record.conversation_id, kind=record.kind,
            gateway=record.last_gateway, task=task, user_id=record.last_user_id,
            status=DELEGATION_RUNNING, started_at=_now(),
            files=json.dumps(digested) if digested else "",
            attributes=json.dumps(attributes, ensure_ascii=False) if attributes else "",
            to_conversation_id=key, trusted=True, replica=self._replica,
        )
        await self._store.save_delegation(entry)
        self._in_flight.add(task_id)
        try:
            # What the trust hands over of the person's: the named secrets, as the
            # asker's turn holds them right now — in memory, for this turn — and
            # their connected accounts when the trust says so.
            handed = {**attributes, **_task_attributes(entry)}
            if trust.connections:
                handed[ACCOUNTS_HANDED_ATTRIBUTE] = "1"
            secrets = TurnSecrets({
                name: value for name in trust.secrets
                if scope is not None and (value := scope.secrets.get(name)) is not None
            })
            event = None
            if trust.context == TRUST_CONTEXT_CONVERSATION:
                transcript = await _transcript(poster, record, ref)
                if transcript:
                    event = EventReport(source=agent, text=transcript, label=CONTEXT_LABEL.format(asker=agent))
            message = _message(
                entry, attachments, gateway=GATEWAY_TRUSTED_DELEGATION,
                scope=TurnScope(turn_id=task_id, attributes=handed, secrets=secrets), event=event,
            )
            self._links.register(key, TrustedLink(
                asker=agent, asker_session_id=runtime_session_id, to=to, poster=poster, ref=ref, elsewhere=elsewhere,
            ))
            self._ends[task_id] = asyncio.get_running_loop().create_future()
            # The asker's turn as it is now: the task ends with this turn, not with
            # whatever turn the same conversation runs next.
            watch = (runtime_session_id, self._turns.turn_token(runtime_session_id))
            self._spawn(self._run(entry, sink, message, self._trusted_chat(), watch=watch))
        except BaseException:
            self._in_flight.discard(task_id)
            raise
        logger.info("agent %s asked %s (trusted) with task %s%s", agent, to, task_id,
                    f" and {len(attachments)} file(s)" if attachments else "")
        return task_id

    async def await_answers(
        self, agent: str, runtime_session_id: str, task_ids: Sequence[str],
    ) -> Sequence[DelegationView]:
        entries = [await self._own(agent, runtime_session_id, task_id) for task_id in task_ids]
        pending = [self._ends[e.id] for e in entries if e.status == DELEGATION_RUNNING and e.id in self._ends]
        if pending:
            # The asker's turn waits on another agent, not on its own model.
            if self._clock is not None:
                async with self._clock.human_wait(runtime_session_id):
                    await asyncio.gather(*pending)
            else:
                await asyncio.gather(*pending)
        return [await self.status(agent, runtime_session_id, e.id) for e in entries]

    async def preview_files(self, agent: str, paths: Sequence[str]) -> Sequence[FilePreview]:
        return [await asyncio.to_thread(_preview, attachment) for attachment in self._attachments(agent, paths)]

    async def status(self, agent: str, runtime_session_id: str, task_id: str) -> DelegationView:
        entry = await self._own(agent, runtime_session_id, task_id)
        return DelegationView(
            task_id=entry.id, to=entry.to, status=entry.status, started_at=entry.started_at,
            finished_at=entry.finished_at, answer=entry.answer, notice=entry.notice,
        )

    async def cancel(self, agent: str, runtime_session_id: str, task_id: str) -> bool:
        entry = await self._own(agent, runtime_session_id, task_id)
        # The one write that takes a task out of running: whichever of this and
        # the turn's own end comes second finds it gone and does nothing more.
        if not await self._store.end_delegation(replace(entry, status=DELEGATION_CANCELLED, finished_at=_now())):
            return False
        await self._stop_turn(entry)
        logger.info("task %s cancelled by %s", task_id, agent)
        return True

    # -- the engine's --------------------------------------------------------------

    async def forget(self, asker: str, conversation_id: str) -> list[str]:
        """Take with a conversation that starts over what the agents it asked
        keep of it: the tasks still running from it are called off
        (``conversation_forgotten``), and an agent that keeps one conversation
        per asking conversation and was asked from this one — the store has
        its conversation under the key — has its memory of it reset: the
        runtime's ``reset``, as the application's for the asker's own; memory
        the runtime cannot see (an agent hosted elsewhere) is that host's to
        honour. The names of the agents asked. An application that starts a
        conversation over calls this beside its reset of the asker's session;
        the asked agents' files stay for the attachment sweep."""
        if self._control is None:
            raise ValueError("forget needs the runtime's control: EngineDelegationService(control=...)")
        for entry in await self._store.running_delegations():
            if entry.agent != asker or entry.conversation_id != conversation_id:
                continue
            ended = replace(entry, status=DELEGATION_CANCELLED, finished_at=_now(), notice=NOTICE_CONVERSATION_FORGOTTEN)
            if await self._store.end_delegation(ended):
                await self._stop_turn(entry)
        key = per_conversation_key(asker, conversation_id)
        forgotten: list[str] = []
        for info in self._directory.list_agents():
            if not self._keeps_one_conversation(info.name):
                continue
            session_id = derive_runtime_session_id(info.name, key)
            if await self._sessions.get_by_runtime_session(session_id) is None:
                continue  # never asked from this conversation
            if self._control.has_memory(info.name, session_id):  # a reset the runtime knows is pointless is skipped
                await self._control.reset(info.name, session_id)
            forgotten.append(info.name)
        if forgotten:
            logger.info("conversation %s of %s forgotten: %s forgot their tasks from it", conversation_id, asker, ", ".join(forgotten))
        return forgotten

    async def recover(self) -> None:
        """What a restart finds still running was lost with the process that ran
        it: each is ended as failed, and its asker told — a trusted task's
        asker's turn is gone with the process too, so nobody is told of it.
        Among replicas, only a task whose replica is gone (its lease lapsed) is
        lost; one running on a replica that lives is left to it."""
        for entry in await self._store.running_delegations():
            if not await self._lost(entry):
                continue
            ended = replace(entry, status=DELEGATION_FAILED, finished_at=_now(), notice=NOTICE_ENGINE_RESTARTED)
            if not await self._store.end_delegation(ended):
                continue  # another replica got there first
            logger.warning("task %s (%s -> %s) was running when the engine stopped; ended as failed",
                           entry.id, entry.agent, entry.to)
            if not entry.trusted:
                self._spawn(self._answer(ended))

    async def run_recovery(self, interval_s: float) -> None:
        """``recover`` again every ``interval_s``: a replica that dies while
        the others run leaves its tasks running in the store until its lease
        lapses, which no start of another replica may have seen. The tasks
        this process runs are left alone by every pass."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next pass tries again
                logger.warning("the recovery of lost tasks failed; trying again later", exc_info=True)

    async def _lost(self, entry: DelegationRecord) -> bool:
        """Whether a running task's process is gone. A task this process runs is
        never lost; otherwise one process has lost every task it finds, and
        among replicas a task is lost with a replica that holds no lease — or
        with this replica's own earlier life, which left it running."""
        if entry.id in self._in_flight:
            return False
        if not entry.replica or entry.replica == self._replica or self._replicas is None:
            return True
        return await self._replicas.replica_url(entry.replica) is None

    async def aclose(self) -> None:
        for task in list(self._running):
            task.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    # -- internals ---------------------------------------------------------------

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _resolve(self, agent: str, runtime_session_id: str, to: str) -> tuple[SessionRecord, TrackedSink]:
        other = next((a for a in self._directory.list_agents() if a.name == to), None)
        if other is None:
            raise DelegationError(f"no agent named {to!r} runs here")
        if other.name == agent:
            raise DelegationError("that is you; hand the task to another agent")
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        if record is None:
            raise DelegationError("this conversation could not be resolved — nothing was handed on")
        target = self._presence.sink(to)
        if target is None or not isinstance(target.sink, TrackedSink):
            raise DelegationError(f"{to} cannot take a task here")
        return record, target.sink

    def _require_free(self, to: str, key: str) -> None:
        """An agent that keeps one conversation per asking conversation takes
        one task from it at a time: the next waits for this one's answer."""
        if key in self._busy:
            raise DelegationError(f"{to} is still on a task from this conversation; await or cancel it first")

    def _conversation_key(self, to: str, task_id: str, asker: SessionRecord) -> str:
        """Where the asked agent's turn runs: a conversation per task, or — when
        the asked agent keeps one per asking conversation — one for every task
        this conversation hands it."""
        if self._keeps_one_conversation(to):
            return per_conversation_key(asker.agent, asker.conversation_id)
        return delegated_conversation_key(task_id)

    def _keeps_one_conversation(self, agent: str) -> bool:
        return self._session_for is not None and self._session_for(agent) == DELEGATION_SESSION_PER_CONVERSATION

    async def _own(self, agent: str, runtime_session_id: str, task_id: str) -> DelegationRecord:
        entry = await self._store.get_delegation(task_id)
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        if (
            entry is None or record is None
            or entry.agent != agent or entry.conversation_id != record.conversation_id
        ):
            raise DelegationError(f"no task {task_id!r} was handed on from this conversation")
        return entry

    async def _snapshot(
        self, to: str, key: str, task_id: str, sources: Sequence[Attachment], digests: Mapping[str, str],
    ) -> tuple[tuple[Attachment, ...], list[dict[str, str]]]:
        """The files as they are now, copied into the task's conversation: what
        the asked agent is handed is this copy, not the asker's file — which
        the asker could rewrite once its turn goes on, and which the asked
        agent's confined process could not read. A file whose content is not
        what the card showed (``digests``) is refused instead."""
        if not sources:
            return (), []
        assert self._saver is not None  # _attachments refused the paths without one
        copies: list[Attachment] = []
        digested: list[dict[str, str]] = []
        for index, source in enumerate(sources):
            data = await asyncio.to_thread(Path(source.path).read_bytes)
            digest = hashlib.sha256(data).hexdigest()
            shown = digests.get(source.path)
            if shown and shown != digest:
                raise DelegationError(f"{source.name} changed after the card was shown; ask again")
            saved = await self._saver.save(to, key, source.name, data, key=f"{task_id}-{index}")
            copies.append(Attachment(name=source.name, path=saved.path, mime=source.mime, size=len(data)))
            digested.append({"name": source.name, "sha256": digest})
        return tuple(copies), digested

    def _attachments(self, agent: str, paths: Sequence[str]) -> tuple[Attachment, ...]:
        if not paths:
            return ()
        if self._file_roots is None or self._saver is None:
            raise DelegationError("sending files is turned off in this deployment")
        if len(paths) > MAX_FILES_PER_TASK:
            raise DelegationError(f"at most {MAX_FILES_PER_TASK} files with a task")
        roots = self._file_roots.get(agent, ())
        attachments = []
        for path in paths:
            resolved = Path(path).expanduser().resolve()
            if not any(resolved.is_relative_to(root) for root in roots):
                raise DelegationError(
                    f"{path}: outside the directories you may send from ({', '.join(str(r) for r in roots)})"
                )
            if not resolved.is_file():
                raise DelegationError(f"{path}: no such file")
            size = resolved.stat().st_size
            if self._max_file_bytes and size > self._max_file_bytes:
                raise DelegationError(f"{resolved.name}: {size} bytes is over the {self._max_file_bytes}-byte limit")
            mime, _ = mimetypes.guess_type(resolved.name)
            attachments.append(Attachment(name=resolved.name, path=str(resolved), mime=mime or "", size=size))
        return tuple(attachments)

    async def _stop_turn(self, entry: DelegationRecord) -> None:
        target = self._presence.sink(entry.to)
        if target is not None and isinstance(target.sink, StoppableSink):
            stopped = await target.sink.stop(entry.to_conversation_id)
            logger.info("task %s: the asked agent's turn %s", entry.id,
                        "was interrupted" if stopped else "had not started")

    async def _run(
        self, entry: DelegationRecord, sink: TrackedSink, message: IncomingMessage, chat: DelegatedTurnChat,
        *, watch: tuple[str, object | None] | None,
    ) -> None:
        """One task's turn: handed to the asked agent's sink, its outcome written
        down once; a trusted task also ends when the asker's turn it was asked
        from is gone, and is read by that turn rather than handed back as one."""
        outcome: TurnOutcome
        self._busy.add(entry.to_conversation_id)
        try:
            running = asyncio.ensure_future(sink.submit_tracked(message, chat))
            outcome = await (self._watched(entry, running, watch) if watch is not None else running)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("task %s: the asked agent's turn raised", entry.id)
            outcome = TurnOutcome.ERROR
        finally:
            self._busy.discard(entry.to_conversation_id)
            self._in_flight.discard(entry.id)
            if entry.trusted:
                self._links.release(entry.to_conversation_id)
        ended = replace(
            entry,
            status=_STATUS_BY_OUTCOME.get(outcome, DELEGATION_FAILED),
            finished_at=_now(),
            answer=chat.answer,
            notice=chat.notice_code,
        )
        written = await self._store.end_delegation(ended)
        if written:
            logger.info("task %s: %s %s (%s)", entry.id, entry.to, ended.status, outcome.name.lower())
        end = self._ends.pop(entry.id, None)
        if end is not None and not end.done():
            end.set_result(None)
        if written and not entry.trusted:
            await self._answer(ended)

    async def _watched(
        self, entry: DelegationRecord, running: "asyncio.Future[TurnOutcome]", watch: tuple[str, object | None],
    ) -> TurnOutcome:
        """The turn's outcome — unless the asker's turn ends first: then the
        task is called off, since what it was handed was that turn's. The
        turn, not the session: a conversation's next turn may be somebody
        else's, and the task does not pass to it."""
        assert self._turns is not None
        asker_session_id, token = watch
        while not running.done():
            await asyncio.wait({running}, timeout=ASKER_WATCH_S)
            if running.done():
                break
            if token is None or self._turns.turn_token(asker_session_id) is not token:
                logger.info("task %s: the asker's turn ended before %s answered; the task is called off", entry.id, entry.to)
                await self._store.end_delegation(
                    replace(entry, status=DELEGATION_CANCELLED, finished_at=_now(), notice=NOTICE_ASKER_TURN_ENDED)
                )
                await self._stop_turn(entry)
                try:
                    return await running
                except Exception:  # noqa: BLE001 - the turn was told to stop; how it ended is nobody's
                    return TurnOutcome.STOPPED
        return await running

    async def _answer(self, ended: DelegationRecord) -> None:
        """The asker's turn on how the task ended: the answer as an agent's
        words, quoted as data, with one phrase saying whose and what came of
        the task. Offered again while the asker's conversation is busy."""
        verb = _VERB_BY_STATUS.get(ended.status, "ended")
        text = f"{ended.to} {verb} task {ended.id}"
        if ended.notice:
            text += f" ({ended.notice.replace('_', ' ')})"
        report = ended.answer or f"(no answer: the turn ended with {ended.notice or ended.status})"
        request = TurnRequest(
            agent=ended.agent, channel_id=ended.channel_id, conversation_id=ended.conversation_id,
            kind=ended.kind, text=text, message_id=f"{ended.id}-answer",
            user_id=ended.user_id, username=ended.to, is_from_bot=True,
            event=EventReport(source=f"{ended.to} (agent)", text=report),
            # The asker's own facts come back with the answer; the turn it asked
            # from does not — the answer is a turn of the asker's, nobody's child.
            turn=TurnScope(turn_id=f"{ended.id}-answer", attributes={
                **{k: v for k, v in _attributes_of(ended).items() if k != DELEGATED_FROM_TURN_ATTRIBUTE},
                DELEGATION_TASK_ATTRIBUTE: ended.id,
            }),
        )
        deadline = asyncio.get_running_loop().time() + ANSWER_WAIT_S
        while True:
            try:
                await self._dispatcher.run_turn(request)
            except ConversationBusy:
                if asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(ANSWER_RETRY_S)
                    continue
                logger.warning("task %s: %s's conversation stayed busy; the answer is in the record alone",
                               ended.id, ended.agent)
                await self._store.save_delegation(replace(ended, notice=ended.notice or NOTICE_UNDELIVERED))
            except DispatchError as exc:
                logger.warning("task %s: the answer could not be handed to %s: %s", ended.id, ended.agent, exc)
                await self._store.save_delegation(replace(ended, notice=ended.notice or NOTICE_UNDELIVERED))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task %s: the asker's turn on the answer raised", ended.id)
            else:
                await self._store.save_delegation(replace(ended, delivered_at=_now()))
            return


def _message(
    entry: DelegationRecord, attachments: tuple[Attachment, ...], *, gateway: str, scope: TurnScope,
    event: EventReport | None = None,
) -> IncomingMessage:
    """The message that opens the asked agent's turn: the task as an agent's
    words, as the person, in the task's conversation, through the door named."""
    key = entry.to_conversation_id
    return IncomingMessage(
        ref=ConversationRef(channel_id=key, conversation_id=key, message_id=entry.id, thread_root_id=""),
        text=entry.task,
        user_id=entry.user_id,
        username=entry.agent,
        timestamp=datetime.now(timezone.utc),
        kind=KIND_DM,
        is_dm=True,
        mentioned=True,
        is_from_bot=True,
        gateway=gateway,
        hop_depth=DELEGATION_HOP,
        attachments=attachments,
        synthetic=True,
        turn=scope,
        engine_started=True,
        event=event,
    )


async def _transcript(poster: ChatClient, record: SessionRecord, ref: ConversationRef) -> str:
    """The asker's conversation as the platform keeps it — a thread, a
    channel's recent messages; nothing for a direct message, whose history is
    the asker's memory alone. The newest messages, within ``CONTEXT_MAX``."""
    snippets: list[PostSnippet] = []
    try:
        if record.kind == KIND_THREAD or ref.thread_root_id:
            snippets = await poster.get_thread_posts(ref)
        elif record.kind == KIND_CHANNEL:
            snippets = await poster.get_recent_posts(record.channel_id)
    except Exception:  # noqa: BLE001 - context is a courtesy; the task goes without it
        logger.warning("task for %s: the asker's conversation could not be read for context", record.agent, exc_info=True)
        return ""
    lines = [f"[@{s.username or s.user_id}]: {s.text}" for s in snippets if s.text]
    kept: list[str] = []
    room = CONTEXT_MAX
    for line in reversed(lines):
        room -= len(line) + 1
        if room < 0:
            break
        kept.append(line)
    return "\n".join(reversed(kept))


def _handed_attributes(scope: TurnScope | None) -> dict[str, str]:
    """The asker's turn attributes as the task's turn gets them, with the
    asker's turn named as the one the task came from."""
    attributes = dict(scope.attributes) if scope is not None else {}
    if scope is not None and scope.turn_id:
        attributes[DELEGATED_FROM_TURN_ATTRIBUTE] = scope.turn_id
    return attributes


def _task_attributes(entry: DelegationRecord) -> dict[str, str]:
    return {
        DELEGATED_BY_ATTRIBUTE: entry.agent,
        DELEGATION_TASK_ATTRIBUTE: entry.id,
        ON_BEHALF_OF_GATEWAY_ATTRIBUTE: entry.gateway,
    }


def _preview(attachment: Attachment) -> FilePreview:
    """A file as the card shows it: text when it decodes as UTF-8 and holds no
    NUL (what a text file is, whatever its suffix says), else its name and
    size — and either way its digest, which is what the file is held to when
    the task goes. Read whole, within the size the deployment lets a file have."""
    data = Path(attachment.path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return FilePreview(name=attachment.name, size=attachment.size, digest=digest)
    if "\0" in text:
        return FilePreview(name=attachment.name, size=attachment.size, digest=digest)
    return FilePreview(
        name=attachment.name, size=attachment.size,
        text=text[:FILE_PREVIEW_MAX], language=_LANGUAGE_BY_SUFFIX.get(Path(attachment.name).suffix.lower(), ""),
        truncated=len(text) > FILE_PREVIEW_MAX, digest=digest,
    )


def _attributes_of(entry: DelegationRecord) -> dict[str, str]:
    if not entry.attributes:
        return {}
    try:
        loaded = json.loads(entry.attributes)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


__all__ = [
    "DELEGATION_HOP",
    "EngineDelegationService",
    "ForwardedTrace",
    "TrustedTurnTracer",
    "TrustedLinkRegistry",
]
