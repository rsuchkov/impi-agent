"""The confirmation a tool declares, enforced by the server (interactions/toolgate.py).

The gate that matters is this one. The runtime's extension asks before it makes
the call, but the token it authenticates with lives in the agent's own
environment — so a shell in that container can POST the tool server directly and
never see the question. These tests are mostly about that call being refused.

The other half is the window: a human can say "yes, for a while", and the second
call inside it does not ask again.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from crucible.approvals import ANSWER_DENY, ANSWER_ONCE, APPROVAL_KEY, PendingApprovals
from crucible.interactions import toolgate
from crucible.interactions.toolgate import ToolGate
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.types import ACTION_SELECT, KIND_DM
from crucible.store.base import KIND_TOOL
from crucible.store.sessions import SqliteSessionStore
from crucible.tools.base import Tool, ToolContext
from crucible.tools.registry import ToolRegistry
from crucible.tools.server import ToolServer

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


class FakePoster:
    def __init__(self) -> None:
        self.posts: list[Posted] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_actions(self, ref, text, actions, *, callback_url) -> str:
        self.posts.append(Posted(text, list(actions), f"post-{len(self.posts) + 1}"))
        return self.posts[-1].post_id

    async def retract(self, post_id: str, text: str) -> None:
        self.retracted.append((post_id, text))


@dataclass
class FakePresence:
    chat: FakePoster

    def poster(self, agent: str):
        return self.chat

    def sink(self, agent: str):
        return None


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


async def _rig(tmp_path: Path, port: int, *, gated: bool = True, **over) -> Rig:
    store = SqliteSessionStore(tmp_path / "db.sqlite")
    record, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM)
    poster = FakePoster()
    approvals = PendingApprovals()
    gate = ToolGate(
        FakePresence(poster), store, store, approvals,  # type: ignore[arg-type]
        callback_url="http://engine/interact",
        timeout_s=over.pop("timeout_s", 5.0),
        max_grant_s=over.pop("max_grant_s", 900),
    )
    dangerous, plain = Dangerous(), Harmless()
    registry = ToolRegistry((dangerous, plain))  # type: ignore[arg-type]
    server = ToolServer(
        registry,
        directory=FakeDirectory(),  # type: ignore[arg-type]
        admins={},
        tokens={"secret-tok": "assistant"},
        allowlists={"assistant": frozenset({"dangerous", "harmless"})},
        port=port,
        tool_gate=gate if gated else None,  # type: ignore[arg-type]
    )
    await server.start()
    return Rig(server, store, poster, approvals, dangerous, plain, record.runtime_session_id, port)


async def _call(rig: Rig, name: str) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"http://127.0.0.1:{rig.port}/tool/{name}",
            json={},
            headers={**TOKEN, "X-Runtime-Session": rig.session},
        ) as response:
            return response.status


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
    finally:
        await _close(rig)


async def test_a_refusal_means_the_tool_does_not_run(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8542)
    try:
        pending = asyncio.create_task(_call(rig, "dangerous"))
        await _answer(rig, ANSWER_DENY)
        assert await pending == 403
        assert rig.tool.ran == 0
    finally:
        await _close(rig)


async def test_nobody_answering_is_a_refusal(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8543, timeout_s=0.05)
    try:
        assert await _call(rig, "dangerous") == 403
        assert rig.tool.ran == 0
        assert any("in time" in text for _, text in rig.poster.retracted)
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
        assert grant.kind == KIND_TOOL and grant.scope == "dangerous"
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
                id="gr_1", kind="secret", principal="assistant", scope="dangerous",
                granted_by=CLICKER, granted_at="2026-01-01T00:00:00+00:00",
                expires_at="2099-01-01T00:00:00+00:00",
            )
        )
        assert await _call(rig, "dangerous") == 403  # asked, and nobody answered
        assert rig.tool.ran == 0
    finally:
        await _close(rig)


# -- who may answer ------------------------------------------------------------


async def test_anyone_in_the_conversation_may_answer(tmp_path: Path) -> None:
    """Unlike a credential, which is addressed to a named approver: a tool call
    is addressed to the people watching the agent work."""
    rig = await _rig(tmp_path, 8549)
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
    card = toolgate._card("assistant", "dangerous", {"payload": hostile})
    # The label appears once as a LINE; inside the block it is literal text,
    # which is containment rather than censorship.
    lines = card.splitlines()
    assert [ln for ln in lines if ln.startswith("**")] == ["**Arguments:**"]
    assert len([ln for ln in lines if set(ln) == {"`"}]) == 2  # exactly one block
