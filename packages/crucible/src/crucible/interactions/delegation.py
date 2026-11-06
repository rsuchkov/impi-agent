"""Handing a task to another agent, through the engine.

An agent that wants another's work — one whose model is better at the task,
or one that is allowed what the asker is not — does not reach into that
agent, and the two need no chat in common: the engine opens a turn of the
other agent on the task alone, in a conversation of its own
(``delegated:<task id>``, on the engine's own gateway) that holds nothing but
the task and its files — no thread to replay, no memory of an earlier task —
and when that turn ends, a turn of the asker's with the answer, quoted as an
agent's words. Asynchronous by design: the tool call ends with the task's
id, the other agent works, and the answer is a later turn; the asker may ask
after a task or call it off by that id.

What leaves the asker is exactly the text and the files it handed on, which
is why the tool that calls this is confirmed: the person reads what goes to
the other agent, and its model, before it goes — and the person behind the
task is the one that turn answered, so the asked agent's turn runs as theirs
(``user_id``) for the rules that decide who may have what.
"""

import asyncio
import json
import logging
import mimetypes
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from crucible.interactions.presence import AgentPresence
from crucible.ports.chat.directory import AgentDirectory
from crucible.ports.chat.flow import StoppableSink, TrackedSink, TurnOutcome
from crucible.ports.chat.gateway import GATEWAY_DELEGATION
from crucible.ports.chat.types import (
    KIND_DM,
    Attachment,
    ConversationRef,
    EventReport,
    IncomingMessage,
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
    TaskTurnChat,
    delegated_conversation,
    delegated_task_id,
)
from crucible.ports.engine_turn import (
    ConversationBusy,
    DispatchError,
    TurnDispatcher,
    TurnRequest,
)
from crucible.ports.turn import TurnScope, TurnScopes
from crucible.store.base import DelegationRecord, DelegationStore, SessionStore

logger = logging.getLogger(__name__)

# The cascade counter a handed-on task starts at: one hop past the person.
DELEGATION_HOP = 1
# How many times a task may be handed on: an agent asked may ask another, and
# that one another, and there it stops — a chain longer than that is a loop
# with extra steps, whoever read the cards.
DELEGATION_MAX_DEPTH = 3
MAX_FILES_PER_TASK = 5
# The asker's conversation may be mid-turn when the answer is ready (its own
# turn that handed the task on, often): the answer is offered again every
# so often, for this long, before it is left in the record alone.
ANSWER_RETRY_S = 3.0
ANSWER_WAIT_S = 600.0

# The notice codes a task ends with beyond its turn's own.
NOTICE_ENGINE_RESTARTED = "engine_restarted"
NOTICE_CANNOT_TAKE = "cannot_take"
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


class EngineDelegationService:
    """``DelegationService`` over the agents' sinks and the engine's turn
    dispatcher: the task is a message of the engine's into a conversation of
    the asked agent's that exists for this task alone, its outcome is written
    down, and the answer is a turn of the asker's. ``task_chat`` makes the
    chat client that turn posts into, one per task — the gateway's
    (``gateways.delegation``), as every chat client is. ``file_roots`` are
    the directories each agent may hand files from (as for ``send_file``);
    None when the deployment hands no files."""

    def __init__(
        self,
        presence: AgentPresence,
        sessions: SessionStore,
        directory: AgentDirectory,
        store: DelegationStore,
        dispatcher: TurnDispatcher,
        task_chat: Callable[[], TaskTurnChat],
        *,
        file_roots: Mapping[str, tuple[Path, ...]] | None = None,
        max_file_bytes: int = 0,
        turns: TurnScopes | None = None,
    ) -> None:
        self._presence = presence
        self._sessions = sessions
        self._directory = directory
        self._store = store
        self._dispatcher = dispatcher
        self._task_chat = task_chat
        self._file_roots = file_roots
        self._max_file_bytes = max_file_bytes
        self._turns = turns
        self._running: set[asyncio.Task] = set()

    # -- the port --------------------------------------------------------------

    async def delegate(
        self, agent: str, runtime_session_id: str, to: str, task: str, paths: Sequence[str] = (),
    ) -> str:
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
        depth = await self._depth(record.conversation_id)
        if depth > DELEGATION_MAX_DEPTH:
            raise DelegationError(
                f"this task has already been handed on {DELEGATION_MAX_DEPTH} times; do it yourself or answer"
            )
        attachments = self._attachments(agent, paths)
        scope = self._turns.current(runtime_session_id) if self._turns is not None else None
        attributes = dict(scope.attributes) if scope is not None else {}
        task_id = delegated_task_id()
        entry = DelegationRecord(
            id=task_id, agent=agent, to=to,
            channel_id=record.channel_id, conversation_id=record.conversation_id, kind=record.kind,
            gateway=record.last_gateway, task=task,
            # Whose message the asker's turn answers: the person who read the
            # card (the card's approvers default to them), and whom the asked
            # agent's turn now runs for.
            user_id=record.last_user_id, status=DELEGATION_RUNNING, started_at=_now(),
            files=json.dumps([a.name for a in attachments]) if attachments else "",
            attributes=json.dumps(attributes, ensure_ascii=False) if attributes else "",
            hop_depth=depth,
        )
        await self._store.save_delegation(entry)
        self._spawn(self._run(entry, target.sink, attachments))
        logger.info("agent %s handed task %s to %s%s", agent, task_id, to,
                    f" with {len(attachments)} file(s)" if attachments else "")
        return task_id

    async def status(self, agent: str, runtime_session_id: str, task_id: str) -> DelegationView:
        entry = await self._own(agent, runtime_session_id, task_id)
        return DelegationView(
            task_id=entry.id, to=entry.to, status=entry.status, started_at=entry.started_at,
            finished_at=entry.finished_at, answer=entry.answer, notice=entry.notice,
        )

    async def cancel(self, agent: str, runtime_session_id: str, task_id: str) -> bool:
        entry = await self._own(agent, runtime_session_id, task_id)
        if entry.status != DELEGATION_RUNNING:
            return False
        # Written first, so a turn that ends while this runs sees it was
        # called off and hands nothing back.
        await self._store.save_delegation(replace(entry, status=DELEGATION_CANCELLED, finished_at=_now()))
        target = self._presence.sink(entry.to)
        if target is not None and isinstance(target.sink, StoppableSink):
            stopped = await target.sink.stop(delegated_conversation(task_id))
            logger.info("task %s cancelled by %s (%s)", task_id, agent,
                        "its turn was interrupted" if stopped else "its turn had not started")
        return True

    # -- the engine's --------------------------------------------------------------

    async def recover(self) -> None:
        """What a restart finds still running was lost with the process that ran
        it: each is ended as failed, and its asker told so."""
        for entry in await self._store.running_delegations():
            ended = replace(entry, status=DELEGATION_FAILED, finished_at=_now(), notice=NOTICE_ENGINE_RESTARTED)
            await self._store.save_delegation(ended)
            logger.warning("task %s (%s -> %s) was running when the engine stopped; ended as failed",
                           entry.id, entry.agent, entry.to)
            self._spawn(self._answer(ended))

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

    async def _own(self, agent: str, runtime_session_id: str, task_id: str) -> DelegationRecord:
        entry = await self._store.get_delegation(task_id)
        record = await self._sessions.get_by_runtime_session(runtime_session_id)
        if (
            entry is None or record is None
            or entry.agent != agent or entry.conversation_id != record.conversation_id
        ):
            raise DelegationError(f"no task {task_id!r} was handed on from this conversation")
        return entry

    async def _depth(self, conversation_id: str) -> int:
        """How many tasks deep the asker's conversation is: one for a person's,
        one more for each task handed on before."""
        parent = conversation_id.removeprefix(delegated_conversation(""))
        if parent == conversation_id:
            return DELEGATION_HOP
        above = await self._store.get_delegation(parent)
        return (above.hop_depth if above is not None else DELEGATION_HOP) + 1

    def _attachments(self, agent: str, paths: Sequence[str]) -> tuple[Attachment, ...]:
        if not paths:
            return ()
        if self._file_roots is None:
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

    async def _run(self, entry: DelegationRecord, sink: TrackedSink, attachments: tuple[Attachment, ...]) -> None:
        key = delegated_conversation(entry.id)
        attributes = _attributes_of(entry)
        message = IncomingMessage(
            ref=ConversationRef(channel_id=key, conversation_id=key, message_id=entry.id, thread_root_id=""),
            text=entry.task,
            user_id=entry.user_id,
            username=entry.agent,
            timestamp=datetime.now(timezone.utc),
            kind=KIND_DM,
            is_dm=True,
            mentioned=True,
            is_from_bot=True,
            gateway=GATEWAY_DELEGATION,
            hop_depth=entry.hop_depth,
            attachments=attachments,
            synthetic=True,
            turn=TurnScope(turn_id=entry.id, attributes={
                **attributes,
                DELEGATED_BY_ATTRIBUTE: entry.agent,
                DELEGATION_TASK_ATTRIBUTE: entry.id,
                ON_BEHALF_OF_GATEWAY_ATTRIBUTE: entry.gateway,
            }),
            engine_started=True,
        )
        chat = self._task_chat()
        try:
            outcome = await sink.submit_tracked(message, chat)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("task %s: the asked agent's turn raised", entry.id)
            outcome = TurnOutcome.ERROR
        current = await self._store.get_delegation(entry.id)
        if current is not None and current.status == DELEGATION_CANCELLED:
            return  # called off while it ran: whatever came of it is nobody's
        ended = replace(
            entry,
            status=_STATUS_BY_OUTCOME.get(outcome, DELEGATION_FAILED),
            finished_at=_now(),
            answer=chat.answer,
            notice=chat.notice_code,
        )
        await self._store.save_delegation(ended)
        logger.info("task %s: %s %s (%s)", entry.id, entry.to, ended.status, outcome.name.lower())
        await self._answer(ended)

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
            turn=TurnScope(turn_id=f"{ended.id}-answer",
                           attributes={**_attributes_of(ended), DELEGATION_TASK_ATTRIBUTE: ended.id}),
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
            except DispatchError as exc:
                logger.warning("task %s: the answer could not be handed to %s: %s", ended.id, ended.agent, exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("task %s: the asker's turn on the answer raised", ended.id)
            else:
                await self._store.save_delegation(replace(ended, delivered_at=_now()))
            return


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
    "DELEGATION_MAX_DEPTH",
    "EngineDelegationService",
]
