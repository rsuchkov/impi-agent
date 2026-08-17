"""The broker's routes on the tool server (crucible/tools/server.py).

A real server on a real port, hit with a real client — the same shape as the
tool-dispatch tests next door. What is being pinned here is the wire contract:
who may call, what a caller is told, and what has no route at all.
"""

import aiohttp
import pytest

from crucible.ports.chat.directory import AgentInfo
from crucible.secrets.ports import (
    BackendStatus,
    LeaseRequest,
    LeaseResult,
    UnlockMaterial,
)
from crucible.store.base import (
    DECISION_APPROVED_ONCE,
    DECISION_LOCKED,
    DECISION_NO_POLICY,
    DECISION_NOT_PERMITTED,
)
from crucible.tools.registry import build_registry
from crucible.tools.server import ToolServer

AGENTS = [
    AgentInfo(
        name="assistant", role="general", description="", username="assistant",
        user_id="bot-1",
    )
]
TOKEN = {"X-Tool-Token": "secret-tok"}


class FakeBroker:
    def __init__(self, result: LeaseResult | None = None) -> None:
        self.result = result or LeaseResult(
            granted=True, decision=DECISION_APPROVED_ONCE, values={"GITHUB_TOKEN": "ghp_x"}
        )
        self.seen: list[LeaseRequest] = []
        self.unlocked: list[UnlockMaterial] = []
        self.state = BackendStatus(reachable=True, sealed=False, authenticated=True)

    async def lease(self, request: LeaseRequest) -> LeaseResult:
        self.seen.append(request)
        return self.result

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        self.unlocked.append(material)
        return self.state

    async def status(self) -> BackendStatus:
        return self.state


class FakeDirectory:
    def agent_user_ids(self):
        return frozenset(a.user_id for a in AGENTS)

    def list_agents(self):
        return list(AGENTS)


async def _server(port: int, broker: FakeBroker | None = None) -> ToolServer:
    registry = build_registry()
    server = ToolServer(
        registry,
        directory=FakeDirectory(),  # type: ignore[arg-type]  # structural test double
        admins={},
        tokens={"secret-tok": "assistant"},
        allowlists={"assistant": frozenset(registry.names())},
        port=port,
        secret_svc=broker,  # type: ignore[arg-type]  # structural test double
    )
    await server.start()
    return server


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _body(**over) -> dict:
    base = {
        "bindings": [{"env": "GITHUB_TOKEN", "ref": "vault://github-token"}],
        "reason": "push release",
        "command": ["gh", "release", "create"],
    }
    base.update(over)
    return base


# -- leasing -------------------------------------------------------------------


async def test_a_granted_lease_returns_the_values() -> None:
    broker = FakeBroker()
    server = await _server(8495, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8495, "/secrets/lease"), json=_body(), headers=TOKEN
            ) as response:
                assert response.status == 200
                payload = await response.json()
        assert payload == {"granted": True, "values": {"GITHUB_TOKEN": "ghp_x"}}
        seen = broker.seen[0]
        assert seen.agent == "assistant"  # the token is the identity
        assert seen.secret == "github-token"
        assert seen.command == ("gh", "release", "create")
    finally:
        await server.stop()


async def test_the_conversation_travels_with_the_request() -> None:
    broker = FakeBroker()
    server = await _server(8496, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8496, "/secrets/lease"),
                json=_body(),
                headers={**TOKEN, "X-Runtime-Session": "assistant--dm1"},
            ) as response:
                assert response.status == 200
        assert broker.seen[0].runtime_session_id == "assistant--dm1"
    finally:
        await server.stop()


async def test_a_lease_without_a_token_is_refused_before_the_broker() -> None:
    broker = FakeBroker()
    server = await _server(8497, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_url(8497, "/secrets/lease"), json=_body()) as response:
                assert response.status == 401
        assert broker.seen == []
    finally:
        await server.stop()


async def test_leases_are_a_404_when_secrets_are_off() -> None:
    server = await _server(8498, None)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8498, "/secrets/lease"), json=_body(), headers=TOKEN
            ) as response:
                assert response.status == 404
    finally:
        await server.stop()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"bindings": []},
        {"bindings": "GITHUB_TOKEN"},
        {"bindings": [{"env": "GITHUB TOKEN", "ref": "vault://github-token"}]},
        {"bindings": [{"env": "GITHUB_TOKEN", "ref": "github-token"}]},
        {"bindings": [{"env": "GITHUB_TOKEN", "ref": "vault://../../sys"}]},
    ],
)
async def test_a_malformed_request_is_a_client_error_not_a_refusal(body: dict) -> None:
    """400, with a reason. The caller built the request wrong, which is a
    different thing from being told no — and says nothing about what exists."""
    broker = FakeBroker()
    server = await _server(8499, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8499, "/secrets/lease"), json=body, headers=TOKEN
            ) as response:
                assert response.status == 400
        assert broker.seen == []
    finally:
        await server.stop()


async def test_a_refusal_carries_no_reason_at_all() -> None:
    """Every authorization outcome looks the same on the wire; only the ledger
    knows which it was."""
    for decision in (DECISION_NO_POLICY, DECISION_NOT_PERMITTED):
        broker = FakeBroker(LeaseResult(granted=False, decision=decision))
        server = await _server(8500, broker)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    _url(8500, "/secrets/lease"), json=_body(), headers=TOKEN
                ) as response:
                    assert response.status == 200
                    payload = await response.json()
            assert payload == {"granted": False, "status": "refused"}
            assert decision not in str(payload)
        finally:
            await server.stop()


async def test_an_engine_that_cannot_serve_says_so_separately() -> None:
    broker = FakeBroker(LeaseResult(granted=False, decision=DECISION_LOCKED))
    server = await _server(8501, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8501, "/secrets/lease"), json=_body(), headers=TOKEN
            ) as response:
                payload = await response.json()
        assert payload == {"granted": False, "status": "unavailable"}
    finally:
        await server.stop()


# -- unlocking -----------------------------------------------------------------


async def test_unlocking_takes_material_and_reports_the_new_state() -> None:
    broker = FakeBroker()
    server = await _server(8502, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _url(8502, "/secrets/unlock"),
                json={"unseal_key": "key", "auth_secret": "sid"},
            ) as response:
                assert response.status == 200
                payload = await response.json()
        assert payload["usable"] is True
        assert broker.unlocked == [UnlockMaterial(unseal_key="key", auth_secret="sid")]
    finally:
        await server.stop()


async def test_unlocking_with_nothing_is_a_client_error() -> None:
    broker = FakeBroker()
    server = await _server(8503, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(_url(8503, "/secrets/unlock"), json={}) as response:
                assert response.status == 400
        assert broker.unlocked == []
    finally:
        await server.stop()


async def test_status_reports_whether_the_engine_can_serve() -> None:
    broker = FakeBroker()
    broker.state = BackendStatus(reachable=True, sealed=False, authenticated=False)
    server = await _server(8504, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_url(8504, "/secrets/status")) as response:
                payload = await response.json()
        assert payload["enabled"] is True
        assert (payload["sealed"], payload["authenticated"], payload["usable"]) == (
            False, False, False,
        )
    finally:
        await server.stop()


async def test_status_says_so_when_secrets_are_off() -> None:
    server = await _server(8505, None)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(_url(8505, "/secrets/status")) as response:
                assert await response.json() == {"enabled": False}
    finally:
        await server.stop()


# -- what has no route ---------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/secrets/values"),
        ("GET", "/secrets"),
        ("POST", "/secrets/values/github-token"),
        ("GET", "/secrets/policies"),
        ("GET", "/secrets/audit"),
    ],
)
async def test_nothing_on_this_server_lists_or_writes_a_secret(
    method: str, path: str
) -> None:
    """Loopback is where the agents' shells are. A route that a local process
    can reach is a route an agent can reach, so the operator verbs live in the
    CLI and have no HTTP surface here at all."""
    broker = FakeBroker()
    server = await _server(8506, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, _url(8506, path), json={}, headers=TOKEN
            ) as response:
                assert response.status == 404
    finally:
        await server.stop()
