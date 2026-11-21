"""McpHub over real HTTP on a local port (offline, aiohttp client)."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import aiohttp
import pytest

from crucible.approvals.card import card_markdown
from crucible.gateways.factory import GatewayConfig, GatewayFactory
from crucible.gateways.mcp import (
    MCP_PATH,
    META_CONVERSATION,
    TASKS_EXTENSION,
    AskAgentTool,
    CallFailed,
    CardPlace,
    GatewayCall,
    McpCaller,
    McpChatClient,
    McpGateway,
    McpHub,
    TurnStanding,
    WaitAgentTool,
    conversation_key,
)
from crucible.gateways.mcp.hub import RESOURCE_METADATA_PATH
from crucible.loopguard import LoopGuard
from crucible.mcp.protocol import HttpTransport, McpClient
from crucible.ports.agent.events import (
    CallAwaiting,
    TextProgress,
    ToolFinished,
    ToolStarted,
)
from crucible.ports.chat.client import NobodyToAnswer
from crucible.ports.chat.flow import TurnOutcome, TurnStop
from crucible.ports.chat.gateway import (
    GATEWAY_KINDS,
    MCP_CLIENT_ATTRIBUTE,
    MCP_CLIENT_NAME_ATTRIBUTE,
    MCP_SCOPE_ACT,
    MCP_SCOPE_READ,
    MCP_SCOPES_ATTRIBUTE,
)
from crucible.ports.chat.types import (
    KIND_DM,
    Action,
    ApprovalCard,
    IncomingMessage,
    OutgoingFile,
)
from crucible.ports.mcp_turns import TURN_DONE
from crucible.ports.replica import Replica
from crucible.store.base import SessionRecord
from crucible.tools.base import CAP_DELEGATION, CAP_FORMS, CAP_WIDGETS
from tests.fakes.mcp_turns import MemoryMcpTurns
from tests.test_connections import Clock

Script = Callable[[IncomingMessage, McpChatClient], Awaitable[TurnOutcome]]
VERSION = "2026-07-28"
TOKENS = {
    "tok-reader": McpCaller("alice@example.com", "ide-client", frozenset({MCP_SCOPE_READ}), username="alice"),
    "tok-actor": McpCaller(
        "alice@example.com", "ide-client", frozenset({MCP_SCOPE_READ, MCP_SCOPE_ACT}), username="alice", client_name="The IDE",
    ),
    "tok-bob": McpCaller("bob@example.com", "ide-client", frozenset({MCP_SCOPE_READ})),
}
WITH_TASKS = {"io.modelcontextprotocol/clientCapabilities": {"extensions": {TASKS_EXTENSION: {}}}}


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


class StoppableFakeSink(FakeSink):
    def __init__(self, script: Script) -> None:
        super().__init__(script)
        self.stopped: list[tuple[str, str, str]] = []
        self.gate = asyncio.Event()

    async def stop(self, conversation_id: str, *, gateway: str = "", user_id: str = "") -> TurnStop:
        self.stopped.append((conversation_id, gateway, user_id))
        self.gate.set()  # the turn ends with what it had
        return TurnStop.STOPPED


class FakeAuthorization:
    async def authenticate(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return TOKENS.get(token)


class FakeCards:
    """The application's: a card goes "to Slack", by the person's key."""

    def __init__(self) -> None:
        self.posted: list[tuple[McpCaller, ApprovalCard, list[Action]]] = []
        self.places: list[CardPlace] = []
        self.updated: list[tuple[str, ApprovalCard, list[Action] | None]] = []
        self.retracted: list[tuple[str, str]] = []

    async def post_confirmation(self, asker, card, actions, *, place, callback_url):
        self.posted.append((asker, card, actions))
        self.places.append(place)
        return f"slack-{len(self.posted)}"

    async def update_confirmation(self, post_id, card, actions=None, *, callback_url=""):
        self.updated.append((post_id, card, actions))

    async def retract(self, post_id, text):
        self.retracted.append((post_id, text))


class ActTool:
    name = "deploy"
    description = "Deploy something."
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    scope = MCP_SCOPE_ACT
    long_running = False
    conversation_argument = ""
    title = ""
    annotations = None

    async def run(self, call: GatewayCall) -> str:
        return "deployed"


class BrokenTool:
    name = "broken"
    description = "Fails."
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    scope = MCP_SCOPE_READ
    long_running = False
    conversation_argument = ""
    title = ""
    annotations = None

    async def run(self, call: GatewayCall) -> str:
        raise RuntimeError("boom")


class ProgressTool:
    name = "slow"
    description = "Says what it does."
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    scope = MCP_SCOPE_READ
    long_running = False
    conversation_argument = ""
    title = ""
    annotations = None

    async def run(self, call: GatewayCall) -> str:
        await call.progress("step one")
        await call.progress("step two")
        return "done"


class LongTool:
    """A long call of the application's own: no agent, a wait, its own progress."""

    name = "long_job"
    description = "Takes a while."
    input_schema: Mapping[str, Any] = {"type": "object", "properties": {}}
    scope = MCP_SCOPE_READ
    long_running = True
    conversation_argument = ""
    title = ""
    annotations = None

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.fail = False

    async def run(self, call: GatewayCall) -> str:
        await call.progress("counting")
        await self.gate.wait()
        if self.fail:
            raise RuntimeError("boom")
        return "counted"


def _hub(port: int, *, script: Script = _reply, agents=("helper",), **kw) -> tuple[McpHub, dict[str, FakeSink]]:
    kw.setdefault("store", MemoryMcpTurns())
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


async def _call(s, port, name, arguments=None, *, handle="", token="tok-reader", tasks=False, meta_handle=False):
    params: dict = {"name": name, "arguments": dict(arguments or {})}
    meta: dict = dict(WITH_TASKS) if tasks else {}
    if handle and meta_handle:
        meta[META_CONVERSATION] = handle
    elif handle:
        params["arguments"]["conversation"] = handle
    if meta:
        params["_meta"] = meta
    return await _rpc(s, port, "tools/call", params, headers=_headers(token, method="tools/call"))


async def _task_rpc(s, port, method, task_id, *, token="tok-reader", tasks=True):
    params: dict = {"taskId": task_id}
    if tasks:
        params["_meta"] = dict(WITH_TASKS)
    return await _rpc(s, port, method, params, headers=_headers(token, method=method))


async def _settled(s, port, task_id, *, token="tok-reader", timeout=3.0) -> dict:
    """The task once it has left ``working``: polled, as a client would."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        _, answer, _ = await _task_rpc(s, port, "tasks/get", task_id, token=token)
        if "error" in answer or answer["result"]["status"] != "working" or asyncio.get_running_loop().time() > deadline:
            return answer
        await asyncio.sleep(0.02)


def _text(answer: dict) -> str:
    return answer["result"]["content"][0]["text"]


def _handle(answer: dict) -> str:
    return answer["result"]["_meta"][META_CONVERSATION]


@pytest.mark.asyncio
async def test_discover_list_and_a_call_run_a_turn_as_the_person() -> None:
    hub, sinks = _hub(8601, instructions="Ask helper anything.")
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, answer, _ = await _rpc(s, 8601, "server/discover")
            assert status == 200 and answer["result"]["supportedVersions"] == [VERSION]
            assert answer["result"]["resultType"] == "complete"  # on every result
            assert answer["result"]["capabilities"] == {"tools": {}, "extensions": {TASKS_EXTENSION: {}}}
            assert answer["result"]["instructions"] == "Ask helper anything."
            status, answer, _ = await _rpc(s, 8601, "tools/list")
            tools = {t["name"]: t for t in answer["result"]["tools"]}
            assert set(tools) == {"ask_agent", "wait_agent"} and answer["result"]["resultType"] == "complete"
            assert tools["ask_agent"]["inputSchema"]["required"] == ["text"]
            assert "conversation" in tools["ask_agent"]["inputSchema"]["properties"] and "execution" not in tools["ask_agent"]
            # A client tells a reading tool from one that may act by its annotations.
            assert tools["ask_agent"]["title"] == "Ask the agent" and tools["ask_agent"]["annotations"] == {"readOnlyHint": True}
            assert tools["wait_agent"]["annotations"] == {"readOnlyHint": True}

            status, answer, _ = await _call(s, 8601, "ask_agent", {"text": "hello"})
            assert status == 200
            result = answer["result"]
            handle = _handle(answer)
            assert handle and result["resultType"] == "complete" and result["isError"] is False
            # The conversation is named where the client's model reads, and in
            # _meta for a program — never in structuredContent, which a client
            # would hand its model instead of the answer.
            assert result["content"] == [{"type": "text", "text": f"you said: hello\n\nConversation: {handle}"}]
            assert result["_meta"] == {META_CONVERSATION: handle} and "structuredContent" not in result
            msg = sinks["helper"].submitted[0]
            assert msg.gateway == "mcp" and msg.user_id == "alice@example.com" and msg.username == "alice"
            assert msg.kind == KIND_DM and msg.is_dm and msg.mentioned
            assert msg.conversation_id == conversation_key("alice@example.com", handle)
            assert msg.turn is not None and msg.turn.attributes == {
                MCP_CLIENT_ATTRIBUTE: "ide-client", MCP_CLIENT_NAME_ATTRIBUTE: "", MCP_SCOPES_ATTRIBUTE: "read",
            }
            assert msg.turn.turn_id

            # The handle, as the tool's argument or in _meta, continues the
            # conversation; another person's same handle is another conversation.
            _, answer, _ = await _call(s, 8601, "ask_agent", {"text": "again"}, handle=handle)
            assert _handle(answer) == handle
            assert sinks["helper"].submitted[1].conversation_id == msg.conversation_id
            _, answer, _ = await _call(s, 8601, "ask_agent", {"text": "once more"}, handle=handle, meta_handle=True)
            assert _handle(answer) == handle and sinks["helper"].submitted[2].conversation_id == msg.conversation_id
            _, answer, _ = await _call(s, 8601, "ask_agent", {"text": "mine"}, handle=handle, token="tok-bob")
            assert sinks["helper"].submitted[3].conversation_id == conversation_key("bob@example.com", handle)
            assert sinks["helper"].submitted[3].username == "bob@example.com"
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
            assert [t.name for t in tools] == ["ask_agent", "wait_agent"] and not client.legacy
            result = await client.call_tool("ask_agent", {"text": "hi"})
            assert result.text.startswith("you said: hi\n\nConversation: ") and not result.is_error
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
            status, _, headers = await _rpc(s, 8603, "tools/list", headers=_headers("nobody"))
            assert status == 401
            assert headers["WWW-Authenticate"] == f'Bearer resource_metadata="http://127.0.0.1:8603{RESOURCE_METADATA_PATH}"'
            # A browser from elsewhere.
            status, _, _ = await _rpc(s, 8603, "tools/list", headers=_headers(Origin="https://evil.example.com"))
            assert status == 403
            status, answer, _ = await _rpc(s, 8603, "tools/list", headers=_headers(Origin="https://app.example.com"))
            assert status == 200 and "tools" in answer["result"]
            # A legacy client: no version, or a handshake.
            status, answer, _ = await _rpc(s, 8603, "initialize", headers=_headers(version=""))
            assert status == 400 and answer["error"]["code"] == -32022 and answer["error"]["data"] == {"supported": [VERSION]}
            status, answer, _ = await _rpc(s, 8603, "tools/list", headers=_headers(version="2025-11-25"))
            assert status == 400 and answer["error"]["code"] == -32022
            # Headers that contradict the body.
            status, answer, _ = await _rpc(s, 8603, "tools/list", headers=_headers(method="tools/call"))
            assert status == 400 and answer["error"]["code"] == -32020
            status, answer, _ = await _rpc(
                s, 8603, "tools/call", {"name": "ask_agent", "arguments": {"text": "x"}},
                headers=_headers(method="tools/call", **{"Mcp-Name": "other"}),
            )
            assert status == 400 and answer["error"]["code"] == -32020
            status, answer, _ = await _rpc(
                s, 8603, "tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}}
            )
            assert status == 400 and answer["error"]["code"] == -32020
            # Not one request.
            async with s.post(_url(8603), json=[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], headers=_headers()) as r:
                assert r.status == 400 and (await r.json())["error"]["code"] == -32600
            async with s.post(_url(8603), data="not json", headers=_headers()) as r:
                assert r.status == 400 and (await r.json())["error"]["code"] == -32700
            # A notification is taken and answered nothing; what this revision
            # dropped, and what this server never had, is said so.
            body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            async with s.post(_url(8603), json=body, headers=_headers(method="notifications/initialized")) as r:
                assert r.status == 202
            for gone in ("ping", "resources/list", "tasks/result", "tasks/list", "subscriptions/listen"):
                status, answer, _ = await _rpc(s, 8603, gone)
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
            deploy = answer["result"]["tools"][1]
            assert deploy["annotations"] == {"destructiveHint": True} and "title" not in deploy  # what its scope implies
            status, answer, headers = await _call(s, 8604, "deploy")
            assert status == 403 and answer["error"]["code"] == -32602
            assert headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="act"'
            status, answer, _ = await _call(s, 8604, "deploy", token="tok-actor")
            assert status == 200 and _text(answer) == "deployed" and answer["result"]["resultType"] == "complete"
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
            assert _text(answer).startswith("The agent could not answer this time.\n\nConversation: ")
            assert _handle(answer)  # the conversation exists all the same
            status, answer, _ = await _call(s, 8605, "ask_agent", {"text": "   "})
            assert answer["result"]["isError"] is True and "text" in _text(answer)
            status, answer, _ = await _call(s, 8605, "broken")
            assert answer["result"]["isError"] is True and "broken failed" in _text(answer)
            status, answer, _ = await _call(s, 8605, "ask_agent", {"text": "x"}, handle="not a handle!")
            assert status == 200 and answer["error"]["code"] == -32602
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_call_too_long_for_the_client_answers_with_where_the_turn_stands() -> None:
    """A client without tasks gets the answer within ``call_wait_s`` or the
    turn's state and the way back (``wait_agent``); a second call into the
    busy conversation joins the wait rather than failing; the same request
    again without a handle joins the turn it opened."""
    gate = asyncio.Event()

    async def slow(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm", "rs-1", "t", "t")
        trace = hub.turns.begin(record, chat)
        trace.on_event(ToolStarted("c1", "search", {"q": "x"}, caption="Searching the runbook"))
        await gate.wait()
        trace.on_event(ToolFinished("c1", "search", is_error=False))
        await trace.end()
        await chat.post_reply(msg.ref, "done")
        return TurnOutcome.REPLIED

    hub, sinks = _hub(8606, script=slow, call_wait_s=0.2)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8606, "ask_agent", {"text": "one"})
            handle = _handle(answer)
            assert answer["result"]["isError"] is False
            assert "structuredContent" not in answer["result"]
            assert _text(answer) == (
                f"The agent is still working (now: Searching the runbook). Call wait_agent with conversation {handle} "
                f"to get the answer.\n\nConversation: {handle}"
            )
            # The busy conversation: another request is told where the turn
            # stands at once, no second turn and no result of a call it did not make.
            _, answer, _ = await _call(s, 8606, "ask_agent", {"text": "two"}, handle=handle)
            assert "still working" in _text(answer) and len(sinks["helper"].submitted) == 1
            # The same request again without a handle: the stream broke, the client asks again.
            _, answer, _ = await _call(s, 8606, "ask_agent", {"text": "one"})
            assert _handle(answer) == handle and len(sinks["helper"].submitted) == 1
            # Another request without a handle is another conversation (and waits in turn).
            gate.set()
            _, answer, _ = await _rpc(s, 8606, "tools/call", {"name": "wait_agent", "arguments": {"conversation": handle}})
            assert _text(answer) == f"done\n\nConversation: {handle}"
            # Waiting again on a conversation that is done answers at once with the answer; an unknown one is refused.
            _, answer, _ = await _rpc(s, 8606, "tools/call", {"name": "wait_agent", "arguments": {"conversation": handle}})
            assert _text(answer).startswith("done")
            _, answer, _ = await _rpc(s, 8606, "tools/call", {"name": "wait_agent", "arguments": {"conversation": "nobody"}})
            assert answer["result"]["isError"] is True and "no conversation" in _text(answer)
            _, answer, _ = await _rpc(s, 8606, "tools/call", {"name": "wait_agent", "arguments": {}})
            assert answer["result"]["isError"] is True
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_several_agents_are_named_and_one_is_the_default() -> None:
    hub, sinks = _hub(8607, agents=("helper", "scribe"))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi"})
            assert answer["result"]["isError"] is True and "name the agent" in _text(answer)
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi", "agent": "scribe"})
            assert answer["result"]["isError"] is False and sinks["scribe"].submitted
            _, answer, _ = await _call(s, 8607, "ask_agent", {"text": "hi", "agent": "nobody"})
            assert "not on this gateway" in _text(answer)
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
            assert final["id"] == 7 and final["result"]["content"][0]["text"].startswith("FOUND IT\n\nConversation: ")  # the presenter's word

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
            assert _text(answer).startswith("deployed\n\n")
            asker, posted_card, posted_actions = cards.posted[0]
            assert asker == TOKENS["tok-actor"] and posted_actions == actions
            # The person reads in their chat that a program asked, not they — a
            # field every renderer signs the card with, the headline as it was.
            assert posted_card.asked_through == "The IDE" and posted_card.headline == "wants to run deploy."
            assert "asked through The IDE" in card_markdown(posted_card)
            # And where the card comes from, for the application's own context.
            place = cards.places[0]
            handle = _handle(answer)
            assert place == CardPlace(conversation_key("alice@example.com", handle), handle, place.turn_id) and place.turn_id
            assert seen["status"] == "awaiting_input"  # the journal says the turn waits
            assert cards.updated == [("slack-1", card, [])]  # a verdict rewrites it as the gate wrote it
            assert cards.retracted == [("slack-1", "withdrawn")]
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_read_tokens_turn_asks_nobody_even_with_a_way_to_the_person() -> None:
    cards = FakeCards()
    outcome: dict = {}

    async def asks(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        try:
            await chat.post_confirmation(msg.ref, ApprovalCard(agent="helper", headline="x"), [], callback_url="")
        except NobodyToAnswer as exc:
            outcome["card"] = str(exc)
        await chat.post_reply(msg.ref, "ok")
        return TurnOutcome.REPLIED

    hub, _ = _hub(8621, script=asks, cards=cards)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8621, "ask_agent", {"text": "x"})  # tok-reader: read alone
            assert _text(answer).startswith("ok\n\n")
            assert "may only read" in outcome["card"] and cards.posted == []  # never delivered
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
            assert _text(answer).startswith("ok\n\n")
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
            assert _text(answer).startswith("Here it is\n\nFiles the agent produced (kept in its conversation): report.csv\n\nConversation: ")
    finally:
        await hub.stop()


def test_the_factory_builds_an_mcp_handle_only_over_a_hub() -> None:
    assert "mcp" in GATEWAY_KINDS
    hub = McpHub("127.0.0.1", 0, FakeAuthorization(), store=MemoryMcpTurns())
    with_hub = GatewayFactory(directory=None, loop_guard=LoopGuard(), dispatcher=None, mcp_hub=hub)  # type: ignore[arg-type]
    handle = with_hub.create("helper", GatewayConfig(kind="mcp"))
    assert handle is not None and isinstance(handle.chat, McpChatClient)
    assert handle.denied_caps == {CAP_WIDGETS, CAP_FORMS, CAP_DELEGATION}
    gateway = handle.create_gateway(FakeSink())
    assert isinstance(gateway, McpGateway) and "helper" in hub.agents
    with pytest.raises(TypeError):
        handle.create_gateway(object())  # type: ignore[arg-type]
    without = GatewayFactory(directory=None, loop_guard=LoopGuard(), dispatcher=None)  # type: ignore[arg-type]
    assert without.create("helper", GatewayConfig(kind="mcp")) is None


@pytest.mark.asyncio
async def test_a_gateway_call_is_what_a_tool_sees() -> None:
    said: list[str] = []

    async def asker(call, text, agent):
        return f"{agent or 'default'}: {text}"

    async def waiter(call, handle):
        return TurnStanding(handle=handle, state=TURN_DONE, answer="later")

    async def progress(text):
        said.append(text)

    call = GatewayCall(TOKENS["tok-reader"], {"text": "hi"}, "", ask_agent=asker, wait_turn=waiter, progress=progress)
    assert await AskAgentTool().run(call) == "default: hi"
    await call.progress("working")
    assert said == ["working"] and call.handle == "" and call.turn_id == ""
    with pytest.raises(CallFailed):
        await AskAgentTool().run(GatewayCall(TOKENS["tok-reader"], {}, "", ask_agent=asker, wait_turn=waiter, progress=progress))
    waiting = GatewayCall(TOKENS["tok-reader"], {"conversation": "h1"}, "h1", ask_agent=asker, wait_turn=waiter, progress=progress)
    assert await WaitAgentTool().run(waiting) == "later"
    assert AskAgentTool().long_running and not WaitAgentTool().long_running
    assert AskAgentTool().conversation_argument == WaitAgentTool().conversation_argument == "conversation"


# -- tasks ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_client_with_the_tasks_extension_gets_a_task_and_polls_it() -> None:
    gate = asyncio.Event()
    cards = FakeCards()
    card = ApprovalCard(agent="helper", headline="wants to run deploy.", tool="deploy")

    async def slow(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm", "rs-2", "t", "t")
        trace = hub.turns.begin(record, chat)
        trace.on_event(TextProgress("Reading the runbook"))
        trace.on_event(ToolStarted("c1", "deploy", {}, caption="Deploying"))
        trace.on_event(CallAwaiting("deploy"))
        post_id = await chat.post_confirmation(msg.ref, card, [Action(id="ok", label="Allow", value="yes")], callback_url="")
        await gate.wait()
        await chat.update_confirmation(post_id, card, [], callback_url="")
        trace.on_event(ToolFinished("c1", "deploy", is_error=False))
        await trace.end()
        await chat.post_reply(msg.ref, "deployed")
        return TurnOutcome.REPLIED

    hub, sinks = _hub(8612, script=slow, cards=cards)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            status, answer, _ = await _call(s, 8612, "ask_agent", {"text": "deploy"}, token="tok-actor", tasks=True)
            assert status == 200
            created = answer["result"]
            assert created["resultType"] == "task" and created["status"] == "working"
            assert created["ttlMs"] == 600_000 and created["pollIntervalMs"] == 2000 and created["createdAt"]
            task_id = created["taskId"]
            assert "content" not in created
            await asyncio.sleep(0.05)
            _, answer, _ = await _task_rpc(s, 8612, "tasks/get", task_id, token="tok-actor")
            got = answer["result"]
            assert got["resultType"] == "complete" and got["status"] == "working"  # a card up is still the server's wait
            assert got["statusMessage"].startswith("The agent is waiting for the person's yes in their chat (deploy)")
            assert got["lastUpdatedAt"] >= got["createdAt"] and "result" not in got
            # The task, like the conversation, is the person's: another token of
            # theirs reads it; another person does not.
            _, answer, _ = await _task_rpc(s, 8612, "tasks/get", task_id, token="tok-reader")
            assert answer["result"]["taskId"] == task_id
            _, answer, _ = await _task_rpc(s, 8612, "tasks/get", task_id, token="tok-bob")
            assert answer["error"]["code"] == -32602
            # Without the extension declared, the tasks methods are not the client's.
            _, answer, _ = await _task_rpc(s, 8612, "tasks/get", task_id, token="tok-actor", tasks=False)
            assert answer["error"]["code"] == -32021
            assert answer["error"]["data"] == {"requiredCapabilities": {"extensions": {TASKS_EXTENSION: {}}}}
            # Nothing of the task asks the client for input: an update is taken and ignored.
            _, answer, _ = await _rpc(s, 8612, "tasks/update", {"taskId": task_id, "inputResponses": {}, "_meta": WITH_TASKS},
                                      headers=_headers("tok-actor", method="tasks/update"))
            assert answer["result"] == {"resultType": "complete"}

            gate.set()
            answer = await _settled(s, 8612, task_id, token="tok-actor")
            done = answer["result"]
            assert done["status"] == "completed" and "statusMessage" not in done
            result = done["result"]
            assert result["resultType"] == "complete" and result["isError"] is False
            handle = result["_meta"][META_CONVERSATION]
            assert result["content"] == [{"type": "text", "text": f"deployed\n\nConversation: {handle}"}]
            assert sinks["helper"].submitted[0].conversation_id == conversation_key("alice@example.com", handle)
            assert cards.posted[0][0] == TOKENS["tok-actor"]
            # A cancel of an ended task is acknowledged all the same (cooperative, idempotent).
            _, answer, _ = await _task_rpc(s, 8612, "tasks/cancel", task_id, token="tok-actor")
            assert answer["result"] == {"resultType": "complete"}
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_task_says_what_its_tool_says_and_is_forgotten_after_its_ttl() -> None:
    tool = LongTool()
    clock = Clock()
    hub, _ = _hub(8613, tools=(tool,), task_ttl_ms=300, call_wait_s=0.1, clock=clock)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8613, "long_job", tasks=True)
            task_id = answer["result"]["taskId"]
            assert answer["result"]["ttlMs"] == 300
            await asyncio.sleep(0.02)
            _, answer, _ = await _task_rpc(s, 8613, "tasks/get", task_id)
            assert answer["result"] == {**answer["result"], "status": "working", "statusMessage": "counting"}
            # The same call for a client without tasks: the state, with the tool's word.
            _, answer, _ = await _call(s, 8613, "long_job")
            assert "(now: counting)" in _text(answer)
            tool.gate.set()
            answer = await _settled(s, 8613, task_id)
            assert answer["result"]["status"] == "completed" and answer["result"]["result"]["content"][0]["text"].startswith("counted")
            clock.advance(1)
            _, answer, _ = await _task_rpc(s, 8613, "tasks/get", task_id)
            assert answer["error"]["code"] == -32602  # kept as long as the server said, then gone
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_cancelling_a_task_stops_its_turn() -> None:
    async def until_stopped(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await sink.gate.wait()
        await chat.post_notice(msg.ref, "Stopped.", code="stopped")
        return TurnOutcome.STOPPED

    hub = McpHub("127.0.0.1", 8614, FakeAuthorization(), store=MemoryMcpTurns())
    sink = StoppableFakeSink(until_stopped)
    hub.register_agent("helper", sink, McpChatClient(hub.turns, "helper", cards=None, asker_of=hub.asker_of))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8614, "ask_agent", {"text": "go"}, tasks=True)
            task_id = answer["result"]["taskId"]
            await asyncio.sleep(0.05)
            _, answer, _ = await _task_rpc(s, 8614, "tasks/get", task_id)
            assert answer["result"]["status"] == "working"
            _, answer, _ = await _task_rpc(s, 8614, "tasks/cancel", task_id)
            assert answer["result"] == {"resultType": "complete"}
            conversation = sink.submitted[0].conversation_id
            assert sink.stopped == [(conversation, "mcp", "alice@example.com")]
            answer = await _settled(s, 8614, task_id)
            assert answer["result"]["status"] == "cancelled"
            assert hub.turns.active(conversation) is None  # the turn was closed when the flow ended it
            # The conversation is free for the next call; an unknown task is said so.
            _, answer, _ = await _call(s, 8614, "ask_agent", {"text": "again"}, tasks=True)
            assert answer["result"]["resultType"] == "task"
            _, answer, _ = await _task_rpc(s, 8614, "tasks/get", "nope")
            assert answer["error"]["code"] == -32602
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_task_whose_tool_breaks_is_completed_with_an_error_result() -> None:
    tool = LongTool()
    tool.fail = True
    hub, _ = _hub(8615, tools=(tool,))
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8615, "long_job", tasks=True)
            task_id = answer["result"]["taskId"]
            tool.gate.set()
            answer = await _settled(s, 8615, task_id)
            # The tool's own failure is a result that says so, as in a plain call.
            assert answer["result"]["status"] == "completed" and answer["result"]["result"]["isError"] is True
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_a_turn_of_a_replica_that_restarted_is_failed_and_another_replica_reads_it() -> None:
    """Two hubs over one store, as two pods: a task started on one is polled on
    the other; a cancel reaches the one that runs it; a turn of a pod that is
    gone is failed rather than waited for."""
    store = MemoryMcpTurns()
    gate = asyncio.Event()

    async def slow(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await gate.wait()
        await chat.post_reply(msg.ref, "done on a")
        return TurnOutcome.REPLIED

    class Live:
        def __init__(self) -> None:
            self.alive = {"pod-a", "pod-b"}

        async def replica_url(self, replica):
            return f"http://{replica}" if replica in self.alive else None

        async def claim_conversation(self, agent, conversation, replica, *, take=False):
            raise AssertionError("not asked here")

    live = Live()
    hub_a, _ = _hub(8616, script=slow, store=store, replica=Replica("pod-a", "http://pod-a"), replicas=live, call_wait_s=0.1)  # type: ignore[arg-type]
    hub_b, _ = _hub(8617, script=slow, store=store, replica=Replica("pod-b", "http://pod-b"), replicas=live, call_wait_s=0.1)  # type: ignore[arg-type]
    await hub_a.start()
    await hub_b.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8616, "ask_agent", {"text": "go"}, tasks=True)
            task_id = answer["result"]["taskId"]
            # Polled on the other pod: the record says working.
            _, answer, _ = await _task_rpc(s, 8617, "tasks/get", task_id)
            assert answer["result"]["status"] == "working"
            # Waited for on the other pod: the state, read from the record.
            _, answer, _ = await _call(s, 8617, "ask_agent", {"text": "go"})  # the same request joins, anywhere
            handle = _handle(answer)
            assert "still working" in _text(answer)
            gate.set()
            _, answer, _ = await _rpc(s, 8617, "tools/call", {"name": "wait_agent", "arguments": {"conversation": handle}})
            assert _text(answer).startswith("done on a")
            answer = await _settled(s, 8617, task_id)
            assert answer["result"]["status"] == "completed"

            # A cancel that lands on the other pod reaches the one that runs the turn.
            gate.clear()
            _, answer, _ = await _call(s, 8616, "ask_agent", {"text": "second"}, tasks=True)
            task_id = answer["result"]["taskId"]
            _, answer, _ = await _task_rpc(s, 8617, "tasks/cancel", task_id)
            answer = await _settled(s, 8616, task_id)  # the running pod reads the cancel on its next poll
            assert answer["result"]["status"] == "cancelled"

            # The pod that ran a turn is gone: the turn is failed, not waited for.
            _, answer, _ = await _call(s, 8616, "ask_agent", {"text": "third"}, tasks=True)
            task_id = answer["result"]["taskId"]
            live.alive.discard("pod-a")
            _, answer, _ = await _task_rpc(s, 8617, "tasks/get", task_id)
            assert answer["result"]["status"] == "failed" and "restarted" in answer["result"]["error"]["message"]
            assert answer["result"]["statusMessage"] == answer["result"]["error"]["message"]
            # And a pod coming back closes what it was running.
            live.alive.add("pod-a")
            gate.set()
    finally:
        await hub_b.stop()
        await hub_a.stop()


@pytest.mark.asyncio
async def test_a_cancel_from_another_replica_is_not_wiped_by_the_owners_steps() -> None:
    """The owner writes the record at every step; a cancel another replica
    marked between two of its writes must survive them and stop the turn."""
    store = MemoryMcpTurns()

    async def busy(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        record = SessionRecord("helper", msg.channel_id, msg.conversation_id, "dm", "rs-3", "t", "t")
        trace = hub_a.turns.begin(record, chat)
        step = 0
        while not sink.gate.is_set():
            step += 1
            trace.on_event(TextProgress(f"step {step}"))  # a write of the record each time
            try:
                await asyncio.wait_for(sink.gate.wait(), 0.03)
            except asyncio.TimeoutError:
                continue
        await chat.post_notice(msg.ref, "Stopped.", code="stopped")
        return TurnOutcome.STOPPED

    class Live:
        async def replica_url(self, replica):
            return f"http://{replica}"

        async def claim_conversation(self, agent, conversation, replica, *, take=False):
            raise AssertionError("not asked here")

    hub_a = McpHub("127.0.0.1", 8618, FakeAuthorization(), store=store, replica=Replica("pod-a", ""), replicas=Live())  # type: ignore[arg-type]
    sink = StoppableFakeSink(busy)
    hub_a.register_agent("helper", sink, McpChatClient(hub_a.turns, "helper", cards=None, asker_of=hub_a.asker_of))
    hub_b, _ = _hub(8619, store=store, replica=Replica("pod-b", ""), replicas=Live())  # type: ignore[arg-type]
    await hub_a.start()
    await hub_b.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, answer, _ = await _call(s, 8618, "ask_agent", {"text": "go"}, tasks=True)
            task_id = answer["result"]["taskId"]
            await asyncio.sleep(0.15)  # steps are being written
            _, answer, _ = await _task_rpc(s, 8619, "tasks/cancel", task_id)
            assert answer["result"] == {"resultType": "complete"}
            answer = await _settled(s, 8619, task_id)
            assert answer["result"]["status"] == "cancelled" and sink.stopped
    finally:
        sink.gate.set()
        await hub_b.stop()
        await hub_a.stop()


@pytest.mark.asyncio
async def test_the_same_request_after_the_window_is_a_new_turn_and_a_stop_of_the_engine_is_not_a_cancel() -> None:
    clock = Clock()
    gate = asyncio.Event()

    async def slow(msg: IncomingMessage, chat: McpChatClient) -> TurnOutcome:
        await gate.wait()
        await chat.post_reply(msg.ref, "done")
        return TurnOutcome.REPLIED

    store = MemoryMcpTurns()
    hub, sinks = _hub(8620, script=slow, call_wait_s=0.05, repeat_window_s=120, clock=clock, store=store)
    await hub.start()
    try:
        async with aiohttp.ClientSession() as s:
            _, first, _ = await _call(s, 8620, "ask_agent", {"text": "same"})
            clock.advance(121)
            _, second, _ = await _call(s, 8620, "ask_agent", {"text": "same"})
            assert _handle(second) != _handle(first) and len(sinks["helper"].submitted) == 2
        await hub.stop()  # the engine stops with both turns running
        await asyncio.sleep(0.05)
        states = {r.state for r in store.records.values()}
        assert states == {"failed"} and all("restarted" in r.answer for r in store.records.values())  # not a client's cancel
    finally:
        gate.set()
