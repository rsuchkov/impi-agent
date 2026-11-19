"""McpHub over real HTTP on a local port (offline, aiohttp client)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
import pytest

from crucible.gateways.factory import GatewayConfig, GatewayFactory
from crucible.gateways.mcp import (
    MCP_PATH,
    META_CONVERSATION,
    SCOPE_ACT,
    SCOPE_READ,
    AskAgentTool,
    CallFailed,
    GatewayCall,
    McpChatClient,
    McpGateway,
    McpHub,
    Principal,
    conversation_key,
)
from crucible.gateways.mcp.hub import RESOURCE_METADATA_PATH
from crucible.loopguard import LoopGuard
from crucible.mcp.protocol import HttpTransport, McpClient
from crucible.ports.agent.events import TextProgress, ToolFinished, ToolStarted
from crucible.ports.chat.client import NobodyToAnswer
from crucible.ports.chat.flow import TurnOutcome
from crucible.ports.chat.gateway import (
    GATEWAY_KINDS,
    MCP_CLIENT_ATTRIBUTE,
    MCP_SCOPES_ATTRIBUTE,
)
from crucible.ports.chat.types import (
    KIND_DM,
    Action,
    ApprovalCard,
    IncomingMessage,
    OutgoingFile,
)
from crucible.store.base import SessionRecord
from crucible.tools.base import CAP_DELEGATION, CAP_FORMS, CAP_WIDGETS

Script = Callable[[IncomingMessage, McpChatClient], Awaitable[TurnOutcome]]
VERSION = "2026-07-28"
TOKENS = {
    "tok-reader": Principal("alice@example.com", "ide-client", frozenset({SCOPE_READ}), username="alice"),
    "tok-actor": Principal("alice@example.com", "ide-client", frozenset({SCOPE_READ, SCOPE_ACT}), username="alice"),
    "tok-bob": Principal("bob@example.com", "ide-client", frozenset({SCOPE_READ})),
}


async def _reply(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
    await chat.post_reply(msg.ref, f"you said: {msg.text}")
    return TurnOutcome.REPLIED


class FakeSink:
    def __init__(self, script: Script = _reply) -> None:
        self.script = script
        self.submitted: list[IncomingMessage] = []

    def submit(self, msg, chat) -> None:
        self.submit_tracked(msg, chat)

    def submit_tracked(self, msg, chat) -> asyncio.Future:
        self.submitted.append(msg)
        return asyncio.ensure_future(self.script(msg, chat))


class FakeAuthorization:
    async def authenticate(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return TOKENS.get(token)


class FakeCards:
    """The application's: a card goes "to Slack", by the person's key."""

    def __init__(self) -> None:
        self.posted: list[tuple[Principal, ApprovalCard, list[Action]]] = []
        self.updated: list[tuple[str, ApprovalCard, list[Action] | None]] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_confirmation(self, asker, card, actions, *, callback_url):
        self.posted.append((asker, card, actions))
        return f"slack-{len(self.posted)}"

    async def update_confirmation(self, post_id, card, actions=None, *, callback_url=""):
        self.updated.append((post_id, card, actions))

    async def retract(self, post_id, text):
        self.retracted.append((post_id, text))


class ActTool:
    name = "deploy"
    description = "Deploy something."
    input_schema = {"type": "object", "properties": {}}
    scope = SCOPE_ACT
    requires_task = False

    async def run(self, call: GatewayCall) -> str:
        return "deployed"


class BrokenTool:
    name = "broken"
    description = "Fails."
    input_schema = {"type": "object", "properties": {}}
    scope = SCOPE_READ
    requires_task = False

    async def run(self, call: GatewayCall) -> str:
        raise RuntimeError("boom")


class ProgressTool:
    name = "slow"
    description = "Says what it does."
    input_schema = {"type": "object", "properties": {}}
    scope = SCOPE_READ
    requires_task = False

    async def run(self, call: GatewayCall) -> str:
        await call.progress("step one")
        await call.progress("step two")
        return "done"


def _hub(port: int, *, script: Script = _reply, agents=("helper",), **kw) -> tuple[McpHub, dict[str, FakeSink]]:
    hub = McpHub("127.0.0.1", port, FakeAuthorization(), public_url=f"http://127.0.0.1:{port}", **kw)
    sinks: dict[str, FakeSink] = {}
    for agent in agents:
        sinks[agent] = FakeSink(script)
        hub.register_agent(agent, sinks[agent], McpChatClient(hub.turns, agent, cards=hub.cards, asker_of=hub.asker_of))
    return hub, sinks


def _url(port: int) -> str:
    return f"http://127.0.0.1:{port}{MCP_PATH}"


def _headers(token: str = "tok-reader", method: str = "", version: str = VERSION, **extra) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}
    if version:
        headers["MCP-Protocol-Version"] = version
    if method:
        headers["Mcp-Method"] = method
    headers.update(extra)
    return headers


async def _rpc(
    s: aiohttp.ClientSession, port: int, method: str, params=None, *, rid=1, headers=None,
) -> tuple[int, dict[str, Any], Any]:
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    async with s.post(_url(port), json=body, headers=headers or _headers(method=method)) as response:
        text = await response.text()
        parsed = json.loads(text) if response.content_type == "application/json" and text else {}
        return response.status, parsed, response.headers


async def _call(s, port, name, arguments=None, *, handle="", token="tok-reader"):
    params: dict = {"name": name, "arguments": arguments or {}}
    if handle:
        params["_meta"] = {META_CONVERSATION: handle}
    return await _rpc(s, port, "tools/call", params, headers=_headers(token, method="tools/call"))


@pytest.mark.asyncio
async def test_discover_list_and_a_call_run_a_turn_as_the_person() -> None:
    hub, sinks = _hub(8601, instructions="Ask helper anything.")
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, answer, _ = await _rpc(s, 8601, "server/discover")
            assert status == 200 and answer["result"]["supportedVersions"] == [VERSION]
            assert answer["result"]["instructions"] == "Ask helper anything."
            status, answer, _ = await _rpc(s, 8601, "tools/list")
            assert [t["name"] for t in answer["result"]["tools"]] == ["ask_agent"]
            assert answer["result"]["tools"][0]["inputSchema"]["required"] == ["text"]

            status, answer, _ = await _call(s, 8601, "ask_agent", {"text": "hello"})
            assert status == 200
            result = answer["result"]
            assert result["content"] == [{"type": "text", "text": "you said: hello"}] and result["isError"] is False
            handle = result["_meta"][META_CONVERSATION]
            assert handle
            msg = sinks["helper"].submitted[0]
            assert msg.gateway == "mcp" and msg.user_id == "alice@example.com" and msg.username == "alice"
            assert msg.kind == KIND_DM and msg.is_dm and msg.mentioned
            assert msg.conversation_id == conversation_key("alice@example.com", handle)
            assert msg.turn is not None and msg.turn.attributes == {MCP_CLIENT_ATTRIBUTE: "ide-client", MCP_SCOPES_ATTRIBUTE: "read"}
            assert msg.turn.turn_id

            # The handle continues the conversation; another person's same
            # handle is another conversation.
            _, answer, _ = await _call(s, 8601, "ask_agent", {"text": "again"}, handle=handle)
            assert answer["result"]["_meta"][META_CONVERSATION] == handle
            assert sinks["helper"].submitted[1].conversation_id == msg.conversation_id
            _, answer, _ = await _call(s, 8601, "ask_agent", {"text": "mine"}, handle=handle, token="tok-bob")
            assert sinks["helper"].submitted[2].conversation_id == conversation_key("bob@example.com", handle)
            assert sinks["helper"].submitted[2].username == "bob@example.com"
            # The turn ended: the registry has no running turn, the journal is kept.
            assert hub.turns.active(msg.conversation_id) is None
            assert hub.asker_of(msg.conversation_id) is None
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_the_engines_own_client_speaks_to_the_gateway() -> None:
    hub, _ = _hub(8602)
    await hub.start()
    try:
        client = McpClient(HttpTransport(_url(8602), {"Authorization": "Bearer tok-reader"}))
        try:
            tools = await client.list_tools()
            assert [t.name for t in tools] == ["ask_agent"] and not client.legacy
            result = await client.call_tool("ask_agent", {"text": "hi"})
            assert result.text == "you said: hi" and not result.is_error
        finally:
            await client.close()
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_who_is_let_in_and_what_the_envelope_must_say() -> None:
    hub, _ = _hub(8603, allowed_origins=("https://app.example.com",))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            # No token: 401 pointing at the resource metadata.
            status, _, headers = await _rpc(s, 8603, "ping", headers=_headers("nobody"))
            assert status == 401
            assert headers["WWW-Authenticate"] == f'Bearer resource_metadata="http://127.0.0.1:8603{RESOURCE_METADATA_PATH}"'
            # A browser from elsewhere.
            status, _, _ = await _rpc(s, 8603, "ping", headers=_headers(Origin="https://evil.example.com"))
            assert status == 403
            status, answer, _ = await _rpc(s, 8603, "ping", headers=_headers(Origin="https://app.example.com"))
            assert status == 200 and answer["result"] == {}
            # A legacy client: no version, or a handshake.
            status, answer, _ = await _rpc(s, 8603, "initialize", headers=_headers(version=""))
            assert status == 400 and answer["error"]["code"] == -32022 and answer["error"]["data"] == {"supported": [VERSION]}
            status, answer, _ = await _rpc(s, 8603, "ping", headers=_headers(version="2025-11-25"))
            assert status == 400 and answer["error"]["code"] == -32022
            # Headers that contradict the body.
            status, answer, _ = await _rpc(s, 8603, "ping", headers=_headers(method="tools/list"))
            assert status == 400 and answer["error"]["code"] == -32020
            status, answer, _ = await _rpc(
                s, 8603, "tools/call", {"name": "ask_agent", "arguments": {"text": "x"}},
                headers=_headers(method="tools/call", **{"Mcp-Name": "other"}),
            )
            assert status == 400 and answer["error"]["code"] == -32020
            status, answer, _ = await _rpc(
                s, 8603, "ping", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}}
            )
            assert status == 400 and answer["error"]["code"] == -32020
            # Not one request.
            async with s.post(_url(8603), json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=_headers()) as r:
                assert r.status == 400 and (await r.json())["error"]["code"] == -32600
            async with s.post(_url(8603), data="not json", headers=_headers()) as r:
                assert r.status == 400 and (await r.json())["error"]["code"] == -32700
            # A notification is taken and answered nothing; an unknown method is said so.
            body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            async with s.post(_url(8603), json=body, headers=_headers(method="notifications/initialized")) as r:
                assert r.status == 202
            status, answer, _ = await _rpc(s, 8603, "resources/list")
            assert status == 200 and answer["error"]["code"] == -32601 and answer["id"] == 1
            async with s.get(_url(8603), headers=_headers()) as r:
                assert r.status == 405
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_tool_is_there_for_the_scope_that_may_call_it() -> None:
    hub, _ = _hub(8604, tools=(AskAgentTool(), ActTool()))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _rpc(s, 8604, "tools/list")
            assert [t["name"] for t in answer["result"]["tools"]] == ["ask_agent"]
            _, answer, _ = await _rpc(s, 8604, "tools/list", headers=_headers("tok-actor", method="tools/list"))
            assert [t["name"] for t in answer["result"]["tools"]] == ["ask_agent", "deploy"]
            status, answer, headers = await _call(s, 8604, "deploy")
            assert status == 403 and answer["error"]["code"] == -32602
            assert headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="act"'
            status, answer, _ = await _call(s, 8604, "deploy", token="tok-actor")
            assert status == 200 and answer["result"]["content"][0]["text"] == "deployed"
            assert "_meta" not in answer["result"]  # no conversation was used
            status, answer, _ = await _call(s, 8604, "nope")
            assert status == 200 and answer["error"]["code"] == -32602
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_what_goes_wrong_in_a_call_is_an_error_result_not_a_protocol_error() -> None:
    async def fails(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await chat.post_notice(msg.ref, "The agent could not answer this time.", code="internal_error")
        return TurnOutcome.ERROR

    hub, _ = _hub(8605, script=fails, tools=(AskAgentTool(), BrokenTool()))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, answer, _ = await _call(s, 8605, "ask_agent", {"text": "hi"})
            assert status == 200
            assert answer["result"]["isError"] is True
            assert answer["result"]["content"][0]["text"] == "The agent could not answer this time."
            assert answer["result"]["_meta"][META_CONVERSATION]  # the conversation exists all the same
            status, answer, _ = await _call(s, 8605, "ask_agent", {"text": "   "})
            assert answer["result"]["isError"] is True and "text" in answer["result"]["content"][0]["text"]
            status, answer, _ = await _call(s, 8605, "broken")
            assert answer["result"]["isError"] is True and "broken failed" in answer["result"]["content"][0]["text"]
            status, answer, _ = await _call(s, 8605, "ask_agent", {"text": "x"}, handle="not a handle!")
            assert status == 200 and answer["error"]["code"] == -32602
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_busy_conversation_refuses_a_second_call_until_the_first_is_done() -> None:
    gate = asyncio.Event()

    async def slow(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await gate.wait()
        await chat.post_reply(msg.ref, "done")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8606, script=slow)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            first = asyncio.ensure_future(_call(s, 8606, "ask_agent", {"text": "one"}, handle="h1"))
            await asyncio.sleep(0.05)
            _, answer, _ = await _call(s, 8606, "ask_agent", {"text": "two"}, handle="h1")
            assert answer["result"]["isError"] is True and "earlier call" in answer["result"]["content"][0]["text"]
            gate.set()
            _, answer, _ = await first
            assert answer["result"]["content"][0]["text"] == "done"
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_several_agents_are_named_and_one_is_the_default() -> None:
    hub, sinks = _hub(8607, agents=("helper", "scribe"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi"})
            assert answer["result"]["isError"] is True and "name the agent" in answer["result"]["content"][0]["text"]
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi", "agent": "scribe"})
            assert answer["result"]["isError"] is False and sinks["scribe"].submitted
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi", "agent": "nobody"})
            assert "not on this gateway" in answer["result"]["content"][0]["text"]
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_client_that_asks_for_progress_gets_the_turns_steps_as_a_stream() -> None:
    async def tools(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm", "rs-1", "t", "t")
        trace = hub.turns.begin(record, chat)
        trace.on_event(TextProgress("Looking it up"))
        trace.on_event(ToolStarted("c1", "search", {"q": "x"}, caption="Searching for x"))
        await asyncio.sleep(0.02)
        trace.on_event(ToolFinished("c1", "search", is_error=False))
        await trace.end()
        await chat.post_reply(msg.ref, "found it")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8608, script=tools, tools=(AskAgentTool(), ProgressTool()), presenter=str.upper)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            body = {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "ask_agent", "arguments": {"text": "x"}, "_meta": {"progressToken": "p1"}},
            }
            headers = _headers(method="tools/call", Accept="application/json, text/event-stream")
            async with s.post(_url(8608), json=body, headers=headers) as r:
                assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
                text = await r.text()
            messages = [json.loads(chunk.split("data: ", 1)[1]) for chunk in text.strip().split("\n\n")]
            progress = [m["params"] for m in messages if m.get("method") == "notifications/progress"]
            assert [p["message"] for p in progress] == ["Looking it up", "Searching for x"]
            assert [p["progress"] for p in progress] == [1, 2] and all(p["progressToken"] == "p1" for p in progress)
            final = messages[-1]
            assert final["id"] == 7 and final["result"]["content"][0]["text"] == "FOUND IT"  # the presenter's word

            # A tool's own progress travels the same way; without a token the
            # answer is plain JSON and the words go nowhere.
            body["params"] = {"name": "slow", "arguments": {}, "_meta": {"progressToken": 2}}
            async with s.post(_url(8608), json=body, headers=headers) as r:
                text = await r.text()
            messages = [json.loads(chunk.split("data: ", 1)[1]) for chunk in text.strip().split("\n\n")]
            assert [m["params"]["message"] for m in messages[:-1]] == ["step one", "step two"]
            assert messages[-1]["result"]["content"][0]["text"] == "done"  # not the agent's answer: as the tool said it
            body["params"] = {"name": "slow", "arguments": {}}
            async with s.post(_url(8608), json=body, headers=headers) as r:
                assert r.headers["Content-Type"].startswith("application/json")
                assert (await r.json())["result"]["content"][0]["text"] == "done"
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_card_goes_to_the_person_through_the_application_not_to_the_client() -> None:
    cards = FakeCards()
    card = ApprovalCard(agent="helper", headline="wants to run deploy.", tool="deploy")
    actions = [Action(id="approve", label="Allow", value="yes"), Action(id="deny", label="Deny", value="no")]
    seen: dict = {}

    async def asks(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        post_id = await chat.post_confirmation(msg.ref, card, actions, callback_url="")
        turn = hub.turns.active(msg.conversation_id)
        assert turn is not None
        seen["status"] = turn.journal.status
        await chat.update_confirmation(post_id, card, [], callback_url="")
        await chat.retract(post_id, "withdrawn")
        await chat.post_reply(msg.ref, "deployed")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8609, script=asks, cards=cards)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8609, "ask_agent", {"text": "deploy"}, token="tok-actor")
            assert answer["result"]["content"][0]["text"] == "deployed"
            asker, posted_card, posted_actions = cards.posted[0]
            assert asker == TOKENS["tok-actor"] and posted_card is card and posted_actions == actions
            assert seen["status"] == "awaiting_input"  # the journal says the turn waits
            assert cards.updated == [("slack-1", card, [])]
            assert cards.retracted == [("slack-1", "withdrawn")]
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_without_a_way_to_reach_the_person_a_card_has_nobody_to_answer() -> None:
    outcome: dict = {}

    async def asks(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        try:
            await chat.post_confirmation(msg.ref, ApprovalCard(agent="helper", headline="x"), [], callback_url="")
        except NobodyToAnswer as exc:
            outcome["card"] = str(exc)
        try:
            await chat.post_actions(msg.ref, "pick", [], callback_url="")
        except NobodyToAnswer as exc:
            outcome["widget"] = str(exc)
        await chat.post_reply(msg.ref, "ok")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8610, script=asks)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8610, "ask_agent", {"text": "x"})
            assert answer["result"]["content"][0]["text"] == "ok"
            assert "cannot answer" in outcome["card"] and "cannot click" in outcome["widget"]
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_files_and_notices_read_back_as_the_answer() -> None:
    async def sends(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await chat.post_files(msg.ref, [OutgoingFile("report.csv", b"a,b", "text/csv")], text="Here it is")
        return TurnOutcome.ACTED

    hub, _ = _hub(8611, script=sends)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8611, "ask_agent", {"text": "report"})
            text = answer["result"]["content"][0]["text"]
            assert text == "Here it is\n\nFiles the agent produced (kept in its conversation): report.csv"
    finally:
        await hub.stop()


def test_the_factory_builds_an_mcp_handle_only_over_a_hub() -> None:
    assert "mcp" in GATEWAY_KINDS
    hub = McpHub("127.0.0.1", 0, FakeAuthorization())
    with_hub = GatewayFactory(directory=None, loop_guard=LoopGuard(), dispatcher=None, mcp_hub=hub)  # type: ignore[arg-type]
    handle = with_hub.create("helper", GatewayConfig(kind="mcp"))
    assert handle is not None and isinstance(handle.chat, McpChatClient)
    assert handle.denied_caps == {CAP_WIDGETS, CAP_FORMS, CAP_DELEGATION}
    gateway = handle.create_gateway(FakeSink())
    assert isinstance(gateway, McpGateway) and hub.serves("helper")
    with pytest.raises(TypeError):
        handle.create_gateway(object())  # type: ignore[arg-type]
    without = GatewayFactory(directory=None, loop_guard=LoopGuard(), dispatcher=None)  # type: ignore[arg-type]
    assert without.create("helper", GatewayConfig(kind="mcp")) is None


@pytest.mark.asyncio
async def test_a_gateway_call_is_what_a_tool_sees() -> None:
    said: list[str] = []

    async def asker(call, text, agent):
        return f"{agent or 'default'}: {text}"

    async def progress(text):
        said.append(text)

    call = GatewayCall(TOKENS["tok-reader"], {"text": "hi"}, "", asker=asker, progress=progress)
    assert await AskAgentTool().run(call) == "default: hi"
    await call.progress("working")
    assert said == ["working"] and call.handle == "" and not call.wants_progress
    with pytest.raises(CallFailed):
        await AskAgentTool().run(GatewayCall(TOKENS["tok-reader"], {}, "", asker=asker, progress=progress))
