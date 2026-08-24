"""The Vault adapter: the only module that knows Vault's API shape.

Hand-rolled over aiohttp rather than a client library. Six endpoints are used —
seal status, unseal, AppRole login, and KV v2 read/write/list — and a dedicated
client would bring a synchronous API into an async process for the privilege of
wrapping them.

Two behaviours are worth knowing before reading the code:

* **The credential lives only in memory.** ``unlock`` takes the AppRole secret
  id, logs in, and keeps the resulting token (and the secret id, to log in
  again) as instance state. Nothing is written to disk here, because a
  credential on disk beside the ciphertext is readable by every process that can
  read the ciphertext's path.
* **A 403 is treated as an expired token, once.** AppRole tokens have a TTL and
  the broker outlives it. Rather than a renewal loop, a read that comes back
  forbidden re-logs-in and retries a single time; a second 403 is a real
  permission problem and is raised.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ward.ports import BackendStatus, SecretBackendError, UnlockMaterial
from wardline.wire import SecretRef

logger = logging.getLogger(__name__)


# Renewable indefinitely while the broker keeps using it, so a long-running
# deployment doesn't need an unlock every hour — but still short enough that a
# stolen token stops working.
_TOKEN_PERIOD = "24h"


def _broker_policy(mount: str, role: str) -> str:
    """What the broker may do in Vault: everything inside its own mount, nothing
    at all outside it, and the two paths that let it replace its own credential.

    Storing a value runs through the broker too, so write is included; the
    ceiling is the mount, not the verb.

    Self-rotation is here so that the root token can be destroyed at the end of
    the ceremony rather than kept for the day a credential has to be replaced. A
    stolen secret id could mint a successor, but it can already log in whenever
    it likes — the gain is persistence past a rotation, and the price of the
    alternative is a permanent root token in somebody's password manager.
    """
    return (
        f'path "{mount}/data/*" {{ capabilities = ["create", "read", "update", "delete"] }}\n'
        f'path "{mount}/metadata/*" {{ capabilities = ["list", "read", "delete"] }}\n'
        f'path "{mount}/metadata" {{ capabilities = ["list"] }}\n'
        f'path "auth/approle/role/{role}/secret-id" {{ capabilities = ["create", "update"] }}\n'
        f'path "auth/approle/role/{role}/secret-id-accessor/lookup" '
        f'{{ capabilities = ["create", "update"] }}\n'
        f'path "auth/approle/role/{role}/secret-id-accessor/destroy" '
        f'{{ capabilities = ["create", "update"] }}\n'
        f'path "auth/approle/role/{role}/secret-id" {{ capabilities = ["list"] }}\n'
    )


@dataclass(frozen=True)
class VaultBootstrap:
    """What initialising a fresh Vault produced, and all of it that survives:
    the root token is destroyed before this is returned. The unseal key and the
    secret id belong in a password manager, not in a config file next to the
    data; the role id is not a credential and lives in the broker's env."""

    unseal_key: str
    role_id: str
    secret_id: str


class VaultBackend:
    """The SecretBackend over HashiCorp Vault's KV v2 engine."""

    def __init__(
        self, addr: str, *, mount: str = "secrets", role_id: str = "", timeout_s: float = 10.0
    ) -> None:
        self._addr = addr.rstrip("/")
        self._mount = mount.strip("/")
        # The AppRole and ACL policy the broker logs in as, derived from the
        # mount rather than configured. Both halves live in this adapter —
        # bootstrap creates them, login uses them — so a knob would only be a
        # way for the two to disagree.
        self._role = f"{self._mount}-broker"
        self._role_id = role_id
        self._timeout = ClientTimeout(total=timeout_s)
        self._session: ClientSession | None = None
        # In memory, deliberately. See the module docstring.
        self._token = ""
        self._secret_id = ""

    # -- plumbing -------------------------------------------------------------

    async def _client(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(timeout=self._timeout)
        return self._session

    async def _call(
        self, method: str, path: str, *, token: str = "", body: dict | None = None,
        params: dict | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """One request. Returns (status, payload) rather than raising on a bad
        status: several callers treat 404 or 400 as an ordinary answer."""
        session = await self._client()
        headers = {"X-Vault-Token": token or self._token} if (token or self._token) else {}
        url = f"{self._addr}/v1/{path.lstrip('/')}"
        try:
            async with session.request(
                method, url, headers=headers, json=body, params=params
            ) as response:
                text = await response.text()
                payload: dict[str, Any] = {}
                if text:
                    try:
                        payload = await response.json(content_type=None)
                    except ValueError:
                        payload = {"errors": [text[:200]]}
                return response.status, payload
        except (ClientError, TimeoutError) as exc:
            # The address, not the exception text: a connection error can carry
            # the URL with credentials in it on some transports.
            raise SecretBackendError(f"vault at {self._addr} is unreachable") from exc

    @staticmethod
    def _fail(status: int, payload: Mapping[str, Any], what: str) -> SecretBackendError:
        errors = payload.get("errors") or []
        detail = "; ".join(str(e) for e in errors)[:200] if errors else f"http {status}"
        return SecretBackendError(f"{what}: {detail}")

    # -- state ----------------------------------------------------------------

    async def status(self) -> BackendStatus:
        try:
            code, payload = await self._call("GET", "sys/seal-status")
        except SecretBackendError as exc:
            return BackendStatus(reachable=False, detail=str(exc))
        if code != 200:
            return BackendStatus(reachable=False, detail=f"seal-status: http {code}")
        sealed = bool(payload.get("sealed", True))
        initialized = bool(payload.get("initialized", False))
        return BackendStatus(
            reachable=True,
            sealed=sealed or not initialized,
            authenticated=bool(self._token),
            # The remedy names a command, so it belongs to whatever CLI is
            # driving this — the caller turns "not initialized" into advice.
            detail="" if initialized else "vault is not initialized",
        )

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        """Unseal if a key was supplied, then log in if a secret id was.

        Both halves are optional so the two deployment shapes share one path: a
        Vault that auto-unseals still needs the login, and a re-unlock after a
        credential rotation needs only the login.
        """
        state = await self.status()
        if not state.reachable:
            return state
        if state.sealed and material.unseal_key:
            code, payload = await self._call(
                "PUT", "sys/unseal", body={"key": material.unseal_key}
            )
            if code != 200:
                # Still sealed, whatever Vault disliked about the key — a
                # malformed one is refused before it is even tried, and the
                # caller's remedy is the same as for a wrong one.
                refusal = self._fail(code, payload, "unseal refused")
                raise SecretBackendError(str(refusal), sealed=True)
            if payload.get("sealed", True):
                # A threshold above one would need several distinct keys;
                # bootstrap uses a threshold of one, so this means a wrong key.
                progress = payload.get("progress", 0)
                raise SecretBackendError(
                    f"vault is still sealed after that key (progress {progress})", sealed=True
                )
        if material.auth_secret:
            self._secret_id = material.auth_secret
            await self._login()
        return await self.status()

    async def _login(self) -> None:
        if not self._role_id or not self._secret_id:
            raise SecretBackendError("no vault credential — the broker is locked")
        code, payload = await self._call(
            "POST", "auth/approle/login",
            body={"role_id": self._role_id, "secret_id": self._secret_id},
            token="-",  # any non-empty value; login must not send a stale token
        )
        if code != 200:
            self._token = ""
            raise self._fail(code, payload, "vault login refused")
        self._token = str((payload.get("auth") or {}).get("client_token") or "")
        if not self._token:
            raise SecretBackendError("vault login returned no token")
        logger.info("vault: logged in as %s", self._role)

    # -- values ---------------------------------------------------------------

    async def _kv(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """A KV call that survives its own token expiring. See the module
        docstring: one silent re-login, then the failure is real."""
        if not self._token:
            raise SecretBackendError("the broker holds no vault credential")
        code, payload = await self._call(method, path, body=body, params=params)
        if code == 403:
            await self._login()
            code, payload = await self._call(method, path, body=body, params=params)
        return code, payload

    async def read(self, ref: SecretRef) -> str:
        code, payload = await self._kv("GET", f"{self._mount}/data/{ref.name}")
        if code == 404:
            raise SecretBackendError(f"no secret named {ref.name}")
        if code != 200:
            raise self._fail(code, payload, f"reading {ref.name}")
        data = ((payload.get("data") or {}).get("data")) or {}
        if ref.field not in data:
            raise SecretBackendError(f"{ref.name} has no field {ref.field!r}")
        return str(data[ref.field])

    async def write(self, name: str, values: Mapping[str, str]) -> None:
        code, payload = await self._kv(
            "POST", f"{self._mount}/data/{name}", body={"data": dict(values)}
        )
        if code not in (200, 204):
            raise self._fail(code, payload, f"writing {name}")

    async def delete(self, name: str) -> None:
        # metadata, not data: deleting the data leaves the versions behind, and
        # "removed" has to mean the value is gone rather than shadowed.
        code, payload = await self._kv("DELETE", f"{self._mount}/metadata/{name}")
        if code not in (200, 204):
            raise self._fail(code, payload, f"removing {name}")

    async def names(self) -> list[str]:
        code, payload = await self._kv(
            "GET", f"{self._mount}/metadata", params={"list": "true"}
        )
        if code == 404:
            return []  # nothing written yet — an empty mount has no metadata
        if code != 200:
            raise self._fail(code, payload, "listing secrets")
        keys = (payload.get("data") or {}).get("keys") or []
        return sorted(str(k) for k in keys if not str(k).endswith("/"))

    # -- one-time setup -------------------------------------------------------

    async def bootstrap(self) -> VaultBootstrap:
        """Initialise a fresh Vault for this broker and return the material.

        One key share with a threshold of one: the ceremony this protects
        against — several holders having to agree — is not the one a personal
        deployment has. What it does buy is that the store is unreadable until
        somebody supplies the key after a restart.
        """
        code, payload = await self._call("GET", "sys/seal-status")
        if code != 200:
            raise self._fail(code, payload, "vault is not answering")
        if payload.get("initialized"):
            raise SecretBackendError("vault is already initialized")
        code, payload = await self._call(
            "PUT", "sys/init", body={"secret_shares": 1, "secret_threshold": 1}
        )
        if code != 200:
            raise self._fail(code, payload, "vault init failed")
        keys = payload.get("keys_base64") or payload.get("keys") or []
        root = str(payload.get("root_token") or "")
        if not keys or not root:
            raise SecretBackendError("vault init returned no key or no root token")
        unseal_key = str(keys[0])

        await self.unlock(UnlockMaterial(unseal_key=unseal_key))
        role_id, secret_id = await self._provision(root)
        # Log in right away, so `init` leaves a usable broker
        # rather than a setup that only works after the next unlock.
        self._role_id, self._secret_id = role_id, secret_id
        await self._login()
        # And destroy the root token. Nothing needs it again: the broker runs on
        # its AppRole, and its policy lets it replace that itself. A credential
        # that can do everything, kept for a day that may never come, is a
        # credential somebody has to protect for ever — and if that day does
        # come, the unseal key regenerates one (`vault operator generate-root`).
        code, payload = await self._call("POST", "auth/token/revoke-self", token=root)
        if code not in (200, 204):
            logger.warning("could not revoke the root token: http %s", code)
        return VaultBootstrap(unseal_key=unseal_key, role_id=role_id, secret_id=secret_id)

    async def rotate(self) -> str:
        """Replace the credential this broker logs in with, and destroy the old.

        Runs as the broker itself — see the policy. The new secret id is
        returned; the running process keeps its current token until that
        expires, so whoever rotates has to unlock with the new one.
        """
        if not self._token:
            await self._login()
        code, payload = await self._kv("POST", f"auth/approle/role/{self._role}/secret-id")
        if code != 200:
            raise self._fail(code, payload, "minting a new secret id")
        data = payload.get("data") or {}
        fresh, keep = str(data.get("secret_id") or ""), str(data.get("secret_id_accessor") or "")
        if not fresh:
            raise SecretBackendError("vault returned an empty secret id")
        # `?list=true` rather than the LIST verb, as `names` does: not every
        # HTTP stack between here and Vault accepts a method it has never heard
        # of, and this spelling is Vault's own documented alternative.
        code, payload = await self._kv(
            "GET", f"auth/approle/role/{self._role}/secret-id", params={"list": "true"}
        )
        for accessor in (payload.get("data") or {}).get("keys") or []:
            if str(accessor) == keep:
                continue
            await self._call(
                "POST", f"auth/approle/role/{self._role}/secret-id-accessor/destroy",
                body={"secret_id_accessor": str(accessor)},
            )
        self._secret_id = fresh
        logger.info("the broker's credential was replaced")
        return fresh

    async def _provision(self, root: str) -> tuple[str, str]:
        """Create the mount, the policy and the AppRole, as root."""
        code, payload = await self._call(
            "POST", f"sys/mounts/{self._mount}", token=root,
            body={"type": "kv", "options": {"version": "2"}},
        )
        # 400 is what an existing mount looks like; re-running setup is allowed.
        if code not in (200, 204, 400):
            raise self._fail(code, payload, "enabling the kv engine")
        code, payload = await self._call(
            "PUT", f"sys/policies/acl/{self._role}", token=root,
            body={"policy": _broker_policy(self._mount, self._role)},
        )
        if code not in (200, 204):
            raise self._fail(code, payload, "writing the broker policy")
        code, payload = await self._call(
            "POST", "sys/auth/approle", token=root, body={"type": "approle"}
        )
        if code not in (200, 204, 400):
            raise self._fail(code, payload, "enabling approle")
        code, payload = await self._call(
            "POST", f"auth/approle/role/{self._role}", token=root,
            body={"token_policies": self._role, "token_period": _TOKEN_PERIOD},
        )
        if code not in (200, 204):
            raise self._fail(code, payload, "creating the broker role")
        code, payload = await self._call(
            "GET", f"auth/approle/role/{self._role}/role-id", token=root
        )
        if code != 200:
            raise self._fail(code, payload, "reading the role id")
        role_id = str((payload.get("data") or {}).get("role_id") or "")
        code, payload = await self._call(
            "POST", f"auth/approle/role/{self._role}/secret-id", token=root
        )
        if code != 200:
            raise self._fail(code, payload, "minting the secret id")
        secret_id = str((payload.get("data") or {}).get("secret_id") or "")
        if not role_id or not secret_id:
            raise SecretBackendError("vault returned an empty approle credential")
        return role_id, secret_id

    async def close(self) -> None:
        self._token = ""
        self._secret_id = ""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
