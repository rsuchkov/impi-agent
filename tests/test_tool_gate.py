"""The confirmation a tool declares, enforced by the server (interactions/toolgate.py).

The gate that matters is this one. The runtime's extension asks before it makes
the call, but the token it authenticates with lives in the agent's own
environment — so a shell in that container can POST the tool server directly and
never see the question. These tests are mostly about that call being refused.

The other half is the window: a human can say "yes, for a while", and the second
call inside it does not ask again.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from crucible.approvals import (
    ANSWER_DENY,
    ANSWER_ONCE,
    APPROVAL_KEY,
    PendingApprovals,
    card_markdown,
)
from crucible.approvals.card import card_rows
from crucible.config import ToolSettings
from crucible.interactions import toolgate
from crucible.interactions.toolgate import ToolGate, tool_scope
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.types import (
    ACTION_SELECT,
    KIND_DM,
    ApprovalCard,
    CallPreview,
    ConversationRef,
    PreviewRow,
)
from crucible.store.base import KIND_TOOL
from crucible.store.sqlite import SqliteSessionStore
from crucible.tools.base import CAP_CONFIRMATION, Tool, ToolContext
from crucible.tools.registry import ToolRegistry
from crucible.tools.server import ToolServer
from crucible.tools.wiring import ToolWiring, _gate_tools
from impi.engine_tools import engine_tool_registry

AGENTS = [
    AgentInfo(name="assistant", role="r", description="", username="assistant", user_id="bot-1")
]
TOKEN = {"X-Tool-Token": "secret-tok"}
CLICKER = "uid-roman"


class Dangerous(Tool):
    name = "dangerous"
    description = "does something worth asking about"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    requires_confirmation = True

    def __init__(self) -> None:
        self.ran = 0
        self.preview: CallPreview | None = None
        self.described: list[ToolContext] = []

    async def describe(self, ctx: ToolContext, args: dict[str, Any]) -> CallPreview | None:
        self.described.append(ctx)
        return self.preview

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        self.ran += 1
        return {"ok": True}


class Harmless(Tool):
    name = "harmless"
    description = "does not need asking about"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    requires_confirmation = False

    def __init__(self) -> None:
        self.ran = 0

    async def execute(self, ctx: ToolContext, args: dict[str, Any]) -> Any:
        self.ran += 1
        return {"ok": True}


class FakeDirectory:
    def agent_user_ids(self):
        return frozenset(a.user_id for a in AGENTS)

    def list_agents(self):
        return list(AGENTS)


@dataclass
class Posted:
    text: str
    actions: list
    post_id: str
    card: ApprovalCard | None = None
    ref: ConversationRef | None = None  # where it was posted


@dataclass
class Updated:
    post_id: str
    card: ApprovalCard
    actions: list


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[Posted] = []
        self.updated: list[Updated] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_actions(self, ref, text, actions, *, callback_url) -> str:
        self.posts.append(Posted(text, list(actions), f"post-{len(self.posts) + 1}"))
        return self.posts[-1].post_id

    async def post_confirmation(self, ref, card, actions, *, callback_url) -> str:
        # The text form, as a chat platform would show it, so the assertions on
        # what a person reads stay readable; the card itself beside it.
        self.posts.append(Posted(card_markdown(card), list(actions), f"post-{len(self.posts) + 1}", card, ref))
        return self.posts[-1].post_id

    async def update_confirmation(self, post_id, card, actions=None, *, callback_url="") -> None:
        self.updated.append(Updated(post_id, card, list(actions or [])))

    async def retract(self, post_id: str, text: str) -> None:
        self.retracted.append((post_id, text))


@dataclass
class FakePresence:
    chat: FakePoster
    by_gateway: dict[str, FakePoster] | None = None

    def poster(self, agent: str, gateway: str = ""):
        if self.by_gateway and gateway in self.by_gateway:
            return self.by_gateway[gateway]
        return self.chat

    def sink(self, agent: str):
        return None


class FakeClock:
    """A TurnClock that remembers which session was paused, and when."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def human_wait(self, session_id: str):
        return self._pause(session_id)

    @contextlib.asynccontextmanager
    async def _pause(self, session_id: str):
        self.events.append(("enter", session_id))
        try:
            yield
        finally:
            self.events.append(("exit", session_id))


@dataclass
class Rig:
    server: ToolServer
    store: SqliteSessionStore
    poster: FakePoster
    approvals: PendingApprovals
    tool: Dangerous
    plain: Harmless
    session: str
    port: int
    clock: FakeClock


async def _rig(tmp_path: Path, port: int, *, gated: bool = True, **over) -> Rig:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
    poster = FakePoster()
    approvals = PendingApprovals()
    gate = ToolGate(
        FakePresence(poster, over.pop("by_gateway", None)), store, store, approvals,  # type: ignore[arg-type]
        callback_url="http://engine/interact",
        timeout_s=over.pop("timeout_s", 5.0),
        max_grant_s=over.pop("max_grant_s", 900),
        approvers=frozenset(over.pop("approvers", ())),
        replica_id=over.pop("replica_id", ""),
        people=over.pop("people", None),
    )
    dangerous, plain = Dangerous(), Harmless()
    clock = FakeClock()
    registry = ToolRegistry((dangerous, plain))  # type: ignore[arg-type]
    server = ToolServer(
        registry,
        directory=FakeDirectory(),  # type: ignore[arg-type]
        admins={},
        tokens={"secret-tok": "assistant"},
        allowlists={"assistant": frozenset({"dangerous", "harmless"})},
        port=port,
        tool_gate=gate if gated else None,  # type: ignore[arg-type]
        clock=clock,
    )
    await server.start()
    return Rig(
        server, store, poster, approvals, dangerous, plain, record.runtime_session_id, port, clock
    )


async def _call(rig: Rig, name: str, *, conversation: str = "") -> int:
    status, _ = await _call_told(rig, name, conversation=conversation)
    return status


async def _call_told(rig: Rig, name: str, *, conversation: str = "") -> tuple[int, str]:
    """The status and what the model is told — the error text, or ""."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{rig.port}/tool/{name}",
            json={},
            headers={**TOKEN, "X-Runtime-Session": conversation or rig.session},
        ) as response:
            body = await response.json()
            return response.status, str(body.get("error", ""))


async def _answer(rig: Rig, value: str) -> Posted:
    for _ in range(400):
        for card in reversed(rig.poster.posts):
            token = str(card.actions[0].context.get(APPROVAL_KEY, ""))
            if token and rig.approvals.pending(token):
                rig.approvals.resolve(token, value, CLICKER)
                return card
        await asyncio.sleep(0.005)
    raise AssertionError("nobody was asked")


async def _close(rig: Rig) -> None:
    await rig.server.stop()
    await rig.store.close()


# -- the bypass ----------------------------------------------------------------


async def test_a_call_that_skips_the_runtime_is_still_asked_about(tmp_path: Path) -> None:
    """The whole point: this request never went through the extension, and it
    still has to wait for a human."""
    rig = await _rig(tmp_path, 8541)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        assert rig.tool.ran == 1
        # The verdict as a code too, for a program drawing its own line.
        assert (rig.poster.updated[-1].card.verdict, rig.poster.updated[-1].card.grant_s) == ("allowed_once", 0)
    finally:
        await _close(rig)


async def test_a_refusal_means_the_tool_does_not_run(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8542)
    try:
        pending = asyncio.create_task(_call_told(rig, "dangerous"))
        await _answer(rig, ANSWER_DENY)
        assert await pending == (403, "declined by the user")
        assert rig.tool.ran == 0
        assert rig.poster.updated[-1].card.verdict == "denied"
    finally:
        await _close(rig)


async def test_nobody_answering_is_a_refusal_the_model_can_tell_from_a_no(tmp_path: Path) -> None:
    """Silence refuses the call, like a no — but the model is told it was
    silence: a person who is away may still be asked when they are back, a
    person who said no may not."""
    rig = await _rig(tmp_path, 8543, timeout_s=0.05)
    try:
        status, told = await _call_told(rig, "dangerous")
        assert status == 403
        assert "nobody answered" in told and "not a refusal" in told
        assert "declined" not in told
        assert rig.tool.ran == 0
        # Closed as a card, not a sentence: the same post, the verdict for a headline.
        closed = rig.poster.updated[-1]
        assert closed.post_id == rig.poster.posts[-1].post_id and closed.actions == []
        assert "in time" in closed.card.headline and "dangerous" in closed.card.headline
        assert closed.card.expires_at == ""  # nothing left to count down to
        assert closed.card.verdict == "expired"
        assert rig.poster.retracted == []
        assert (await rig.store.list_audit())[0].decision == "timeout"
    finally:
        await _close(rig)


async def test_with_no_gate_a_confirmed_tool_is_refused_not_run(tmp_path: Path) -> None:
    """Fail closed. A composition with no way to ask cannot answer yes on a
    human's behalf."""
    rig = await _rig(tmp_path, 8544, gated=False)
    try:
        assert await _call(rig, "dangerous") == 403
        assert rig.tool.ran == 0
    finally:
        await _close(rig)


async def test_a_tool_that_needs_no_confirmation_is_not_gated(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8545)
    try:
        assert await _call(rig, "harmless") == 200
        assert rig.plain.ran == 1
        assert rig.poster.posts == []  # nobody was disturbed
    finally:
        await _close(rig)


# -- the card ------------------------------------------------------------------


async def test_the_card_shows_what_the_tool_says_the_call_would_do(tmp_path: Path) -> None:
    """The arguments are the model's account; the preview is the tool's — read
    from the system, with the current value beside the new one."""
    rig = await _rig(tmp_path, 8553)
    rig.tool.preview = CallPreview(
        title="move IPA-7 to another status",
        rows=(
            PreviewRow("Issue", "IPA-7 — Fix the login page"),
            PreviewRow("Status", "Done", before="In Progress"),
        ),
        danger=True,
    )
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        assert "⚠️" in card.text
        assert "⚠️ **move IPA-7 to another status**" in card.text  # the title is the heading
        assert "**Issue:** `IPA-7 — Fix the login page`" in card.text
        assert "**Status:** `In Progress → Done`" in card.text
        assert "Arguments" not in card.text
        # describe() ran with the same context execute() gets: as the caller.
        assert [ctx.agent_name for ctx in rig.tool.described] == ["assistant"]
        assert rig.tool.described[0].runtime_session_id == rig.session
    finally:
        await _close(rig)


async def test_a_preview_that_fails_leaves_the_arguments_on_the_card(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8554)

    async def boom(ctx, args):
        raise RuntimeError("the record could not be read")

    rig.tool.describe = boom  # type: ignore[method-assign]
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, ANSWER_ONCE)
        assert await pending == 200  # the decision was still possible
        assert "⚙️" in card.text and "wants to run dangerous" in card.text
    finally:
        await _close(rig)


# -- the caller hangs up -------------------------------------------------------


async def _call_and_hang_up(rig: Rig, name: str, *, after: float) -> None:
    """Start a call and drop the connection ``after`` seconds — what the
    runtime's extension does when the turn is aborted or it gives up waiting."""
    timeout = aiohttp.ClientTimeout(total=after)
    with contextlib.suppress(asyncio.TimeoutError, aiohttp.ClientError):
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await session.post(
                f"http://127.0.0.1:{rig.port}/tool/{name}",
                json={}, headers={**TOKEN, "X-Runtime-Session": rig.session},
            )


async def test_a_call_abandoned_while_waiting_cannot_be_approved_later(tmp_path: Path) -> None:
    """The model was told the call failed; a click after that must not run it —
    not now, not as a duplicate of the retry the model is about to make."""
    rig = await _rig(tmp_path, 8552)
    try:
        await _call_and_hang_up(rig, "dangerous", after=0.2)
        for _ in range(400):
            if rig.poster.updated:
                break
            await asyncio.sleep(0.005)
        card = rig.poster.posts[0]
        token = str(card.actions[0].context[APPROVAL_KEY])
        assert not rig.approvals.pending(token)
        assert [(u.post_id, u.card.headline) for u in rig.poster.updated] == [
            (card.post_id, toolgate._ABANDONED.format(tool="dangerous")),
        ]
        assert rig.poster.updated[-1].card.verdict == "withdrawn"
        assert rig.approvals.resolve(token, ANSWER_ONCE, CLICKER).name == "NOT_MINE"
        assert rig.tool.ran == 0
        assert [row.decision for row in await rig.store.list_audit()] == ["abandoned"]
    finally:
        await _close(rig)


# -- the turn's clock ----------------------------------------------------------


async def test_the_turn_clock_is_paused_while_the_gate_waits(tmp_path: Path) -> None:
    """A person deciding is not the runtime being stuck: the wait for the card
    is taken out of the turn's timeout — for exactly the session that is waiting,
    and only for as long as it waits."""
    rig = await _rig(tmp_path, 8551)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        for _ in range(400):
            if rig.clock.events:
                break
            await asyncio.sleep(0.005)
        assert rig.clock.events == [("enter", rig.session)]  # paused, and still paused
        await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        assert rig.clock.events == [("enter", rig.session), ("exit", rig.session)]

        await _call(rig, "harmless")
        assert len(rig.clock.events) == 2  # nothing to wait for, nothing paused
    finally:
        await _close(rig)


# -- windows -------------------------------------------------------------------


async def test_a_window_stops_the_next_call_from_asking(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8546)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, "grant:900")
        assert await pending == 200

        assert await _call(rig, "dangerous") == 200
        assert rig.tool.ran == 2
        assert len(rig.poster.posts) == 1  # asked once, ran twice
        assert [row.decision for row in await rig.store.list_audit()] == [
            "reused_grant", "approved_grant",
        ]
    finally:
        await _close(rig)


async def test_the_window_is_capped_however_it_was_asked_for(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8547, max_grant_s=300)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, "grant:86400")
        await pending
        dropdown = next(a for a in card.actions if a.kind == ACTION_SELECT)
        assert [c.value for c in dropdown.options] == ["grant:60", "grant:300"]
        grant = (await rig.store.list_grants(now="2000-01-01T00:00:00+00:00"))[0]
        assert grant.kind == KIND_TOOL and grant.scope == tool_scope("dangerous", rig.session)
    finally:
        await _close(rig)


async def test_a_window_belongs_to_the_conversation_it_was_opened_in(tmp_path: Path) -> None:
    """The people who were asked are the people the window covers. Another
    conversation of the same agent — someone else talking to it, who saw no
    card — is asked afresh."""
    rig = await _rig(tmp_path, 8550, timeout_s=0.05)
    other, _ = await rig.store.get_or_create("assistant", "dm2", "dm2", KIND_DM)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, "grant:900")
        assert await pending == 200

        # Same agent, same tool, a different conversation: asked, and (nobody
        # answering within the short timeout) refused.
        assert await _call(rig, "dangerous", conversation=other.runtime_session_id) == 403
        assert rig.tool.ran == 1
        assert len(rig.poster.posts) == 2
        # The first conversation's window is still open.
        assert await _call(rig, "dangerous") == 200
        assert rig.tool.ran == 2
    finally:
        await _close(rig)


async def test_a_window_of_another_kind_does_not_open_a_tool(tmp_path: Path) -> None:
    """Every kind shares one table, so this is worth pinning: a window some other
    consumer opened over the name `dangerous` — a secret, say — must not let the
    tool `dangerous` run."""
    rig = await _rig(tmp_path, 8548, timeout_s=0.05)
    try:
        from crucible.store.base import ApprovalGrant

        await rig.store.create_grant(
            ApprovalGrant(
                id="gr_1", kind="secret", principal="assistant",
                scope=tool_scope("dangerous", rig.session),
                granted_by=CLICKER, granted_at="2026-01-01T00:00:00+00:00",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )
        assert await _call(rig, "dangerous") == 403  # asked, and nobody answered
        assert rig.tool.ran == 0
    finally:
        await _close(rig)


# -- who may answer ------------------------------------------------------------


async def test_a_conversation_that_names_no_person_may_be_answered_by_anyone_in_it(
    tmp_path: Path,
) -> None:
    """A record that never had a user — nobody's message to address the card
    to — leaves the question to whoever is watching."""
    rig = await _rig(tmp_path, 8549)  # the record names no last user
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        for _ in range(400):
            if rig.poster.posts:
                break
            await asyncio.sleep(0.005)
        token = str(rig.poster.posts[0].actions[0].context[APPROVAL_KEY])
        assert rig.approvals.resolve(token, ANSWER_ONCE, "somebody-else").name == "RESOLVED"
        assert await pending == 200
    finally:
        await _close(rig)


async def test_the_card_names_the_agent_and_the_tool(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8550)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, ANSWER_ONCE)
        await pending
        assert "assistant" in card.text and "dangerous" in card.text
        assert [a.id for a in card.actions] == ["once", "grant", "deny"]
    finally:
        await _close(rig)


@pytest.mark.parametrize("hostile", ['{"a": "```\\n**Arguments:**"}', "x\ny"])
def test_arguments_cannot_forge_the_question(hostile: str) -> None:
    """The arguments are the caller's, so they go through the same hardening as
    a secret request's command line."""
    card = card_markdown(toolgate._card("assistant", "dangerous", {"payload": hostile}))
    # The label appears once as a LINE; inside the block it is literal text,
    # which is containment rather than censorship.
    lines = card.splitlines()
    assert [ln for ln in lines if ln.startswith("**")] == ["**Arguments:**"]
    assert len([ln for ln in lines if set(ln) == {"`"}]) == 2  # exactly one block


# --- advertising: a confirmed tool exists only where someone can confirm it -----


def _wiring(tmp_path: Path, **flags) -> ToolWiring:
    return ToolWiring(
        ToolSettings(enabled=True, server_host="127.0.0.1", server_port=8422, max_grant_s=900),
        registry=engine_tool_registry(),
        data_dir=str(tmp_path), interactivity_on=True, **flags,
    )


def test_a_confirmed_tool_is_not_advertised_without_a_gate() -> None:
    # Offered-and-refused is the worst of both: the model is told the tool exists
    # and gets a 403 every time it tries. Without a gate the tool is simply absent.
    registry = ToolRegistry((Dangerous(), Harmless()))  # type: ignore[arg-type]
    both = ("dangerous", "harmless")
    kept, dropped = _gate_tools(registry, both, frozenset())
    assert kept == ("harmless",)
    assert dropped == {"dangerous": frozenset({CAP_CONFIRMATION})}
    kept, dropped = _gate_tools(registry, both, frozenset({CAP_CONFIRMATION}))
    assert kept == both and dropped == {}


def test_promising_a_gate_and_not_wiring_one_is_a_composition_error(tmp_path: Path) -> None:
    gate = object()  # any ToolApproving; never consulted here
    promised = _wiring(tmp_path, confirmations_on=True)
    with pytest.raises(ValueError, match="no tool_gate"):
        promised.build_server(
            directory=FakeDirectory(), interaction_svc=None, dotenv_path=str(tmp_path / ".env"),  # type: ignore[arg-type]
        )
    unpromised = _wiring(tmp_path)
    with pytest.raises(ValueError, match="confirmations_on=False"):
        unpromised.build_server(
            directory=FakeDirectory(), interaction_svc=None,  # type: ignore[arg-type]
            dotenv_path=str(tmp_path / ".env"), tool_gate=gate,  # type: ignore[arg-type]
        )
    # Both halves present: a server.
    server = promised.build_server(
        directory=FakeDirectory(), interaction_svc=None,  # type: ignore[arg-type]
        dotenv_path=str(tmp_path / ".env"), tool_gate=gate,  # type: ignore[arg-type]
    )
    assert server is not None


async def test_a_refusal_for_want_of_anyone_to_ask_is_filed_under_the_same_scope(
    tmp_path: Path,
) -> None:
    rig = await _rig(tmp_path, 8555)
    try:
        # A session the store does not know: nowhere to post, nobody to ask —
        # and the model is told that, not that somebody said no.
        status, told = await _call_told(rig, "dangerous", conversation="assistant--stranger")
        assert status == 403
        assert "nobody to ask" in told and "declined" not in told
        rows = await rig.store.list_audit()
        assert [(r.decision, r.scope) for r in rows] == [
            ("no_approver", tool_scope("dangerous", "assistant--stranger")),
        ]
    finally:
        await _close(rig)


async def test_the_gate_asks_with_the_card_as_data(tmp_path: Path) -> None:
    """The gate renders nothing: the card goes out as data — who asks, what
    for, the facts, the deadline — and each channel draws it its own way."""
    from datetime import datetime, timedelta, timezone

    rig = await _rig(tmp_path, 8556, timeout_s=90.0)
    rig.tool.preview = CallPreview("move IPA-7", (PreviewRow("Status", "Done", before="Open"),), danger=True)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        posted = await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        card = posted.card
        assert card is not None
        assert (card.agent, card.icon, card.danger) == ("assistant", "⚠️", True)
        assert card.headline == "wants to run dangerous."  # the engine's words only
        assert card.title == "move IPA-7" and card.tool == "dangerous"
        assert card.fields == (PreviewRow("Status", "Done", before="Open"),)  # what the call changes, alone
        remaining = datetime.fromisoformat(card.expires_at) - datetime.now(timezone.utc)
        assert timedelta(seconds=80) < remaining <= timedelta(seconds=90)
    finally:
        await _close(rig)


async def test_the_verdict_rewrites_the_card_with_its_facts_kept(tmp_path: Path) -> None:
    """The card is the history: what was allowed, for what, with the rows a
    person decided by still on it — and no buttons."""
    rig = await _rig(tmp_path, 8557)
    rig.tool.preview = CallPreview("move IPA-7", (PreviewRow("Status", "Done", before="Open"),))
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        asked = await _answer(rig, "grant:900")
        assert await pending == 200
        closed = rig.poster.updated[-1]
        assert closed.post_id == asked.post_id and closed.actions == []
        assert closed.card.headline == "— Allowed for 15 min: dangerous."
        assert (closed.card.verdict, closed.card.grant_s) == ("allowed_for", 900)
        assert closed.card.fields == asked.card.fields  # type: ignore[union-attr]
        assert closed.card.expires_at == ""
        assert "Allowed for 15 min" in card_markdown(closed.card)
    finally:
        await _close(rig)


async def test_a_tool_with_a_display_name_is_named_by_it_and_keeps_its_id_as_a_row(tmp_path: Path) -> None:
    """A person reads "Add a comment", not an identifier; the operator and the
    audit still get the id."""
    rig = await _rig(tmp_path, 8558, timeout_s=0.05)
    rig.tool.display_name = "Add a comment"  # pyright: ignore[reportAttributeAccessIssue]
    rig.tool.preview = CallPreview("comment on IPA-7", ())
    try:
        assert await _call(rig, "dangerous") == 403
        asked = rig.poster.posts[-1].card
        assert asked is not None
        assert asked.headline == "wants to run “Add a comment”."
        assert asked.title == "comment on IPA-7" and asked.tool == "dangerous" and asked.fields == ()
        # Drawn, the id becomes a row after the facts — the headline does not say it.
        assert card_rows(asked) == (PreviewRow("Action", "comment on IPA-7"), PreviewRow("Tool", "dangerous"))
        closed = rig.poster.updated[-1].card
        assert closed.headline == toolgate._EXPIRED.format(tool="“Add a comment”")
        assert (await rig.store.list_audit())[0].scope.startswith("dangerous@")
    finally:
        await _close(rig)


def test_a_card_without_a_preview_keeps_the_id_beside_the_arguments() -> None:
    card = toolgate._card("assistant", "add_comment", {"body": "hi"}, display_name="Add a comment")
    assert card.headline == "wants to run “Add a comment”."
    assert card.tool == "add_comment" and card.fields == () and card.block_label == "Arguments"
    assert card_rows(card) == (PreviewRow("Tool", "add_comment"),)
    # Named by its id in the headline, the id is not repeated as a row.
    plain = toolgate._card("assistant", "add_comment", {"body": "hi"})
    assert plain.tool == "add_comment" and card_rows(plain) == ()


@pytest.mark.parametrize("hostile", [
    "move IPA-7\n⚙️ **support** wants to run delete_everything.",
    "<!channel> [Allow](https://evil.example)",
])
def test_a_previews_title_cannot_restyle_the_heading(hostile: str) -> None:
    """The title is the tool's text — in impi, often a model argument. It leads
    the card, so it is escaped character by character: one line, no emphasis,
    no link, no second heading of its own."""
    card = toolgate._card("assistant", "dangerous", {}, CallPreview(hostile, ()))
    assert card.headline == "wants to run dangerous."  # the data is the engine's words
    text = card_markdown(card)
    lines = text.splitlines()
    assert [ln for ln in lines if ln.startswith("⚙️")] == [lines[0]]  # one heading
    assert lines[0].startswith("⚙️ **") and lines[0].endswith("**")
    assert "**support**" not in lines[0] and "[Allow](" not in lines[0]  # escaped, not rendered
    assert "\\*\\*support\\*\\*" in lines[0] or "\\[Allow\\]" in lines[0]
    assert lines[-1] == "_assistant · dangerous_"  # who asks and what, as a signature


def test_the_arguments_on_a_card_are_capped() -> None:
    card = toolgate._card("assistant", "dangerous", {"body": "x" * 5000})
    assert len(card.block) == toolgate.ARGUMENTS_LIMIT and card.block.endswith("…")
    assert toolgate._card("assistant", "dangerous", {"a": 1}).block == '{"a": 1}'


async def _asked(rig: Rig) -> str:
    for _ in range(400):
        if rig.poster.posts:
            return str(rig.poster.posts[0].actions[0].context[APPROVAL_KEY])
        await asyncio.sleep(0.005)
    raise AssertionError("nobody was asked")


async def test_the_person_who_asked_the_agent_is_the_one_who_may_answer(tmp_path: Path) -> None:
    """In a shared channel the stranger who talked the model into a call must
    not be the one who approves it."""
    rig = await _rig(tmp_path, 8561)
    await rig.store.touch("assistant", "dm1", "uid-asker")
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        token = await _asked(rig)
        assert rig.approvals.resolve(token, ANSWER_ONCE, "uid-stranger").name == "NOT_ALLOWED"
        assert rig.approvals.pending(token)  # the card is still live for the asker
        assert rig.approvals.resolve(token, ANSWER_ONCE, "uid-asker").name == "RESOLVED"
        assert await pending == 200
    finally:
        await _close(rig)


async def test_named_approvers_replace_the_asker(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8562, approvers=("uid-lead", "uid-ops"))
    await rig.store.touch("assistant", "dm1", "uid-asker")
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        token = await _asked(rig)
        assert rig.approvals.resolve(token, ANSWER_ONCE, "uid-asker").name == "NOT_ALLOWED"
        assert rig.approvals.resolve(token, ANSWER_ONCE, "uid-ops").name == "RESOLVED"
        assert await pending == 200
    finally:
        await _close(rig)



async def test_a_gate_that_never_asked_says_why_and_the_model_does_not_retry(tmp_path: Path) -> None:
    """An application's own gate may refuse to put the question at all — a
    budget of questions spent, a policy — and the model hears its reason, apart
    from the three refusals that are about a person."""
    from crucible.approvals import NotAsked

    class Budget:
        async def confirm(self, agent, tool, args, *, runtime_session_id, preview=None, display_name=""):
            return NotAsked("third write this turn without a question in between")

    rig = await _rig(tmp_path, 8559)
    rig.server._tool_gate = Budget()  # the composition's choice of gate
    try:
        status, told = await _call_told(rig, "dangerous")
        assert status == 403
        assert told == "not asked: third write this turn without a question in between. Tell the user, and do not retry"
        assert rig.tool.ran == 0 and rig.poster.posts == []
    finally:
        await _close(rig)


async def test_a_previews_verbatim_block_reaches_the_card_whole(tmp_path: Path) -> None:
    """A tool that shows a script asks the person to read it as written: the
    preview's block goes to the card as its block, uncut — not as a row, and
    not trimmed the way raw arguments are."""
    rig = await _rig(tmp_path, 8560, timeout_s=90.0)
    script = "\n".join(f"line {i}: issue.update(x)" for i in range(400))
    rig.tool.preview = CallPreview(
        "run a script on IPA-7", (PreviewRow("Issue", "IPA-7"),), danger=True,
        block_label="Script", block=script,
    )
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        posted = await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        card = posted.card
        assert card is not None
        assert card.block == script and card.block_label == "Script"  # whole, past ARGUMENTS_LIMIT
        assert len(script) > toolgate.ARGUMENTS_LIMIT
        assert card.fields == (PreviewRow("Issue", "IPA-7"),)
        assert "Script" in card_markdown(card) and "line 399" in card_markdown(card)
    finally:
        await _close(rig)


async def test_the_card_is_posted_in_every_gateway_and_the_first_answer_closes_them_all(
    tmp_path: Path,
) -> None:
    """A conversation live in two gateways is asked in both; whichever copy is
    answered first settles it — the other click finds no question — and both
    copies then show the verdict."""
    from crucible.approvals.pending import ApprovalOutcome
    from crucible.ports.chat.types import KIND_THREAD
    from crucible.store.base import GatewayAddress

    slack = FakePoster()
    rig = await _rig(tmp_path, 8562, by_gateway={"slack": slack})
    try:
        await rig.store.set_gateway_address(
            "assistant", "dm1", "mattermost", GatewayAddress("dm1", "dm1", KIND_DM)
        )
        await rig.store.set_gateway_address(
            "assistant", "dm1", "slack", GatewayAddress("C9", "1700.1", KIND_THREAD)
        )
        await rig.store.touch("assistant", "dm1", gateway="mattermost")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, ANSWER_ONCE)  # answered on the Mattermost copy
        assert await pending == 200
        assert len(slack.posts) == 1 and len(rig.poster.posts) == 1
        ref = slack.posts[0].ref
        assert ref is not None and (ref.channel_id, ref.thread_root_id) == ("C9", "1700.1")
        token = str(slack.posts[0].actions[0].context[APPROVAL_KEY])
        assert token == str(card.actions[0].context[APPROVAL_KEY])  # one question, two copies
        assert rig.approvals.resolve(token, ANSWER_DENY, CLICKER) is ApprovalOutcome.NOT_MINE  # 2nd click
        assert [u.card.headline for u in rig.poster.updated] == [u.card.headline for u in slack.updated]
        assert "Allowed once" in slack.updated[0].card.headline
    finally:
        await _close(rig)


async def test_the_asker_is_named_as_a_person_so_a_click_from_another_gateway_counts(tmp_path: Path) -> None:
    """The person who asked from the panel is the approver; their click on the
    Slack copy of the card arrives under a Slack id the application maps to
    the same person."""
    from crucible.approvals import ApprovalOutcome

    class _People:
        def key(self, gateway: str, user_id: str) -> str:
            return {("mattermost", CLICKER): "roman", ("slack", "U0123"): "roman"}.get((gateway, user_id), user_id)

    rig = await _rig(tmp_path, 8564, people=_People())
    try:
        await rig.store.touch("assistant", "dm1", CLICKER, gateway="mattermost")  # the asker, on Mattermost
        pending = asyncio.create_task(_call(rig, "dangerous"))
        for _ in range(400):
            if rig.poster.posts:
                break
            await asyncio.sleep(0.005)
        token = str(rig.poster.posts[0].actions[0].context[APPROVAL_KEY])
        assert rig.approvals.resolve(token, ANSWER_ONCE, "U0123") is ApprovalOutcome.NOT_ALLOWED  # a raw id
        assert rig.approvals.resolve(token, ANSWER_ONCE, "roman") is ApprovalOutcome.RESOLVED  # the person
        assert await pending == 200
    finally:
        await _close(rig)


async def test_a_cards_token_names_the_replica_that_asked(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8563, replica_id="a1")
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        card = await _answer(rig, ANSWER_ONCE)
        assert await pending == 200
        assert str(card.actions[0].context[APPROVAL_KEY]).startswith("a1.")
    finally:
        await _close(rig)


async def test_the_card_is_posted_where_the_conversation_last_spoke(tmp_path: Path) -> None:
    """An agent reached through two gateways asks on the one the turn came
    through, at the conversation's address there — not on its default."""
    from crucible.ports.chat.types import KIND_THREAD
    from crucible.store.base import GatewayAddress

    slack = FakePoster()
    rig = await _rig(tmp_path, 8561, by_gateway={"slack": slack})
    try:
        await rig.store.set_gateway_address("assistant", "dm1", "slack", GatewayAddress("C9", "1700.1", KIND_THREAD))
        await rig.store.touch("assistant", "dm1", gateway="slack")
        pending = asyncio.create_task(_call(rig, "dangerous"))
        for _ in range(100):
            if slack.posts:
                break
            await asyncio.sleep(0.02)
        assert slack.posts and not rig.poster.posts  # asked on Slack, not on the default gateway
        ref = slack.posts[0].ref
        assert ref is not None and (ref.channel_id, ref.thread_root_id) == ("C9", "1700.1")  # its Slack address
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pending
    finally:
        await _close(rig)
