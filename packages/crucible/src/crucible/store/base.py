"""Session-inventory port + the deterministic session-id rule.

The id is derived, never invented: ``<agent>--<conversation_id>``, coerced to the
runtime's allowed alphabet. Deterministic derivation means a lost DB still resumes
runtime-side memory from disk — the DB only exists so humans (and the cleanup CLI)
can enumerate what sessions exist. The runtime applies the same coercion
defensively when it starts a session; ``tests/test_session_store.py`` pins the
agreement so the two never drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.directory import AgentInfo


def derive_runtime_session_id(agent: str, conversation_id: str) -> str:
    """Deterministic, filesystem-safe session key from (agent, conversation).

    Deterministic so the DB stays inventory, not source of truth: the key is
    recomputable from the pair alone. The charset is a portable safe-identifier
    set (also valid as the runtime's session id); the runtime re-coerces to the
    same set at its own boundary, so stored key and on-disk session agree.
    """
    raw = f"{agent}--{conversation_id}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    return cleaned or "session"


@dataclass(frozen=True)
class SessionRecord:
    agent: str
    channel_id: str
    conversation_id: str
    kind: str  # KIND_THREAD | KIND_DM | KIND_CHANNEL
    runtime_session_id: str
    created_at: str  # ISO8601 UTC
    last_active: str
    # The user who most recently triggered a turn in this conversation. Lets a
    # tool address that person (e.g. an ephemeral reply) without the runtime
    # forwarding per-message identity. "" when unknown.
    last_user_id: str = ""


@dataclass(frozen=True)
class InteractionRecord:
    """A widget awaiting a click. ``token`` gates the callback; the conversation
    fields say where the click resumes."""

    interaction_id: str
    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    created_at: str


class InteractionStore(Protocol):
    """Pending widget interactions, keyed for one-shot consumption on click."""

    async def create_interaction(self, record: InteractionRecord) -> None: ...

    async def take_interaction(self, token: str) -> InteractionRecord | None:
        """Return and CONSUME the interaction for this token (one-shot); None if
        unknown/already used — so a replayed click can't fire twice."""
        ...


@dataclass(frozen=True)
class FormRecord:
    """A pending modal form (open_form). The button was posted; on click we
    rebuild the dialog from ``spec`` (opaque JSON — a serialized chat.Form), on
    submit we feed the values back into the conversation and retire the button
    message (``post_id``)."""

    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    spec: str
    created_at: str
    # The message carrying the "fill in" button, so submitting can strike it out.
    # "" for records written before the engine started recording it.
    post_id: str = ""


class FormStore(Protocol):
    """Pending forms. Unlike a one-shot interaction the token is READ on the
    open-click (to build the dialog) and only deleted on submit/cancel."""

    async def create_form(self, record: FormRecord) -> None: ...

    async def get_form(self, token: str) -> FormRecord | None: ...

    async def delete_form(self, token: str) -> None: ...


# -- scheduled work -----------------------------------------------------------

# How a due task runs: as an ordinary turn in its own conversation (memory, the
# reply lands there), or as a fresh memoryless run whose text is posted for it.
MODE_TURN = "turn"
MODE_PROMPT = "prompt"
MODES = (MODE_TURN, MODE_PROMPT)

# A task's own state. `running` doubles as the lease: a task is claimed for one
# occurrence at a time, so a second ticker cannot fire the same one.
STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_DONE = "done"

# What a scheduled occurrence came to. A closed set: sub-reasons go in
# ``TaskRunRecord.detail``, so "why didn't it run" is always greppable.
RUN_RUNNING = "running"  # claimed and in flight
RUN_OK = "ok"  # the agent answered
RUN_EMPTY = "empty"  # the turn produced neither text nor a tool call
RUN_TIMEOUT = "timeout"  # the runtime timed out (the user got the fallback)
RUN_ERROR = "error"  # the runtime or the dispatch path failed
RUN_DEADLINE = "deadline"  # we stopped waiting; the turn may still be running
RUN_INTERRUPTED = "interrupted"  # the engine died mid-run
RUN_NO_AGENT = "no_agent"  # the agent isn't live (profile gone, no token)
RUN_NO_CONVERSATION = "no_conversation"  # nowhere to post the result
RUN_MISSED = "missed"  # past its grace window, or on_missed=skip
RUN_OVERLAP = "overlap"  # the previous run outlasted the task's own period
RUN_CANCELLED = "cancelled"  # shutdown, or the task was removed mid-run
RUN_DUPLICATE = "duplicate"  # the turn was deduped — a run id was replayed
RUN_STATUSES = (
    RUN_RUNNING, RUN_OK, RUN_EMPTY, RUN_TIMEOUT, RUN_ERROR, RUN_DEADLINE,
    RUN_INTERRUPTED, RUN_NO_AGENT, RUN_NO_CONVERSATION, RUN_MISSED,
    RUN_OVERLAP, RUN_CANCELLED, RUN_DUPLICATE,
)
# Why an occurrence fired: on time, late but inside its grace window, or by hand.
TRIGGERED_SCHEDULE = "schedule"
TRIGGERED_CATCHUP = "catchup"
TRIGGERED_MANUAL = "manual"

ON_MISSED_RUN = "run"
ON_MISSED_SKIP = "skip"
NOTIFY_FAILURES = "failures"
NOTIFY_ALWAYS = "always"
NOTIFY_NEVER = "never"
NOTIFY_MODES = (NOTIFY_FAILURES, NOTIFY_ALWAYS, NOTIFY_NEVER)


@dataclass(frozen=True)
class TaskRecord:
    """One scheduled task.

    Timestamps are UTC ISO8601 with seconds precision (string order == time
    order). ``next_run_at``/``due_at`` are None — not "" — when there is no
    further occurrence: an empty string sorts before every timestamp and would
    make a finished task permanently due.

    ``next_run_at`` is the honest scheduled instant; ``due_at`` is the same plus
    this task's stable jitter, and is what the ticker compares against. Keeping
    them apart is why jitter never accumulates."""

    id: str
    agent: str
    name: str
    channel_id: str
    conversation_id: str
    kind: str  # KIND_THREAD | KIND_DM | KIND_CHANNEL
    mode: str  # MODE_TURN | MODE_PROMPT
    prompt: str
    trigger_kind: str  # 'at' | 'every' | 'cron'
    trigger_spec: str  # what the human wrote, kept for display
    interval_s: int
    cron_expr: str
    timezone: str  # IANA name; "" = UTC
    anchor_at: str  # the phase an interval counts from
    next_run_at: str | None
    due_at: str | None
    jitter_s: int
    state: str
    claim_owner: str  # the scheduler holding the lease
    claim_at: str
    lease_until: str  # a lease past this is reclaimable
    on_missed: str  # 'run' | 'skip'
    notify: str  # 'failures' | 'always' | 'never'
    deadline_s: int  # 0 = the engine default
    created_by: str
    created_by_username: str
    created_at: str
    updated_at: str
    last_run_at: str
    last_status: str
    run_count: int
    miss_count: int
    consecutive_failures: int


@dataclass(frozen=True)
class TaskRunRecord:
    """One occurrence, ever — including the ones that never started.

    ``scheduled_at`` is the occurrence itself, not when we got to it; the store
    keeps (task_id, scheduled_at) unique, which is the hard guarantee that an
    occurrence cannot fire twice. ``coalesced`` says how many occurrences this
    row stands for after an outage."""

    run_id: str
    task_id: str
    agent: str  # denormalized: runs outlive the task they belong to
    scheduled_at: str
    started_at: str  # "" when nothing started (missed, overlap)
    finished_at: str
    status: str
    trigger: str  # 'schedule' | 'catchup' | 'manual'
    owner: str  # the scheduler that ran it
    duration_ms: int
    detail: str  # one line: why this status
    reply_chars: int
    tool_calls: int
    coalesced: int
    notified: int  # 0/1 — the user notice was delivered


@dataclass(frozen=True)
class SchedulerHeartbeat:
    """Proof of life, written at the END of every tick — a fresh timestamp means
    a tick COMPLETED, so an idle scheduler is distinguishable from a dead one."""

    scheduler_id: str
    pid: int
    version: str
    started_at: str
    last_tick_at: str
    tick_seq: int
    interval_s: float
    next_wake_at: str | None
    next_task_id: str
    next_task_name: str
    running_count: int
    tasks_total: int
    last_error: str
    last_error_at: str


class TaskStore(Protocol):
    """Scheduled work and its run history. Two methods are atomic state machines
    (``claim_due``, ``complete_run``); the rest are reads or plain admin."""

    async def create_task(self, task: TaskRecord) -> None: ...

    async def get_task(self, task_id: str) -> TaskRecord | None: ...

    async def find_task(self, agent: str, name: str) -> TaskRecord | None: ...

    async def list_tasks(
        self, agent: str | None = None, *, conversation_id: str | None = None
    ) -> list[TaskRecord]: ...

    async def due_tasks(self, now: str, *, limit: int = 50) -> list[TaskRecord]:
        """Tasks whose ``due_at`` has arrived, soonest first. Includes RUNNING
        ones deliberately: a running task past its own due_at IS the overlap."""
        ...

    async def peek_next(self) -> TaskRecord | None:
        """The soonest idle task — the heartbeat's "when do I next wake, and for
        which task"."""
        ...

    async def delete_task(self, task_id: str) -> TaskRecord | None: ...

    async def set_paused(
        self, task_id: str, paused: bool, *, now: str,
        next_run_at: str | None = None, due_at: str | None = None,
    ) -> bool:
        """Pausing clears the schedule; resuming is given a freshly computed one,
        so a resumed task returns to its original phase."""
        ...

    async def request_run_now(self, task_id: str, *, now: str) -> bool:
        """Bring the next occurrence forward to ``now``. The CLI runs in another
        container with no gateways, so it asks — the engine executes."""
        ...

    async def claim_due(
        self, *, task_id: str, seen_due_at: str, next_run_at: str | None,
        due_at: str | None, run: TaskRunRecord, owner: str, lease_until: str, now: str,
    ) -> TaskRunRecord | None:
        """Take the lease on one occurrence and open its run row, atomically —
        advancing the schedule in the same transaction, so a crash an instant
        later cannot re-fire it. None means the race was lost."""
        ...

    async def record_skip(
        self, *, task_id: str, seen_due_at: str, run: TaskRunRecord,
        next_run_at: str | None, due_at: str | None, now: str,
    ) -> bool:
        """An occurrence that will not run (missed, overlap): write its row and
        advance the schedule under the same compare-and-swap, without a lease."""
        ...

    async def complete_run(
        self, *, run_id: str, task_id: str, status: str, finished_at: str,
        duration_ms: int, detail: str = "", reply_chars: int = 0,
        tool_calls: int = 0, failed: bool = False, release_state: str = STATE_IDLE,
    ) -> None:
        """Close the run and release the lease atomically — a run can never end
        finished-but-still-claimed."""
        ...

    async def reclaim_orphans(
        self, *, scheduler_id: str, now: str
    ) -> list[TaskRunRecord]:
        """Runs left ``running`` by a dead engine (or an expired lease) become
        ``interrupted``, and their tasks idle again."""
        ...

    async def list_runs(self, task_id: str, *, limit: int = 20) -> list[TaskRunRecord]: ...

    async def unnotified_runs(self, *, limit: int = 20) -> list[TaskRunRecord]:
        """Finished runs whose user notice has not been delivered — how "a
        failure always reaches the user" survives the crash that caused it."""
        ...

    async def mark_notified(self, run_id: str) -> None: ...

    async def prune_runs(self, *, keep_per_task: int, before: str) -> int: ...


class SchedulerStateStore(Protocol):
    async def write_heartbeat(self, beat: SchedulerHeartbeat) -> None: ...

    async def read_heartbeat(self) -> SchedulerHeartbeat | None: ...


# -- approvals ------------------------------------------------------------------

# What a window or a ledger row is about. The engine's own consumer is the tool
# gate; an application may authorize other things and brings its own word for
# them, which is why this is a plain string in the schema rather than an enum.
KIND_TOOL = "tool"

# How a request a human was asked about came out. A closed set, like the run
# statuses: the ledger is the only place the real reason is written, so "why did
# that not work" has to be greppable here or it is nowhere. An application that
# authorizes something of its own extends this with its own words — see the
# secret broker, whose refusals are its own business and not the engine's.
DECISION_APPROVED_ONCE = "approved_once"  # a human allowed this one call
DECISION_APPROVED_GRANT = "approved_grant"  # a human opened a window; this call used it
DECISION_REUSED_GRANT = "reused_grant"  # served by a window opened earlier
DECISION_DENIED = "denied"  # a human refused
DECISION_TIMEOUT = "timeout"  # nobody answered in time
DECISION_NO_APPROVER = "no_approver"  # approval needed, nobody configured to give it
DECISIONS = (
    DECISION_APPROVED_ONCE, DECISION_APPROVED_GRANT, DECISION_REUSED_GRANT,
    DECISION_DENIED, DECISION_TIMEOUT, DECISION_NO_APPROVER,
)


@dataclass(frozen=True)
class ApprovalGrant:
    """A window during which one principal may do one thing without asking again.

    Keyed by ``(kind, principal, scope)`` rather than by anything secret-shaped,
    because the same window means "this agent may use github-token" and "this
    agent may run bash" — the two consumers differ in what they put in ``scope``,
    not in what a window is.

    It holds no value and no capability, only the permission. Whatever the
    window covers is still fetched or checked afresh on every use, so revoking a
    window — or changing what it points at — takes effect on the very next call.
    """

    id: str
    kind: str  # KIND_TOOL, or whatever else an application authorizes
    principal: str  # who was trusted — an agent name
    scope: str  # with what — a secret name, a tool name
    granted_by: str  # the user id that clicked
    granted_at: str
    expires_at: str
    revoked_at: str = ""


@dataclass(frozen=True)
class ApprovalAudit:
    """One request for authorization, ever — granted or not.

    Append-only, and it never holds a secret value. ``detail`` is what the human
    was shown before deciding (the argv, the tool's arguments): keeping it is
    what makes an approval reviewable after the fact. ``request_id`` ties the
    rows of one multi-part request together.
    """

    id: str
    at: str
    kind: str
    principal: str
    scope: str
    reason: str
    detail: str
    decision: str
    approver: str  # the user id that decided; "" when nobody was asked
    grant_id: str  # the window this was served under, if any
    request_id: str  # shared by every row of one request
    duration_ms: int  # how long the caller waited, mostly on the human


class ApprovalStore(Protocol):
    """Windows a human left open, and the ledger of what was asked.

    Neither is secret-shaped: the same two tables answer "may this agent use
    that credential" and "may this agent run that tool", because a window and a
    ledger row are the same idea either way.
    """

    async def create_grant(self, grant: ApprovalGrant) -> None: ...

    async def live_grant(
        self, kind: str, principal: str, scope: str, *, now: str
    ) -> ApprovalGrant | None:
        """The unexpired, unrevoked window covering this triple, if one is open."""
        ...

    async def list_grants(
        self, *, now: str, kind: str = "", include_dead: bool = False
    ) -> list[ApprovalGrant]: ...

    async def revoke_grant(self, grant_id: str, *, now: str) -> bool: ...

    async def revoke_scope(self, kind: str, scope: str, *, now: str) -> int:
        """Close every window over one scope, returning how many. What deleting
        the thing the windows point at has to do, or a principal keeps reaching
        something whose permission is gone."""
        ...

    async def record_decision(self, record: ApprovalAudit) -> None: ...

    async def list_audit(
        self, *, limit: int = 50, kind: str = "", principal: str = "", scope: str = ""
    ) -> list[ApprovalAudit]: ...


class AgentStore(Protocol):
    """Persistence for the agent registry (synced from profiles at boot)."""

    async def upsert_agent(self, info: AgentInfo) -> None: ...

    async def list_agents(self) -> list[AgentInfo]: ...


class SessionStore(Protocol):
    """Inventory of conversations -> runtime sessions, surviving restarts."""

    async def get_or_create(
        self, agent: str, channel_id: str, conversation_id: str, kind: str,
        user_id: str = "",
    ) -> tuple[SessionRecord, bool]:
        """Returns (record, created); created=True on first sight of the
        conversation — flows use it to backfill thread context once. ``user_id``
        (the triggering user) is recorded as the conversation's last user, so a
        mid-turn tool can address them."""
        ...

    async def touch(self, agent: str, conversation_id: str, user_id: str = "") -> None: ...

    async def list(self, agent: str | None = None) -> list[SessionRecord]: ...

    async def delete(self, agent: str, conversation_id: str) -> SessionRecord | None: ...

    async def get_by_runtime_session(self, runtime_session_id: str) -> SessionRecord | None:
        """Reverse lookup: which conversation a runtime session serves. Used to
        give a tool the ConversationRef of the turn it runs inside."""
        ...

    async def mark_processed(self, agent: str, post_id: str) -> bool:
        """True on first sight of the post for this agent; False on a replay
        (WS reconnects redeliver). Callers drop already-processed posts."""
        ...

    async def close(self) -> None: ...
