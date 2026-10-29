"""AgentFlow: turns a batch of conversation messages into one agent reply.

Depends only on ports (AgentRuntime / SessionStore / ChatClient): swapping the
runtime or the platform never touches this file. Batch-native so the coalescer
can merge messages that arrived during a long turn into a single turn/reply.
"""

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from crucible.containment import one_line, quoted, speaker
from crucible.content_types import format_size, sniff_image
from crucible.ports.agent import (
    NOTICE_STOPPED,
    STOPPED_MESSAGE,
    AgentBusy,
    AgentError,
    AgentEvent,
    AgentProfile,
    AgentResult,
    AgentRuntime,
    AgentStalled,
    AgentTimeout,
    AgentUnavailable,
    CallCaptions,
    EventCallback,
    PromptImage,
    RuntimeControl,
    notice_for,
)
from crucible.ports.agent.events import ToolFinished, ToolStarted
from crucible.ports.attachments import FileKeeper, ReceivedFiles
from crucible.ports.chat.client import ChatClient, ConversationView
from crucible.ports.chat.context import ContextSource, HistorySource
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.keying import ConversationKey, PlatformKey
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    Attachment,
    ConversationRef,
    IncomingMessage,
    PostSnippet,
)
from crucible.ports.entitlement import (
    ConversationProfiles,
    EntitlementSource,
    shared_entitlement,
)
from crucible.ports.telemetry import Telemetry, TurnEnded
from crucible.ports.turn import TurnBinding
from crucible.store.base import (
    GatewayAddress,
    SessionRecord,
    SessionStore,
    gateway_addresses,
    with_gateway_address,
)

logger = logging.getLogger(__name__)

LOADING_REACTION = "eyes"
EMPTY_ANSWER_MESSAGE = "I thought about it but produced no answer — please try rephrasing."
SILENT_TOOLS_MESSAGE = "I looked things up but came back without an answer — please ask again."
# The model ran out of room. Two sentences for the two cases, because what the
# person just saw differs: a reply that stops mid-way, or nothing at all.
TRUNCATED_MESSAGE = "That answer hit the model's length limit and stops where it does."
TRUNCATED_EMPTY_MESSAGE = (
    "The model hit its length limit before it had answered, so there is nothing to show "
    "— ask again, perhaps for a shorter answer."
)
NOTICE_EMPTY_ANSWER = "empty_answer"
NOTICE_TRUNCATED = "truncated"
_MAX_BACKFILL_CHARS = 6000
_CHANNEL_BACKFILL_LIMIT = 20
# The prompt's own structure is a handful of lines beginning with `[`: a block
# of quoted material opens with `[context <tag>: <label>]` and closes with
# `[/context <tag>]`; a message is `[@name · time]: text`. Everything quoted
# inside is other people's text and goes through `quoted()`, which escapes a
# leading `[` — so no message can present as a marker or a speaker. The tag is
# drawn once per turn: a closer somebody typed does not carry it. What the model
# is told about this structure is ports/agent/envelope.py.
_LABEL_FIRST_TURN = "earlier conversation"
# Who is asking, from the application: ahead of everything else, since it frames
# both the history and the message.
_LABEL_PREAMBLE = "who is asking"
# The message a command points at (an emoji was put on it): somebody else's
# words, quoted under their name — never after the speaker's own `]: `, where
# the model would take them for the speaker's request.
_LABEL_POINTED_AT = "the message this points at, by {author}"
# What a turn the engine started on an event is about: the report, as data
# from the system it came from — not a person's words and not a request.
_LABEL_EVENT = "event reported by {source}"
# Later turns: what people said in this conversation while the agent wasn't
# addressed (in a channel it only runs when mentioned, so those messages never
# reached it as a turn).
_LABEL_CATCH_UP = "posted since your last reply"
# The engine's own account of the turn before this one when it was cut short:
# which tool calls finished and which never answered, so the model does not
# repeat a call whose effect it never saw.
_LABEL_CUT_SHORT = "the previous turn, which was cut short"
# Attached files are named in the prompt, always: even when a picture also
# travels to the runtime as an image, the agent needs the path to work with it.
_ATTACHMENT_MARKER = "[attached]"
# Defaults for showing pictures to the runtime. The size cap is per image and
# deliberately modest — model backends reject large inline images, and an
# oversized one is still reachable by path.
DEFAULT_INLINE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_INLINE_IMAGES = 4
# How a model backend's complaint about a picture reads, roughly: enough to tell
# a poisoned conversation apart from an ordinary failed turn.
_IMAGE_ERROR_WORDS = ("image", "picture")


def _new_envelope_tag() -> str:
    return secrets.token_hex(3)


def _context_open(tag: str, label: str) -> str:
    return f"[context {tag}: {label}]"


def _context_close(tag: str) -> str:
    return f"[/context {tag}]"


def _format_time(dt: datetime) -> str:
    """Render a message time for the prompt envelope. UTC keeps it unambiguous; the
    newest message is ~now, so the agent can reason about when things were said."""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


class _TurnFacts:
    """What a turn leaves for its telemetry — the runtime's result when there
    was one, the notice code the person got when there was one — and, while it
    runs, what ``stop`` needs: the runtime session to interrupt, and whether it
    was asked to."""

    def __init__(self, runtime_session_id: str = "") -> None:
        self.result: AgentResult | None = None
        self.notice: str = ""
        self.runtime_session_id = runtime_session_id
        self.stopped = False


STOPPED_NOTE = (
    "The turn before this one was stopped by the person before it finished. "
    "What it did up to then stands; what it was about to do did not happen."
)


async def _set_busy(view: ConversationView, ref: ConversationRef, busy: bool) -> None:
    """The platform's view of the conversation, told and never waited on to
    succeed: an indicator that fails to light must not cost the turn, nor may
    its failure stand in for the turn's own."""
    try:
        await view.set_busy(ref, busy)
    except Exception:  # noqa: BLE001 - a view is told, never relied on
        logger.warning("the conversation's view could not be marked %s", "busy" if busy else "free", exc_info=True)


def _named(args: Mapping[str, Any], names: Mapping[str, str]) -> Mapping[str, Any]:
    """The arguments with every path that is a person's file replaced by the
    name they gave it — for the caption alone; the call runs on the paths."""
    if not names:
        return args
    return {
        key: names.get(value, value) if isinstance(value, str)
        else [names.get(v, v) if isinstance(v, str) else v for v in value] if isinstance(value, list)
        else value
        for key, value in args.items()
    }


async def _adopt_all(keeper: FileKeeper, agent: str, key: str, msg: IncomingMessage) -> IncomingMessage:
    if not msg.attachments:
        return msg
    adopted = tuple([await keeper.adopt(agent, key, a) for a in msg.attachments])
    return replace(msg, attachments=adopted)


def _attachment_line(attachment: Attachment) -> str:
    """One attached file, as the agent sees it: name, type, size, and the path it
    can read."""
    # The name is the sender's and the type is the platform's claim: neither
    # may add a line to the prompt.
    kind = one_line(attachment.mime, limit=80) or "unknown type"
    return (
        f"{_ATTACHMENT_MARKER} {one_line(attachment.name, limit=120)} — {kind}, "
        f"{format_size(attachment.size)} — {attachment.path}"
    )


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class _TurnLog:
    """What one turn's tool calls came to, from the runtime's events: enough to
    tell the next turn what happened when this one was cut short. Captions and
    names only — never arguments, never results."""

    def __init__(self) -> None:
        self._calls: list[tuple[str, str, str]] = []  # (tool, caption, state)
        self._open: dict[str, int] = {}

    def record(self, event: AgentEvent) -> None:
        if isinstance(event, ToolStarted):
            self._open[event.call_id] = len(self._calls)
            self._calls.append((event.tool, event.caption, "running"))
        elif isinstance(event, ToolFinished):
            index = self._open.pop(event.call_id, None)
            if index is not None:
                tool, caption, _ = self._calls[index]
                self._calls[index] = (tool, caption, "failed" if event.is_error else "ok")

    def cut_short_note(self, error: Exception) -> str:
        """One paragraph for the model, in the envelope's quoted block."""
        why = (
            "the model stopped responding" if isinstance(error, AgentStalled)
            else "it ran past its time limit"
        )
        lines = [f"The turn before this one was stopped by the engine: {why}."]
        done = [self._name(c) for c in self._calls if c[2] != "running"]
        running = [self._name(c) for c in self._calls if c[2] == "running"]
        if not self._calls:
            lines.append("It had called no tool.")
        if done:
            lines.append(f"Tool calls that finished, whose results you saw: {', '.join(done)}.")
        if running:
            lines.append(
                f"Still running when it was stopped, so you never saw a result: {', '.join(running)}. "
                "Such a call may have taken effect anyway — check before doing it again."
            )
        return " ".join(lines)

    @staticmethod
    def _name(call: tuple[str, str, str]) -> str:
        tool, caption, state = call
        name = f"{tool} ({caption})" if caption else tool
        return f"{name}, failed" if state == "failed" else name


class ActiveTrace(Protocol):
    """One turn's tool activity, as far as the flow is concerned: it is fed the
    runtime's events and told when the turn is over. What it draws, and where,
    is its own business."""

    def on_event(self, event: AgentEvent) -> None: ...

    async def end(self, *, interrupted: bool = False) -> None: ...


class TurnTracer(Protocol):
    """Hands the flow a trace for each turn. Optional: a flow without one runs
    exactly as before. Declared here rather than imported so this module keeps
    its one rule — it depends on ports and the store, not on the machinery that
    posts widgets."""

    def begin(
        self, record: SessionRecord, chat: ChatClient, *, anchor: ConversationRef | None = None
    ) -> ActiveTrace:
        """``anchor`` is the message the turn answers — None when there is no
        real one (a click, a command, a scheduled run)."""
        ...


class AgentFlow:
    def __init__(
        self,
        runtime: AgentRuntime,
        profile: AgentProfile,
        sessions: SessionStore,
        *,
        agent_name: str,
        inline_image_max_bytes: int = DEFAULT_INLINE_IMAGE_MAX_BYTES,
        max_inline_images: int = DEFAULT_MAX_INLINE_IMAGES,
        tracer: TurnTracer | None = None,
        turns: TurnBinding | None = None,
        flag_notices: Mapping[str, str] | None = None,
        control: RuntimeControl | None = None,
        history: HistorySource | None = None,
        context: ContextSource | None = None,
        entitlements: EntitlementSource | None = None,
        conversation_profiles: ConversationProfiles | None = None,
        captions: CallCaptions | None = None,
        keying: ConversationKey | None = None,
        posting_tools: Collection[str] = (),
        file_keeper: FileKeeper | None = None,
        received_files_of: Callable[[str, str], ReceivedFiles] | None = None,
        telemetry: Telemetry | None = None,
        envelope_tag: Callable[[], str] = _new_envelope_tag,
    ) -> None:
        if (entitlements is None) != (conversation_profiles is None):
            raise ValueError(
                "AgentFlow needs entitlements and conversation_profiles together: one "
                "says what a conversation may do, the other builds the profile that does it"
            )
        self._runtime = runtime
        self._profile = profile
        self._sessions = sessions
        self._agent_name = agent_name
        self._tracer = tracer
        # The tools whose call puts something in front of a person (a widget, a
        # form, a panel, a file, a message — ``speaks_to_user`` or
        # ``posts_to_user``): a turn that called one and wrote nothing has
        # reached them. Any other tool-only turn has not.
        self._posting_tools = frozenset(posting_tools)
        # Where the conversation's files are kept: a file a gateway saved under
        # the key it could compute is taken into the conversation as keyed here.
        self._file_keeper = file_keeper
        # (agent, conversation key) -> what the conversation received, for the
        # captions: a call on a person's file is captioned with the name they
        # gave it, not the name on disk. None = captions see the paths.
        self._received_files_of = received_files_of
        # Where a turn's telemetry goes (None = nobody listens).
        self._telemetry = telemetry
        # Where a message's TurnScope is bound for the length of its turn, so the
        # tools the turn calls can read it. None = messages here carry none.
        self._turns = turns
        # What to say for a flag a tool left on the turn's scope ("the session
        # has expired"): flag -> sentence. A flag with no sentence is still
        # reported, as itself — the code is what a program acts on.
        self._flag_notices = dict(flag_notices or {})
        # Asked, on a conversation the store already knows, whether the runtime
        # still remembers it, and told to interrupt a turn a person stops. None
        # = trust the store, and nothing here can be stopped.
        self._control = control
        # The turns in flight, by conversation key and by every address the
        # anchor carried (gateway, a platform id — the conversation's, the
        # thread's, the channel's): what ``stop`` finds, whichever name the
        # caller knows.
        self._active: dict[str, _TurnFacts] = {}
        self._active_at: dict[tuple[str, str], _TurnFacts] = {}
        # Where the conversation is kept when the platform cannot hand it back
        # (a direct message over http has no thread to read). Consulted only on
        # a turn the runtime starts from nothing, and only when the platform
        # had nothing to replay.
        self._history = history
        # Who is asking, from the application, ahead of the message. Every turn.
        self._context = context
        # What this conversation may do, from the application, every turn — and
        # what turns the answer into the profile the turn runs with. None =
        # every conversation runs with the agent's profile.
        self._entitlements = entitlements
        self._conversation_profiles = conversation_profiles
        # Who captions a tool call for the person watching ("Reading X-1"):
        # asked once per call here, so every trace reads the same line.
        self._captions = captions
        # Which conversation a message belongs to — the key its session is kept
        # under. The platform's own id unless the application keys conversations
        # across gateways.
        self._keying: ConversationKey = keying or PlatformKey()
        # The code on this turn's context markers. Injectable so a test can pin
        # it; the default is fresh per prompt.
        self._envelope_tag = envelope_tag
        self._own_user_ids: set[str] = set()
        self._inline_image_max_bytes = inline_image_max_bytes
        self._max_inline_images = max_inline_images

    def add_identity(self, user_id: str) -> None:
        """This agent's own user id on one gateway, learned at that gateway's
        login — one per gateway the agent is reached through. Used to keep the
        agent's own posts out of replayed history (they are already in its
        runtime session). None added = no filtering."""
        if user_id:
            self._own_user_ids.add(user_id)

    @property
    def profile(self) -> AgentProfile:
        """The profile the next turn will use — kept current by set_profile, so
        a caller that runs something outside the flow (a memoryless scheduled
        run) uses the same configuration a reload just applied."""
        return self._profile

    def set_profile(self, profile: AgentProfile) -> None:
        """Swap the profile the next turn runs with (hot-reload). A live
        conversation picks it up on its next turn too: the runtime restarts a
        process whose profile is not the one it was started with, and resumes
        the conversation's memory."""
        self._profile = profile

    async def _turn_profile(self, msgs: list[IncomingMessage], record: SessionRecord) -> AgentProfile:
        """The profile this turn runs with: the agent's, or the conversation's
        own when the application entitles it to less or tells it more. A batch
        may hold several people's messages (a channel, a thread): each is
        asked about once, and the turn gets what they are jointly entitled to
        — the tools all of them may use, instructions only where they agree."""
        if self._entitlements is None or self._conversation_profiles is None:
            return self._profile
        first_by_author = {m.user_id: m for m in reversed(msgs)}
        answers = [
            await self._entitlements.entitlement(self._agent_name, m)
            for m in first_by_author.values()
        ]
        entitlement = answers[0] if len(answers) == 1 else shared_entitlement(answers)
        return self._conversation_profiles.build(
            self._agent_name, self._profile, entitlement, record.runtime_session_id
        )

    async def handle(self, msg: IncomingMessage, chat: ChatClient) -> TurnOutcome:
        return await self.handle_batch([msg], chat)

    async def handle_batch(
        self, msgs: list[IncomingMessage], chat: ChatClient
    ) -> TurnOutcome:
        """Handle one or more messages of the SAME conversation as a single turn.

        Dedup drops replays; the newest surviving message anchors the reply (its
        thread position, hop accounting). All messages share the runtime session,
        so their texts are merged into one prompt with per-message attribution.

        The outcome is for a caller that started this turn and has to report on
        it; a gateway ignores it. Every failing outcome has already been told to
        the user by the time it is returned."""
        fresh = [
            m
            for m in msgs
            if await self._sessions.mark_processed(self._agent_name, m.ref.message_id)
        ]
        if not fresh:
            return TurnOutcome.DUPLICATE
        key = self._keying.key(self._agent_name, fresh[-1])
        if self._file_keeper is not None:
            # The gateway saved what people attached under the key it could
            # compute; the conversation's key is settled here, so the files
            # follow it — a tool's directory and the prompt's path then agree.
            fresh = [await _adopt_all(self._file_keeper, self._agent_name, key, m) for m in fresh]
        anchor = fresh[-1]

        record, created = await self._sessions.get_or_create(
            self._agent_name, anchor.channel_id, key, anchor.kind,
            user_id=anchor.user_id, gateway=anchor.gateway,
        )
        if anchor.gateway:
            # How this conversation is addressed on the gateway the message came
            # through: the platform's ids, kept so a card or an engine-started
            # turn can reach it there — written once per gateway, and again
            # when the address moved.
            prior = gateway_addresses(record).get(anchor.gateway)
            # Who reads the place: what the gateway said of the person's message.
            # A message the engine made (a click, a command, a form, a turn it
            # started) says nothing about it and leaves the flag as it stands.
            direct = (
                prior.direct if anchor.synthetic and prior is not None
                else anchor.is_dm or anchor.kind == KIND_DM
            )
            address = GatewayAddress(anchor.channel_id, anchor.conversation_id, anchor.kind, direct=direct)
            if prior != address:
                await self._sessions.set_gateway_address(self._agent_name, key, anchor.gateway, address)
                record = replace(record, gateways=with_gateway_address(record, anchor.gateway, address))
        started = time.monotonic()
        facts = _TurnFacts(record.runtime_session_id)
        # Every name the gateway may know the conversation by — the id, the
        # thread, the message itself (Slack opens a session under it), and in a
        # direct message its channel, which is the conversation there — so a
        # stop that names any of them finds the turn, even where the
        # application keyed the message elsewhere. A channel's name is not a
        # thread's: a /stop in a channel does not reach a thread of it.
        names = [anchor.conversation_id, anchor.ref.thread_root_id, anchor.ref.message_id]
        if anchor.is_dm or anchor.kind == KIND_DM:
            names.append(anchor.channel_id)
        addresses = {(anchor.gateway, name) for name in names if name}
        self._active[key] = facts
        for address in addresses:
            self._active_at.setdefault(address, facts)
        try:
            outcome = await self._turn(fresh, anchor, record, created, chat, facts)
        finally:
            self._active.pop(key, None)
            for address in addresses:
                if self._active_at.get(address) is facts:
                    del self._active_at[address]
        if self._telemetry is not None:
            self._tell_turn(fresh, anchor, key, outcome, facts, time.monotonic() - started)
        if anchor.turn is not None and anchor.turn.flags:
            # After the answer or the failure notice: a tool's note about the turn
            # ("your session expired") reads as the footnote it is.
            for flag in sorted(anchor.turn.flags):
                await chat.post_notice(
                    anchor.ref, self._flag_notices.get(flag, flag), code=flag
                )
        return outcome

    async def stop(self, conversation_id: str, *, gateway: str = "") -> bool:
        """Stop the turn running in this conversation (``StoppableSink``): the
        runtime is told to interrupt it, and the turn ends as stopped — what it
        had posted, a notice, a note for the next turn. The conversation is
        named by its key, or with ``gateway`` by the platform's id the turn's
        message carried. False when no turn is running here or the runtime
        cannot be told."""
        facts = self._active.get(conversation_id)
        if facts is None and gateway:
            facts = self._active_at.get((gateway, conversation_id))
        if facts is None or self._control is None or not facts.runtime_session_id:
            return False
        cancelled = await self._control.cancel(facts.runtime_session_id)
        # Only a turn the runtime did interrupt ends as stopped: one it had not
        # yet begun runs on and ends on its own terms.
        facts.stopped = cancelled
        logger.info("turn in %s/%s stopped by request (%s)", self._agent_name, conversation_id,
                    "interrupted" if cancelled else "nothing was running")
        return cancelled

    async def _end_stopped(
        self, anchor: IncomingMessage, chat: ChatClient, facts: _TurnFacts, text: str,
    ) -> TurnOutcome:
        """A turn a person stopped: what the model had said is posted, then the
        notice; the next turn is told the previous one did not finish."""
        if text:
            await chat.post_reply(anchor.ref, text)
        await self._sessions.set_cut_short(self._agent_name, anchor.conversation_id, STOPPED_NOTE)
        facts.notice = NOTICE_STOPPED
        await chat.post_notice(anchor.ref, STOPPED_MESSAGE, code=NOTICE_STOPPED)
        return TurnOutcome.STOPPED

    def _tell_turn(
        self, fresh: list[IncomingMessage], anchor: IncomingMessage, key: str,
        outcome: TurnOutcome, facts: _TurnFacts, duration_s: float,
    ) -> None:
        assert self._telemetry is not None
        result = facts.result
        try:
            self._telemetry.record(TurnEnded(
                agent=self._agent_name, gateway=anchor.gateway, user_id=anchor.user_id, conversation_id=key,
                kind=anchor.kind, direct=anchor.is_dm or anchor.kind == KIND_DM, synthetic=anchor.synthetic,
                messages=len(fresh), attachments=sum(len(m.attachments) for m in fresh),
                outcome=outcome.name.lower(), notice=facts.notice, duration_s=duration_s,
                model=result.model if result is not None else "",
                stop_reason=(result.stop_reason or "") if result is not None else "",
                tool_calls=tuple(result.tool_calls) if result is not None else (),
                truncated=result.truncated if result is not None else False,
                usage=result.usage if result is not None else None,
            ))
        except Exception:  # noqa: BLE001 - telemetry must never cost the turn
            logger.warning("telemetry failed to record a turn", exc_info=True)

    async def _turn(
        self,
        fresh: list[IncomingMessage],
        anchor: IncomingMessage,
        record: SessionRecord,
        created: bool,
        chat: ChatClient,
        facts: _TurnFacts,
    ) -> TurnOutcome:
        # A first turn for the runtime is not only a conversation the store has
        # never seen: a session the runtime has forgotten (evicted, wiped with
        # the pod's disk, reset by an operator) also starts from nothing, and
        # replaying the history then is what keeps the conversation one.
        first_turn = created or (
            self._control is not None
            and not self._control.has_memory(self._agent_name, record.runtime_session_id)
        )
        # On a later turn, last_active is still the PREVIOUS turn's end (touch()
        # runs after the turn, and get_or_create doesn't refresh it) — exactly the
        # cursor for "what was said while we weren't addressed". Unless this
        # process's memory of the conversation is older than that: the
        # conversation went on elsewhere (another process ran turns of it), and
        # the catch-up starts where the memory here ends. Only a memory older
        # than the cursor is stale; otherwise it has every turn, and the
        # catch-up is the platform's alone.
        since = None if first_turn else _parse_iso(record.last_active)
        memory_at = (
            self._control.memory_updated_at(self._agent_name, record.runtime_session_id)
            if self._control is not None and not first_turn
            else None
        )
        stale = since is not None and memory_at is not None and memory_at < since
        if not stale:
            memory_at = None
        else:
            since = memory_at
        prompt = await self._render_prompt(
            fresh, chat, first_turn=first_turn, since=since, cut_short=record.cut_short,
            memory_at=memory_at,
        )
        if record.cut_short:
            # Said once: the next turn after this one starts clean.
            await self._sessions.set_cut_short(self._agent_name, anchor.conversation_id, "")
        images = await self._inline_images(fresh)
        profile = await self._turn_profile(fresh, record)

        # Only a real message can wear the "working on it" mark. A synthetic
        # anchor — a slash command, a widget click, a scheduled run — carries an
        # id the platform never issued, so reacting to it just makes the gateway
        # log a rejection nobody can act on.
        acknowledge = not anchor.synthetic
        if acknowledge:
            await chat.add_reaction(anchor.ref, LOADING_REACTION)
        # A platform with a view of the conversation (Slack's agent session) is
        # told the agent is busy — its indicator, its Stop button — for the turn.
        view = chat if isinstance(chat, ConversationView) else None
        if view is not None:
            await _set_busy(view, anchor.ref, True)
        # The trace posts nothing until the first tool runs, so a turn that
        # only talks leaves no extra message.
        trace = (
            self._tracer.begin(record, chat, anchor=None if anchor.synthetic else anchor.ref)
            if self._tracer is not None else None
        )
        # The anchor's scope is the turn's: a batch is one turn, and the newest
        # message is the one whose credentials are freshest.
        bound = (
            self._turns.bind(record.runtime_session_id, anchor.turn)
            if self._turns is not None and anchor.turn is not None
            else contextlib.nullcontext()
        )
        interrupted = True  # until the runtime hands back a result
        log = _TurnLog()
        names = await self._file_names(record.conversation_id)
        try:
            async with bound:
                result = await self._runtime.run_stateful(
                    profile, record.runtime_session_id, prompt, images=images,
                    on_event=self._observer(trace, log, names),
                )
            interrupted = False
            facts.result = result
        except AgentBusy as exc:
            # Nothing ran: the engine was full. Said as such, and nothing for
            # the next turn to be told — there was no turn.
            logger.warning("agent turn could not start for %s: %s", record.runtime_session_id, exc)
            code, text = notice_for(exc)
            facts.notice = code
            await chat.post_notice(anchor.ref, text, code=code)
            return TurnOutcome.TIMEOUT
        except AgentTimeout as exc:
            if facts.stopped:
                return await self._end_stopped(anchor, chat, facts, "")
            logger.warning(
                "agent turn timed out for %s: %s", record.runtime_session_id, exc
            )
            # The next turn is told what this one did before it was stopped:
            # the runtime's own memory may hold the calls but not the result
            # of the one in flight, and that is the one that may have acted.
            await self._sessions.set_cut_short(
                self._agent_name, anchor.conversation_id, log.cut_short_note(exc)
            )
            code, text = notice_for(exc)
            facts.notice = code
            await chat.post_notice(anchor.ref, text, code=code)
            return TurnOutcome.TIMEOUT
        except AgentError as exc:
            if facts.stopped:
                # The runtime may word the interruption as an error of its own;
                # the person asked for it, and that is what it was.
                return await self._end_stopped(anchor, chat, facts, "")
            # The cause goes to the log in full; what reaches the conversation is
            # which KIND of failure it was, because a notice is an ordinary
            # message everyone present can read.
            logger.exception("agent run failed for %s", record.runtime_session_id)
            self._warn_if_picture_rejected(exc, record)
            code, text = notice_for(exc)
            facts.notice = code
            await chat.post_notice(anchor.ref, text, code=code)
            return TurnOutcome.ERROR
        finally:
            if acknowledge:
                await chat.remove_reaction(anchor.ref, LOADING_REACTION)
            if view is not None:
                await _set_busy(view, anchor.ref, False)
            if trace is not None:
                # Settled before the reply is posted, so in a thread the
                # widget sits above it — and on the timeout and error paths
                # too, so a turn that broke mid-tool still says which tool it
                # was in.
                await trace.end(interrupted=interrupted)

        await self._sessions.touch(
            self._agent_name, record.conversation_id, engine_turn=anchor.engine_started,
        )
        if facts.stopped:
            return await self._end_stopped(anchor, chat, facts, result.text)
        truncated = result.truncated
        if result.text:
            # One hop past the deepest message in the batch, so any agent we
            # mention accounts for the cascade (see LoopGuard).
            hop = max(m.hop_depth for m in fresh) + 1
            await chat.post_reply(anchor.ref, result.text, hop_depth=hop)
            if truncated:
                # Said after the reply, so the person reads what there is first
                # and then why it stops where it does.
                facts.notice = NOTICE_TRUNCATED
                await chat.post_notice(anchor.ref, TRUNCATED_MESSAGE, code=NOTICE_TRUNCATED)
                return TurnOutcome.TRUNCATED
            return TurnOutcome.REPLIED
        if truncated:
            # Not an empty turn: the model had no room left to answer, and
            # telling the person to rephrase would blame the question.
            facts.notice = NOTICE_TRUNCATED
            await chat.post_notice(anchor.ref, TRUNCATED_EMPTY_MESSAGE, code=NOTICE_TRUNCATED)
            return TurnOutcome.TRUNCATED
        if any(call in self._posting_tools for call in result.tool_calls):
            # The silence is deliberate: a tool that posts to the person has
            # already put something there (buttons, a form, a panel, a file),
            # and a fallback here would double up on it.
            return TurnOutcome.ACTED
        if result.tool_calls:
            # The agent looked things up and stopped without a word: to the
            # person that is an unanswered question, and the application
            # hears of it the way it hears of an empty turn.
            facts.notice = NOTICE_EMPTY_ANSWER
            await chat.post_notice(anchor.ref, SILENT_TOOLS_MESSAGE, code=NOTICE_EMPTY_ANSWER)
            return TurnOutcome.EMPTY
        # No text AND no tool call: the turn produced nothing at all — nudge the
        # user to rephrase.
        facts.notice = NOTICE_EMPTY_ANSWER
        await chat.post_notice(anchor.ref, EMPTY_ANSWER_MESSAGE, code=NOTICE_EMPTY_ANSWER)
        return TurnOutcome.EMPTY

    # -- internals ----------------------------------------------------------

    async def _file_names(self, conversation_id: str) -> dict[str, str]:
        """The name the person gave each file the conversation received, by
        the path the model sees — read once per turn, for the captions."""
        if self._received_files_of is None or self._captions is None:
            return {}  # nobody to caption for: nothing to read
        try:
            return {r.path: r.name for r in await self._received_files_of(self._agent_name, conversation_id).list()}
        except Exception:  # noqa: BLE001 - a caption must never cost the turn anything
            logger.debug("received files of %s could not be read for the captions", conversation_id, exc_info=True)
            return {}

    def _observer(self, trace: ActiveTrace | None, log: _TurnLog, names: Mapping[str, str]) -> EventCallback:
        """The runtime's events, captioned on their way to the trace and noted
        in the turn's own log. A tool call that the runtime reported without a
        caption gets the one the deployment knows for it, so the chat widget
        and a journal say the same thing without each asking; the log is what
        the flow tells the next turn when this one is cut short. A path among
        the arguments that is a file the person sent is captioned by the name
        they gave it (``names``): the caption is for them."""
        captions = self._captions

        def on_event(event: AgentEvent) -> None:
            if captions is not None and isinstance(event, ToolStarted) and not event.caption:
                event = replace(event, caption=captions.caption(event.tool, _named(event.args, names)))
            log.record(event)
            if trace is not None:
                trace.on_event(event)

        return on_event

    async def _render_prompt(
        self, msgs: list[IncomingMessage], chat: ChatClient, *, first_turn: bool,
        since: datetime | None = None, cut_short: str = "", memory_at: datetime | None = None,
    ) -> str:
        """Envelope: who is speaking (+ replayed history).

        The conversation itself lives in the runtime session, so only sender
        identity travels with each message — vital once multiple people and agents
        share a channel. History is replayed into the prompt (the runtime has no
        history-injection API) in two shapes: on the FIRST turn of a session with
        prior history, the whole transcript; on later turns, only what was posted
        since our last reply — in a channel the agent only runs when mentioned, so
        anything said in between never reached it as a turn. The transcript comes
        from the platform (the thread, the channel) when it has one, else from
        the application's ``HistorySource``; who is asking, from its
        ``ContextSource``, goes ahead of everything."""
        anchor = msgs[-1]
        parts: list[str] = []
        batch_ids = {m.ref.message_id for m in msgs}
        tag = self._envelope_tag()
        if self._context is not None:
            preamble = await self._context.preamble(self._agent_name, anchor)
            if preamble:
                parts.append(f"{_context_open(tag, _LABEL_PREAMBLE)}\n{quoted(preamble)}\n{_context_close(tag)}")
        snippets = await self._backfill_snippets(anchor, chat)
        if self._history is not None and (first_turn and not snippets or memory_at is not None):
            # The platform had nothing to give back — or this process's memory of
            # the conversation is older than the conversation (``memory_at``: it
            # went on in another process, maybe through another gateway), and
            # the application's own record holds what the platform cannot hand
            # back. Only what is newer than that memory is kept below.
            seen = {s.message_id for s in snippets}
            recent = await self._history.recent(self._agent_name, anchor)
            snippets += [s for s in recent if s.message_id not in seen]
        if not first_turn:
            snippets = self._since(snippets, since, memory_at=memory_at)
        if snippets:
            transcript = self._render_transcript(snippets, exclude=batch_ids)
            if transcript:
                label = _LABEL_FIRST_TURN if first_turn else _LABEL_CATCH_UP
                parts.append(f"{_context_open(tag, label)}\n{transcript}\n{_context_close(tag)}")
        if cut_short:
            parts.append(f"{_context_open(tag, _LABEL_CUT_SHORT)}\n{quoted(cut_short)}\n{_context_close(tag)}")
        for m in msgs:
            author = f"@{speaker(m.username)}" if m.username else speaker(m.user_id)
            when = f" · {_format_time(m.timestamp)}" if m.timestamp else ""
            lines = []
            if m.quoted is not None:
                label = _LABEL_POINTED_AT.format(author=f"@{speaker(m.quoted.author)}")
                lines.append(f"{_context_open(tag, label)}\n{quoted(m.quoted.text)}\n{_context_close(tag)}")
            if m.event is not None:
                label = _LABEL_EVENT.format(source=speaker(m.event.source))
                lines.append(f"{_context_open(tag, label)}\n{quoted(m.event.text)}\n{_context_close(tag)}")
            lines.append(f"[{author}{when}]: {quoted(m.text, inline=True)}")
            lines += [_attachment_line(a) for a in m.attachments]
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @staticmethod
    def _warn_if_picture_rejected(error: Exception, record: SessionRecord) -> None:
        """A picture the model backend refuses is not a one-turn problem: the
        session replays its history, so the same request fails from then on. The
        engine only shows pictures it has verified, so reaching this means the
        backend refused one it accepts the format of (an enormous one, say) —
        say so plainly, with the way out. Not for a runtime that could not be
        started at all (``AgentUnavailable``): the model said nothing then, and
        a host's "the image is out of date" is a container image."""
        if isinstance(error, AgentUnavailable):
            return
        if not any(word in str(error).lower() for word in _IMAGE_ERROR_WORDS):
            return
        logger.error(
            "the model backend refused a picture in session %s. Its history "
            "replays on every turn, so this conversation will keep failing until "
            "it is reset: `python -m crucible.sessions_cli delete %s %s`",
            record.runtime_session_id, record.agent, record.conversation_id,
        )

    async def _inline_images(self, msgs: list[IncomingMessage]) -> list[PromptImage]:
        """Pictures from this batch, for runtimes that can look at them.

        Newest first and capped in count and size: a model backend rejects large
        inline images, and everything attached is named by path in the prompt
        anyway, so leaving one out costs the agent nothing but a read."""
        images: list[PromptImage] = []
        for msg in reversed(msgs):
            for attachment in msg.attachments:
                if len(images) >= self._max_inline_images:
                    return images
                if not attachment.is_image:
                    continue
                if attachment.size > self._inline_image_max_bytes:
                    logger.info(
                        "not showing %s inline (%d bytes); the path is in the prompt",
                        attachment.name, attachment.size,
                    )
                    continue
                try:
                    data = await asyncio.to_thread(Path(attachment.path).read_bytes)
                except OSError as exc:
                    logger.warning("could not read attachment %s: %s", attachment.path, exc)
                    continue
                # Trust the bytes, not the label: a file that only claims to be a
                # picture would be rejected by the model backend on this turn AND
                # on every later one, since the session replays its history.
                actual = sniff_image(data)
                if not actual:
                    logger.warning(
                        "%s is not a picture the runtime can read despite its %s type; "
                        "the path is in the prompt", attachment.name, attachment.mime,
                    )
                    continue
                images.append(PromptImage(data=data, mime=actual))
        return images

    async def _backfill_snippets(
        self, msg: IncomingMessage, chat: ChatClient
    ) -> list[PostSnippet]:
        root_id = msg.ref.thread_root_id
        if msg.kind == KIND_THREAD and root_id and root_id != msg.ref.message_id:
            return await chat.get_thread_posts(msg.ref)
        if msg.kind == KIND_CHANNEL:
            return await chat.get_recent_posts(
                msg.channel_id, limit=_CHANNEL_BACKFILL_LIMIT
            )
        return []

    def _since(
        self,
        snippets: list[PostSnippet],
        since: datetime | None,
        *,
        memory_at: datetime | None = None,
    ) -> list[PostSnippet]:
        """Catch-up filter: only messages posted after ``since``, and not our own
        — those are already in the runtime session — unless they are newer than
        this process's memory of the conversation (``memory_at``): then they
        were said elsewhere, and the memory here does not have them. A snippet
        without a timestamp can't be placed in time, so it is left out rather
        than replayed on every turn."""
        if since is None:
            return []
        return [
            s
            for s in snippets
            if s.timestamp is not None
            and s.timestamp > since
            and (
                s.user_id not in self._own_user_ids
                or (memory_at is not None and s.timestamp > memory_at)
            )
        ]

    @staticmethod
    def _render_transcript(snippets: list[PostSnippet], *, exclude: set[str]) -> str:
        lines = [
            f"[@{speaker(s.username)}{f' · {_format_time(s.timestamp)}' if s.timestamp else ''}]: "
            f"{quoted(s.text, inline=True)}"
            for s in snippets
            if s.message_id not in exclude
        ]
        # Budget from the END: the latest messages carry the most context.
        kept: list[str] = []
        budget = _MAX_BACKFILL_CHARS
        for line in reversed(lines):
            budget -= len(line) + 1
            if budget < 0:
                break
            kept.append(line)
        return "\n".join(reversed(kept))
