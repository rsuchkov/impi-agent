"""ward's only door: HTTPS with a client certificate required.

Two kinds of caller come through it, and they are told apart by the certificate
the connection was made with, never by anything in the request:

* **an agent**, whose certificate carries its own name, may ask for a lease and
  nothing else;
* **the operator**, whose certificate carries ``operator``, may drive everything
  else — unlock, status, issuing certificates.

The identity comes from the TLS layer because that is the only part of the
exchange the caller cannot write. A header saying "I am the assistant" is a
claim; a certificate the CA signed and the handshake verified is not.

What this does not do is tell ``secret-exec`` apart from anything else in the
agent's container — see ``ca.py``. Every request still passes a human, which is
what that limitation is answered by.
"""

import logging
import ssl
from pathlib import Path
from typing import Any

from aiohttp import web

from ward.ca import OPERATOR_CN, CertificateAuthority
from ward.operations import Operations
from ward.ports import (
    LeaseRequest,
    SecretBackendError,
    SecretLeasing,
    UnlockMaterial,
    wire_status,
)
from wardline.wire import parse_ref

logger = logging.getLogger(__name__)

# How many distinct secrets one request may name. Several is the point — a
# command often needs two credentials, and asking twice for one operation is how
# a human learns to click without reading. A dozen is a basket.
MAX_SECRETS_PER_REQUEST = 5


def mutual_tls(*, certificate: Path, key: Path, ca: Path) -> ssl.SSLContext:
    """A server context that will not complete a handshake without a client
    certificate this CA signed."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certfile=str(certificate), keyfile=str(key))
    context.load_verify_locations(cafile=str(ca))
    # The whole point. CERT_OPTIONAL would let an anonymous caller reach the
    # handlers and leave the checking to code that could forget.
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def caller_name(request: web.Request) -> str:
    """Who the certificate says is calling, or "" if there is somehow no
    certificate (which the TLS layer should already have refused)."""
    ssl_object = request.transport.get_extra_info("ssl_object") if request.transport else None
    peer = ssl_object.getpeercert() if ssl_object is not None else None
    if not peer:
        return ""
    for pair in peer.get("subject", ()):
        for field, value in pair:
            if field == "commonName":
                return str(value)
    return ""


class WardServer:
    def __init__(
        self,
        broker: SecretLeasing,
        ca: CertificateAuthority,
        operations: Operations | None = None,
        *,
        host: str = "0.0.0.0",
        port: int = 8425,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self._broker = broker
        self._ca = ca
        self._operations = operations
        self._host = host
        self._port = port
        self._ssl = ssl_context
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/lease", self._lease)
        app.router.add_get("/status", self._status)
        app.router.add_post("/unlock", self._unlock)
        app.router.add_post("/certs/{name}", self._issue_cert)
        app.router.add_get("/ca", self._ca_certificate)
        # The operator's half. Every one of these is behind the same check, and
        # none of them has any route from an agent.
        app.router.add_get("/secrets", self._list_secrets)
        app.router.add_put("/secrets/{name}", self._put_secret)
        app.router.add_delete("/secrets/{name}", self._delete_secret)
        app.router.add_get("/policies", self._list_policies)
        app.router.add_put("/policies/{name}", self._put_policy)
        app.router.add_get("/grants", self._list_grants)
        app.router.add_delete("/grants/{id}", self._revoke_grant)
        app.router.add_get("/audit", self._list_audit)
        app.router.add_post("/rotate", self._rotate)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(
            self._runner, self._host, self._port, ssl_context=self._ssl
        ).start()
        logger.info("ward listening on https://%s:%d (client certificate required)",
                    self._host, self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- the agent's door ------------------------------------------------------

    async def _lease(self, request: web.Request) -> web.Response:
        """Ask for a secret, as whoever the certificate says you are.

        The response carries values or one of two words, and never the reason a
        request was turned down: a caller that could tell "no such secret" from
        "not yours" could map the store by trying names.
        """
        agent = caller_name(request)
        if not agent or agent == OPERATOR_CN:
            # The operator's certificate is for administering, not for standing
            # in as an agent — an agent's policy is keyed by its own name.
            return web.json_response({"error": "not an agent"}, status=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            lease = _lease_request(agent, body)
        except ValueError as exc:
            # Malformed, not refused: the caller built it wrong and needs to
            # know which part. Says nothing about what exists.
            return web.json_response({"error": str(exc)}, status=400)

        result = await self._broker.lease(lease)
        if not result.granted:
            return web.json_response(
                {"granted": False, "status": wire_status(result.decision)}
            )
        return web.json_response({"granted": True, "values": dict(result.values)})

    # -- the operator's door ---------------------------------------------------

    async def _status(self, request: web.Request) -> web.Response:
        if (refusal := _operator_only(request)) is not None:
            return refusal
        state = await self._broker.status()
        return web.json_response(
            {
                "reachable": state.reachable,
                "sealed": state.sealed,
                "authenticated": state.authenticated,
                "usable": state.usable,
                "detail": state.detail,
            }
        )

    async def _unlock(self, request: web.Request) -> web.Response:
        if (refusal := _operator_only(request)) is not None:
            return refusal
        try:
            body = await request.json()
        except Exception:
            body = {}
        material = UnlockMaterial(
            unseal_key=str(body.get("unseal_key") or ""),
            auth_secret=str(body.get("auth_secret") or ""),
        )
        if not material:
            return web.json_response({"error": "no unlock material"}, status=400)
        try:
            state = await self._broker.unlock(material)
        except Exception as exc:
            logger.warning("unlock failed")
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"usable": state.usable, "detail": state.detail})

    async def _issue_cert(self, request: web.Request) -> web.Response:
        """Mint a client certificate for an agent.

        Issuing happens here because the CA key is here and goes nowhere else.
        An engine that could mint identities could invent an agent; asking ward
        for one means the operator is in the loop for a new name.
        """
        if (refusal := _operator_only(request)) is not None:
            return refusal
        name = request.match_info["name"]
        if name == OPERATOR_CN:
            return web.json_response({"error": "reserved name"}, status=400)
        issued = self._ca.issue_client(name)
        logger.info("issued a client certificate for %s", name)
        return web.json_response(
            {"certificate": issued.certificate, "key": issued.key, "ca": self._ca.certificate}
        )

    # -- the operator's half of the store --------------------------------------

    async def _list_secrets(self, request: web.Request) -> web.Response:
        return await self._operate(request, lambda ops: ops.list_secrets())

    async def _put_secret(self, request: web.Request) -> web.Response:
        body = await _body(request)
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            return web.json_response({"error": "fields must be a non-empty object"}, status=400)
        name = request.match_info["name"]
        return await self._operate(request, lambda ops: ops.put_secret(name, fields))

    async def _delete_secret(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return await self._operate(request, lambda ops: ops.delete_secret(name))

    async def _list_policies(self, request: web.Request) -> web.Response:
        return await self._operate(request, lambda ops: ops.list_policies())

    async def _put_policy(self, request: web.Request) -> web.Response:
        body = await _body(request)
        name = request.match_info["name"]
        return await self._operate(request, lambda ops: ops.put_policy(name, body))

    async def _list_grants(self, request: web.Request) -> web.Response:
        include_dead = request.query.get("all") == "1"
        return await self._operate(
            request, lambda ops: ops.list_grants(include_dead=include_dead)
        )

    async def _revoke_grant(self, request: web.Request) -> web.Response:
        grant_id = request.match_info["id"]
        return await self._operate(request, lambda ops: ops.revoke_grant(grant_id))

    async def _list_audit(self, request: web.Request) -> web.Response:
        query = request.query
        limit = min(int(query.get("limit", "50") or 50), 500)
        agent, secret = query.get("agent", ""), query.get("secret", "")
        kind = query.get("kind", "")
        return await self._operate(
            request,
            lambda ops: ops.list_audit(
                limit=limit, agent=agent, secret=secret, kind=kind
            ),
        )

    async def _rotate(self, request: web.Request) -> web.Response:
        """Replace the credential the broker logs in with. Operator only, and
        the answer carries the new one — over the same mutual TLS that carries
        a secret's value, and to a certificate no agent has."""
        return await self._operate(request, lambda ops: ops.rotate_credential())

    async def _operate(self, request: web.Request, work) -> web.Response:
        """Every operator route, with the check and the failure handling in one
        place rather than nine."""
        if (refusal := _operator_only(request)) is not None:
            return refusal
        if self._operations is None:
            return web.json_response({"error": "not found"}, status=404)
        try:
            return web.json_response(await work(self._operations))
        except SecretBackendError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

    async def _ca_certificate(self, request: web.Request) -> web.Response:
        """The CA certificate. Public — it authenticates nobody on its own — but
        still behind the same door, because there is no reason for anything
        without a certificate to be talking to ward at all."""
        return web.json_response({"ca": self._ca.certificate})


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _operator_only(request: web.Request) -> web.Response | None:
    if caller_name(request) != OPERATOR_CN:
        return web.json_response({"error": "not found"}, status=404)
    return None


def _lease_request(agent: str, body: Any) -> LeaseRequest:
    """Turn the wire body into a LeaseRequest, or say what is wrong with it."""
    if not isinstance(body, dict):
        raise ValueError("expected a JSON object")
    raw = body.get("bindings")
    if not isinstance(raw, list) or not raw:
        raise ValueError("bindings must be a non-empty list")
    bindings = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each binding is an object with 'env' and 'ref'")
        env_name = str(item.get("env") or "")
        if not env_name.isidentifier():
            raise ValueError(f"not an environment variable name: {env_name!r}")
        bindings.append((env_name, parse_ref(str(item.get("ref") or ""))))
    named = {ref.name for _, ref in bindings}
    if len(named) > MAX_SECRETS_PER_REQUEST:
        raise ValueError(f"at most {MAX_SECRETS_PER_REQUEST} secrets in one request")
    command = body.get("command")
    return LeaseRequest(
        agent=agent,
        runtime_session_id="",  # ward does not know about agents' conversations
        bindings=tuple(bindings),
        reason=str(body.get("reason") or ""),
        command=tuple(str(part) for part in command) if isinstance(command, list) else (),
    )
