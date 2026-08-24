"""ward's door: mutual TLS, and who a certificate says you are.

The point of the whole move is that identity comes from the handshake rather
than from anything the caller writes, so that is what these test: a caller with
no certificate never reaches a handler, an agent's certificate cannot drive the
operator's routes, and the name the broker sees is the one the CA signed.

Real TLS on a real socket — a fake here would assert nothing, since the property
under test belongs to the TLS layer.
"""

import ssl
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
import pytest

from crucible.store.base import DECISION_APPROVED_ONCE
from ward.ca import OPERATOR_CN, CertificateAuthority
from ward.decisions import DECISION_NO_POLICY
from ward.ports import (
    BackendStatus,
    LeaseRequest,
    LeaseResult,
    UnlockMaterial,
)
from ward.server import WardServer, mutual_tls


class FakeBroker:
    def __init__(self, result: LeaseResult | None = None) -> None:
        self.result = result or LeaseResult(
            granted=True, decision=DECISION_APPROVED_ONCE, values={"T": "value"}
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


@dataclass
class Rig:
    server: WardServer
    broker: FakeBroker
    ca: CertificateAuthority
    root: Path
    port: int
    issued: dict[str, tuple[Path, Path]] = field(default_factory=dict)

    def identity(self, name: str) -> tuple[Path, Path]:
        """A client certificate for ``name``, written where a client can load it."""
        if name not in self.issued:
            material = self.ca.issue_client(name)
            cert, key = self.root / f"{name}.crt", self.root / f"{name}.key"
            material.write(cert, key)
            self.issued[name] = (cert, key)
        return self.issued[name]

    def context(self, name: str | None) -> ssl.SSLContext:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=str(self.root / "ca.crt")
        )
        if name is not None:
            cert, key = self.identity(name)
            context.load_cert_chain(certfile=str(cert), keyfile=str(key))
        return context

    def url(self, path: str) -> str:
        # The server certificate names "localhost", so the URL has to as well —
        # verifying the name is half of what TLS is for.
        return f"https://localhost:{self.port}{path}"


async def _rig(tmp_path: Path, port: int, broker: FakeBroker | None = None) -> Rig:
    ca, ca_material = CertificateAuthority.create()
    ca_material.write(tmp_path / "ca.crt", tmp_path / "ca.key")
    server_material = ca.issue_server(("localhost", "127.0.0.1"))
    server_material.write(tmp_path / "ward.crt", tmp_path / "ward.key")

    broker = broker or FakeBroker()
    server = WardServer(
        broker,  # type: ignore[arg-type]  # structural test double
        ca,
        host="127.0.0.1",
        port=port,
        ssl_context=mutual_tls(
            certificate=tmp_path / "ward.crt",
            key=tmp_path / "ward.key",
            ca=tmp_path / "ca.crt",
        ),
    )
    await server.start()
    return Rig(server, broker, ca, tmp_path, port)


def _body(**over) -> dict:
    base = {
        "bindings": [{"env": "T", "ref": "vault://github-token"}],
        "reason": "push the release",
        "command": ["gh", "release", "create"],
    }
    base.update(over)
    return base


# -- the handshake -------------------------------------------------------------


async def test_without_a_certificate_no_handler_is_reached(tmp_path: Path) -> None:
    """Refused by TLS, not by code — the request never becomes a request."""
    rig = await _rig(tmp_path, 8561)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientError):
                async with session.post(
                    rig.url("/lease"), json=_body(), ssl=rig.context(None)
                ):
                    pass
        assert rig.broker.seen == []
    finally:
        await rig.server.stop()


async def test_a_certificate_from_another_ca_is_refused(tmp_path: Path) -> None:
    stranger, _ = CertificateAuthority.create("somebody-else")
    material = stranger.issue_client("assistant")
    material.write(tmp_path / "fake.crt", tmp_path / "fake.key")
    rig = await _rig(tmp_path, 8562)
    try:
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=str(tmp_path / "ca.crt")
        )
        context.load_cert_chain(str(tmp_path / "fake.crt"), str(tmp_path / "fake.key"))
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientError):
                async with session.post(rig.url("/lease"), json=_body(), ssl=context):
                    pass
        assert rig.broker.seen == []
    finally:
        await rig.server.stop()


# -- identity ------------------------------------------------------------------


async def test_the_agent_the_broker_sees_is_the_one_the_ca_signed(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8563)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"),
                json={**_body(), "agent": "somebody-else"},  # ignored: a claim
                ssl=rig.context("assistant"),
            ) as response:
                assert response.status == 200
                assert await response.json() == {"granted": True, "values": {"T": "value"}}
        assert rig.broker.seen[0].agent == "assistant"
    finally:
        await rig.server.stop()


async def test_one_agent_cannot_ask_as_another(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8564)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"), json=_body(), ssl=rig.context("builder")
            ) as response:
                assert response.status == 200
        assert rig.broker.seen[0].agent == "builder"
    finally:
        await rig.server.stop()


async def test_the_operator_certificate_is_not_an_agent(tmp_path: Path) -> None:
    """Administering and asking are different jobs. A policy is keyed by an
    agent's name, and `operator` is not one."""
    rig = await _rig(tmp_path, 8565)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"), json=_body(), ssl=rig.context(OPERATOR_CN)
            ) as response:
                assert response.status == 403
        assert rig.broker.seen == []
    finally:
        await rig.server.stop()


# -- what an agent may not do --------------------------------------------------


@pytest.mark.parametrize(
    "method, path", [("GET", "/status"), ("POST", "/unlock"), ("POST", "/certs/newbie")]
)
async def test_an_agent_cannot_reach_the_operator_routes(
    tmp_path: Path, method: str, path: str
) -> None:
    """404 rather than 403: an agent has no business learning that these exist."""
    rig = await _rig(tmp_path, 8566)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, rig.url(path), json={}, ssl=rig.context("assistant")
            ) as response:
                assert response.status == 404
        assert rig.broker.unlocked == []
    finally:
        await rig.server.stop()


# -- what the operator may do --------------------------------------------------


async def test_the_operator_can_read_status_and_unlock(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8567)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                rig.url("/status"), ssl=rig.context(OPERATOR_CN)
            ) as response:
                assert (await response.json())["usable"] is True
            async with session.post(
                rig.url("/unlock"),
                json={"unseal_key": "k", "auth_secret": "s"},
                ssl=rig.context(OPERATOR_CN),
            ) as response:
                assert (await response.json())["usable"] is True
        assert rig.broker.unlocked == [UnlockMaterial(unseal_key="k", auth_secret="s")]
    finally:
        await rig.server.stop()


async def test_the_operator_can_mint_an_identity_for_a_new_agent(tmp_path: Path) -> None:
    """The CA key lives here and nowhere else, so this is how a new agent gets
    one — which keeps a compromised engine from inventing agents."""
    rig = await _rig(tmp_path, 8568)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/certs/newbie"), json={}, ssl=rig.context(OPERATOR_CN)
            ) as response:
                assert response.status == 200
                issued = await response.json()
        from ward.ca import common_name_of

        assert common_name_of(issued["certificate"]) == "newbie"
        assert "PRIVATE KEY" in issued["key"]
        assert issued["ca"] == rig.ca.certificate
    finally:
        await rig.server.stop()


async def test_the_operator_name_cannot_be_minted_again(tmp_path: Path) -> None:
    rig = await _rig(tmp_path, 8569)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url(f"/certs/{OPERATOR_CN}"), json={}, ssl=rig.context(OPERATOR_CN)
            ) as response:
                assert response.status == 400
    finally:
        await rig.server.stop()


# -- the answer ----------------------------------------------------------------


async def test_a_refusal_carries_no_reason(tmp_path: Path) -> None:
    broker = FakeBroker(LeaseResult(granted=False, decision=DECISION_NO_POLICY))
    rig = await _rig(tmp_path, 8570, broker)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"), json=_body(), ssl=rig.context("assistant")
            ) as response:
                payload = await response.json()
        assert payload == {"granted": False, "status": "refused"}
        assert DECISION_NO_POLICY not in str(payload)
    finally:
        await rig.server.stop()


async def test_ward_does_not_ask_about_the_agents_conversation(tmp_path: Path) -> None:
    """It has no idea where the agent is talking, and does not need one: the
    card goes to an approver, not into the agent's channel."""
    rig = await _rig(tmp_path, 8571)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"), json=_body(), ssl=rig.context("assistant")
            ) as response:
                assert response.status == 200
        assert rig.broker.seen[0].runtime_session_id == ""
    finally:
        await rig.server.stop()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"bindings": []},
        {"bindings": [{"env": "BAD NAME", "ref": "vault://x"}]},
        {"bindings": [{"env": "T", "ref": "not-a-reference"}]},
        {"bindings": [{"env": f"E{n}", "ref": f"vault://s{n}"} for n in range(6)]},
    ],
)
async def test_a_malformed_request_is_a_client_error(tmp_path: Path, body: dict) -> None:
    rig = await _rig(tmp_path, 8572)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/lease"), json=body, ssl=rig.context("assistant")
            ) as response:
                assert response.status == 400
        assert rig.broker.seen == []
    finally:
        await rig.server.stop()


async def test_rotating_the_credential_is_an_operator_verb(tmp_path: Path) -> None:
    """The answer carries a live credential, so it goes to the certificate no
    agent has — over the same mutual TLS that carries a secret's value."""
    rig = await _rig(tmp_path, 8571)
    rig.server._operations = _FakeOperations()  # type: ignore[assignment]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                rig.url("/rotate"), json={}, ssl=rig.context(OPERATOR_CN)
            ) as response:
                assert response.status == 200
                assert (await response.json())["secret_id"] == "fresh"
            # An agent asking is not told a route exists at all.
            async with session.post(
                rig.url("/rotate"), json={}, ssl=rig.context("assistant")
            ) as response:
                assert response.status == 404
    finally:
        await rig.server.stop()


class _FakeOperations:
    async def rotate_credential(self) -> dict:
        return {"secret_id": "fresh"}
