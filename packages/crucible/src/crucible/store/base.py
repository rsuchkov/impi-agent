"""Session-inventory port + the deterministic session-id rule.

The id is derived, never invented: ``<agent>--<conversation_id>``, coerced to the
runtime's allowed alphabet. Deterministic derivation means a lost DB still resumes
runtime-side memory from disk — the DB only exists so humans (and the cleanup CLI)
can enumerate what sessions exist. The runtime applies the same coercion
defensively when it starts a session; ``tests/test_session_store.py`` pins the
agreement so the two never drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Protocol

from crucible.ports.chat.directory import AgentInfo
from crucible.ports.lease import Lease
from crucible.ports.replica import Replicas

# Which implementation holds the inventory. A closed set, like the run statuses
# below: the name comes from configuration a person typed, so an unknown one has
# to be caught and said out loud rather than falling through to a default.
SQLITE = "sqlite"
MONGO = "mongo"
STORE_BACKENDS = (SQLITE, MONGO)

# How much of the digest a coerced id carries: enough that two conversations of
# one agent colliding is not a thing that happens, short enough to read.
_DIGEST_CHARS = 12


def derive_runtime_session_id(agent: str, conversation_id: str) -> str:
    """Deterministic, filesystem-safe session key from (agent, conversation).

    Deterministic so the DB stays inventory, not source of truth: the key is
    recomputable from the pair alone. The charset is a portable safe-identifier
    set (also valid as the runtime's session id); the runtime re-coerces to the
    same set at its own boundary, so stored key and on-disk session agree.

    Coercion alone is lossy — ``john@x`` and ``john-x`` both clean to
    ``john-x`` — so whenever it changed anything, a digest of the original is
    appended. Two conversations then never share a session, and a key the
    platform already spelled safely (every Slack and Mattermost id) is unchanged.
    """
    raw = f"{agent}--{conversation_id}"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")
    if cleaned == raw:
        return cleaned
    digest = hashlib.sha256(raw.encode()).hexdigest()[:_DIGEST_CHARS]
    return f"{cleaned or 'session'}-{digest}"


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
    # When the engine last started a turn here by itself (a scheduled run, an
    # event the application reported), ISO8601 UTC; "" when it never has. What
    # a program polls to learn which of a person's conversations moved while
    # nobody was looking.
    last_engine_turn_at: str = ""
    # What the engine knows about the last turn here that was cut short (a time
    # limit, a stall): which tools had finished and which was still running, in
    # a sentence for the model. Put into the next turn's prompt and cleared;
    # "" when the last turn ended on its own.
    cut_short: str = ""
    # Which gateways this conversation is live in, and how to address it there:
    # JSON ``{gateway: {"channel_id", "conversation_id", "kind"}}`` (see
    # ``gateway_addresses``); the platform's own ids, which differ from this
    # record's key when the application keys a conversation across gateways.
    gateways: str = ""
    # The gateway the last message came through: where a reply or a card goes
    # when nothing says otherwise.
    last_gateway: str = ""


@dataclass(frozen=True)
class GatewayAddress:
    """Where a conversation is on one gateway: the platform's own ids."""

    channel_id: str
    conversation_id: str
    kind: str


def gateway_addresses(record: "SessionRecord") -> dict[str, GatewayAddress]:
    """The gateways a conversation is live in, by name, from the record."""
    if not record.gateways:
        return {}
    try:
        raw = json.loads(record.gateways)
    except json.JSONDecodeError:
        return {}
    return {
        name: GatewayAddress(str(a.get("channel_id", "")), str(a.get("conversation_id", "")), str(a.get("kind", "")))
        for name, a in raw.items() if isinstance(a, dict)
    }


def with_gateway_address(record: "SessionRecord", gateway: str, address: GatewayAddress) -> str:
    """The record's ``gateways`` JSON with ``gateway`` set to ``address``."""
    current = {name: {"channel_id": a.channel_id, "conversation_id": a.conversation_id, "kind": a.kind}
               for name, a in gateway_addresses(record).items()}
    current[gateway] = {"channel_id": address.channel_id, "conversation_id": address.conversation_id, "kind": address.kind}
    return json.dumps(current, ensure_ascii=False, sort_keys=True)


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
    # The gateway the widget was posted through: a click on it comes back as a
    # message through that gateway.
    gateway: str = ""


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
    # Who answers this form when it comes back. "" — the default and everything
    # an agent opens — means the values go into the conversation as a synthetic
    # message. A name means the application answers it instead, and the routing
    # is written down HERE rather than worked out by asking handlers whether a
    # token is theirs: a handler that guessed wrong, or failed, would otherwise
    # let the values fall through into a conversation, and for some forms that
    # is exactly the thing that must not happen.
    handler: str = ""


class FormStore(Protocol):
    """Pending forms. Unlike a one-shot interaction the token is READ on the
    open-click (to build the dialog) and only deleted on submit/cancel."""

    async def create_form(self, record: FormRecord) -> None: ...

    async def get_form(self, token: str) -> FormRecord | None: ...

    async def delete_form(self, token: str) -> None: ...


@dataclass(frozen=True)
class TraceRecord:
    """What one turn's tool calls were, kept so the widget under the reply can
    be opened long after the turn — minutes or a restart later.

    ``calls`` is opaque JSON, like a form's ``spec``: the widget owns its shape
    and the store only keeps it. It holds names, arguments, outcomes and
    timings; never results, which is where a value the agent was granted could
    surface. ``post_id`` names the widget message the trace is drawn in.
    """

    token: str
    agent: str
    channel_id: str
    conversation_id: str
    kind: str
    created_at: str
    finished_at: str
    calls: str
    post_id: str = ""


class TraceStore(Protocol):
    """One trace per turn that called tools, written once when the turn ends and
    read on every click. Pruned by age, since nobody opens last month's."""

    async def create_trace(self, record: TraceRecord) -> None: ...

    async def get_trace(self, token: str) -> TraceRecord | None: ...

    async def prune_traces(self, *, before: str) -> int:
        """Delete traces that finished before ``before``; return how many."""
        ...


# -- scheduled work -----------------------------------------------------------

# How a due task runs: as an ordinary turn in its own conversation (memory, the
# reply lands there), or as a fresh memoryless run whose text is posted for it.
MODE_TURN = "turn"
MODE_PROMPT = "prompt"
# A watch: the scheduler asks the application's hook, every period, whether
# what the task waits for has happened; a turn runs once, when it has.
MODE_WATCH = "watch"
MODES = (MODE_TURN, MODE_PROMPT, MODE_WATCH)
# The modes a person schedules by hand; a watch is opened by a program.
SCHEDULED_MODES = (MODE_TURN, MODE_PROMPT)

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
RUN_GONE = "gone"  # a watch ended with nothing to report: the application said so
RUN_STATUSES = (
    RUN_RUNNING, RUN_OK, RUN_EMPTY, RUN_TIMEOUT, RUN_ERROR, RUN_DEADLINE,
    RUN_INTERRUPTED, RUN_NO_AGENT, RUN_NO_CONVERSATION, RUN_MISSED,
    RUN_OVERLAP, RUN_CANCELLED, RUN_DUPLICATE, RUN_GONE,
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
    # A watch's own: the key the application's hook is asked about (opaque to
    # the engine), and when the watch gives up — "" for the other modes.
    watch_key: str = ""
    expires_at: str = ""


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

    async def reschedule(
        self, task_id: str, *, next_run_at: str | None, due_at: str | None, now: str
    ) -> None:
        """Move the schedule without touching the claim. Unlike the state
        machines above this takes no CAS token, so it is for a caller that
        already knows nothing is running — repairing a schedule after its
        trigger was edited, or putting one back after a test moved it."""
        ...


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
DECISION_ABANDONED = "abandoned"  # the caller gave up (turn aborted) before anyone answered
DECISIONS = (
    DECISION_APPROVED_ONCE, DECISION_APPROVED_GRANT, DECISION_REUSED_GRANT,
    DECISION_DENIED, DECISION_TIMEOUT, DECISION_NO_APPROVER, DECISION_ABANDONED,
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


@dataclass(frozen=True)
class ConnectionRecord:
    """One person's account at one provider, connected over OAuth: what a tool
    acting as that person uses. One per ``(owner, provider)``.

    The tokens are sealed by whoever writes the record (``crucible.connections``)
    and opaque here; the rest — whose it is, which account, until when — stays
    readable, so the store can be looked at without the key.
    ``(connect_id, version)`` names the credential an operation saw: ``connect_id``
    is new at every connect, ``version`` counts refreshes, and every write that
    starts from a record it read is conditional on both.
    """

    owner: str
    provider: str
    access_sealed: str
    refresh_sealed: str  # "" when the provider issued none
    expires_at: str  # "" when the access token does not expire
    account_id: str
    account_username: str
    account_name: str
    account_email: str
    scope: str
    connect_id: str
    version: int = 0


@dataclass(frozen=True)
class ConnectionLinkRecord:
    """A connect someone started and has not finished: single use, short-lived.

    Keyed by a digest of the ``state`` the person carries through the provider,
    never by the state itself — a reader of the store cannot finish a link."""

    state_digest: str
    owner: str
    provider: str
    verifier_sealed: str  # the PKCE verifier, sealed like the tokens
    expires_at: str
    # Where the application wants to say it went once the connect finishes —
    # opaque here (a message to redraw, say), handed back in ``Connected``.
    return_to: str = ""


class ConnectionStore(Protocol):
    """Connected accounts, and the lease that keeps two replicas from refreshing
    one at once — a provider that issues a new refresh token on every refresh
    invalidates the old one, so the loser of a race would be left with a dead
    pair."""

    async def get_connection(self, owner: str, provider: str) -> ConnectionRecord | None: ...

    async def list_connections(self, owner: str) -> list[ConnectionRecord]: ...

    async def save_connection(self, record: ConnectionRecord) -> None:
        """Insert or replace whatever is stored for the pair, ending any lease."""
        ...

    async def claim_refresh(
        self, owner: str, provider: str, *, connect_id: str, version: int, now: str, until: str
    ) -> bool:
        """Take the refresh lease until ``until`` — only while the record is
        still that credential and no lease runs past ``now``."""
        ...

    async def release_refresh(self, owner: str, provider: str, *, connect_id: str, version: int) -> None:
        """End the lease early, if the record is still that credential."""
        ...

    async def replace_connection(self, record: ConnectionRecord, *, expected_version: int) -> bool:
        """Write ``record`` only over the same grant at ``expected_version``,
        ending the lease. False when the record changed or went away meanwhile."""
        ...

    async def delete_connection(
        self, owner: str, provider: str, *, connect_id: str | None = None, version: int | None = None
    ) -> bool:
        """Delete the pair's record — conditional on each of ``connect_id`` and
        ``version`` that is given. Whether one was deleted."""
        ...


class LeaseStore(Lease, Protocol):
    """The store's leases (``ports.lease.Lease``): named, one holder at a time
    across every process that shares the store."""


class ReplicaStore(Replicas, Protocol):
    """Which replica owns which conversation (``ports.replica.Replicas``). A
    replica is alive while it holds the lease ``replica:<id>``, whose holder is
    its address — so the registry is answered from the leases too."""


class ConnectionLinkStore(Protocol):
    """Connects in progress."""

    async def put_link(self, link: ConnectionLinkRecord) -> None: ...

    async def take_link(self, state_digest: str) -> ConnectionLinkRecord | None:
        """The link, removed in the same step: a second take finds nothing."""
        ...


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

    async def touch(
        self, agent: str, conversation_id: str, user_id: str = "", *,
        engine_turn: bool = False, gateway: str = "",
    ) -> None:
        """A turn ended here: refresh ``last_active`` (and the last user when
        given); ``engine_turn`` also stamps ``last_engine_turn_at``; ``gateway``
        says which gateway the turn's message came through (``last_gateway``)."""
        ...

    async def set_gateway_address(
        self, agent: str, conversation_id: str, gateway: str, address: GatewayAddress
    ) -> None:
        """Remember how this conversation is addressed on ``gateway`` — the
        platform's own ids — so a card or a reply can reach it there."""
        ...

    async def set_cut_short(self, agent: str, conversation_id: str, note: str) -> None:
        """Remember — or with "" forget — what the engine knows about a turn here
        that was cut short, for the next turn's prompt."""
        ...

    async def list(self, agent: str | None = None) -> list[SessionRecord]: ...

    async def recent_engine_turns(self, agent: str, since: str) -> list[SessionRecord]:
        """The agent's conversations in which the engine started a turn at or
        after ``since`` (ISO8601 UTC), newest first."""
        ...

    async def delete(self, agent: str, conversation_id: str) -> SessionRecord | None: ...

    async def get_by_runtime_session(self, runtime_session_id: str) -> SessionRecord | None:
        """Reverse lookup: which conversation a runtime session serves. Used to
        give a tool the ConversationRef of the turn it runs inside."""
        ...

    async def mark_processed(self, agent: str, post_id: str) -> bool:
        """True on first sight of the post for this agent; False on a replay
        (WS reconnects redeliver). Callers drop already-processed posts."""
        ...

    async def prune_processed(self, *, before: str) -> int:
        """Forget posts first seen before ``before`` (ISO8601 UTC). A replay
        only ever arrives soon after the original, so the record is worth
        keeping for days, not for the life of the deployment. Returns how many
        were forgotten."""
        ...

    async def close(self) -> None: ...


class Store(
    SessionStore,
    InteractionStore,
    FormStore,
    TaskStore,
    SchedulerStateStore,
    ApprovalStore,
    AgentStore,
    TraceStore,
    ConnectionStore,
    ConnectionLinkStore,
    LeaseStore,
    ReplicaStore,
    Protocol,
):
    """Every question the inventory answers, in one type.

    The protocols above split it by subject so a caller can ask for the slice it
    needs — a flow that only touches conversations should not be handed the
    scheduler. A BACKEND, though, owes all of them: this is the name for that
    obligation, and what a store factory returns.
    """
