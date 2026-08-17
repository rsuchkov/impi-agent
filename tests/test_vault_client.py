"""The Vault adapter against a stub Vault (crucible/secrets/vault.py).

A real aiohttp server on a real port, in the house style — the adapter's whole
job is HTTP shape, and a mock of its own request method would assert nothing.
The stub keeps just enough state to make the interesting sequences reachable:
sealed vs unsealed, a token that expires, a mount that is empty.
"""

import pytest
from aiohttp import web

from crucible.secrets.ports import SecretBackendError, SecretRef, UnlockMaterial
from crucible.secrets.vault import VaultBackend

# A mount that is nobody's app name: the adapter derives its role and policy
# from whatever it is given, and the tests should exercise that rather than
# a value the deployment happens to use.
MOUNT = "kv"
UNSEAL_KEY = "unseal-key-1"
ROOT_TOKEN = "root-token-1"
ROLE_ID = "role-id-1"
SECRET_ID = "secret-id-1"


class StubVault:
    """Enough of Vault's API for the adapter to be exercised end to end."""

    def __init__(self, *, initialized: bool = True, sealed: bool = False) -> None:
        self.initialized = initialized
        self.sealed = sealed
        self.data: dict[str, dict[str, str]] = {}
        self.tokens: set[str] = set()
        self.logins = 0
        self.expire_next = False  # the next KV call answers 403, once
        self.deny_all = False  # every KV call answers 403 (the policy was pulled)
        self.reject_unseal = False  # answer 400, the way a malformed key is refused
        self.mounted = False
        self._runner: web.AppRunner | None = None

    # -- lifecycle ------------------------------------------------------------

    async def start(self, port: int) -> None:
        app = web.Application()
        app.router.add_get("/v1/sys/seal-status", self._seal_status)
        app.router.add_put("/v1/sys/init", self._init)
        app.router.add_put("/v1/sys/unseal", self._unseal)
        app.router.add_post("/v1/auth/approle/login", self._login)
        app.router.add_post("/v1/sys/mounts/{mount}", self._mount)
        app.router.add_put("/v1/sys/policies/acl/{name}", self._ok)
        app.router.add_post("/v1/sys/auth/approle", self._ok)
        app.router.add_post("/v1/auth/approle/role/{role}", self._ok)
        app.router.add_get("/v1/auth/approle/role/{role}/role-id", self._role_id)
        app.router.add_post("/v1/auth/approle/role/{role}/secret-id", self._secret_id)
        app.router.add_get(f"/v1/{MOUNT}/data/{{name}}", self._read)
        app.router.add_post(f"/v1/{MOUNT}/data/{{name}}", self._write)
        app.router.add_delete(f"/v1/{MOUNT}/metadata/{{name}}", self._delete)
        app.router.add_get(f"/v1/{MOUNT}/metadata", self._list)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # -- handlers -------------------------------------------------------------

    def _authorized(self, request: web.Request) -> bool:
        if self.deny_all:
            return False
        if self.expire_next:
            self.expire_next = False
            return False
        return request.headers.get("X-Vault-Token", "") in self.tokens

    async def _seal_status(self, _: web.Request) -> web.Response:
        return web.json_response({"sealed": self.sealed, "initialized": self.initialized})

    async def _init(self, _: web.Request) -> web.Response:
        self.initialized = True
        return web.json_response({"keys_base64": [UNSEAL_KEY], "root_token": ROOT_TOKEN})

    async def _unseal(self, request: web.Request) -> web.Response:
        if self.reject_unseal:
            return web.json_response(
                {"errors": ["'key' must be a valid hex or base64 string"]}, status=400
            )
        body = await request.json()
        if body.get("key") == UNSEAL_KEY:
            self.sealed = False
        return web.json_response({"sealed": self.sealed, "progress": 0})

    async def _login(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("role_id") != ROLE_ID or body.get("secret_id") != SECRET_ID:
            return web.json_response({"errors": ["invalid role or secret id"]}, status=400)
        self.logins += 1
        token = f"client-token-{self.logins}"
        self.tokens = {token}  # a fresh login retires the previous token
        return web.json_response({"auth": {"client_token": token}})

    async def _mount(self, request: web.Request) -> web.Response:
        if self.mounted:
            return web.json_response({"errors": ["path is already in use"]}, status=400)
        self.mounted = True
        return web.Response(status=204)

    async def _ok(self, _: web.Request) -> web.Response:
        return web.Response(status=204)

    async def _role_id(self, _: web.Request) -> web.Response:
        return web.json_response({"data": {"role_id": ROLE_ID}})

    async def _secret_id(self, _: web.Request) -> web.Response:
        return web.json_response({"data": {"secret_id": SECRET_ID}})

    async def _read(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errors": ["permission denied"]}, status=403)
        name = request.match_info["name"]
        if name not in self.data:
            return web.json_response({"errors": []}, status=404)
        return web.json_response({"data": {"data": self.data[name]}})

    async def _write(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errors": ["permission denied"]}, status=403)
        body = await request.json()
        self.data[request.match_info["name"]] = dict(body.get("data") or {})
        return web.Response(status=204)

    async def _delete(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errors": ["permission denied"]}, status=403)
        self.data.pop(request.match_info["name"], None)
        return web.Response(status=204)

    async def _list(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"errors": ["permission denied"]}, status=403)
        if not self.data:
            return web.json_response({"errors": []}, status=404)
        return web.json_response({"data": {"keys": sorted(self.data)}})


async def _live(port: int, **over) -> tuple[StubVault, VaultBackend]:
    """A stub Vault plus an adapter already logged in against it."""
    stub = StubVault(**over)
    await stub.start(port)
    backend = VaultBackend(f"http://127.0.0.1:{port}", mount=MOUNT, role_id=ROLE_ID)
    await backend.unlock(UnlockMaterial(auth_secret=SECRET_ID))
    return stub, backend


async def test_status_reports_an_uninitialized_vault_as_sealed() -> None:
    stub = StubVault(initialized=False, sealed=True)
    await stub.start(8481)
    backend = VaultBackend("http://127.0.0.1:8481", role_id=ROLE_ID)
    try:
        state = await backend.status()
        assert state.reachable and state.sealed and not state.authenticated
        assert not state.usable
        assert "init" in state.detail
    finally:
        await backend.close()
        await stub.stop()


async def test_an_unreachable_vault_is_a_status_not_a_crash() -> None:
    # Nothing is listening on this port; the engine must still be able to say
    # what is wrong rather than fail to start.
    backend = VaultBackend("http://127.0.0.1:8482", role_id=ROLE_ID)
    try:
        state = await backend.status()
        assert not state.reachable and not state.usable
        assert "unreachable" in state.detail
    finally:
        await backend.close()


async def test_unlock_unseals_and_logs_in() -> None:
    stub = StubVault(sealed=True)
    await stub.start(8483)
    backend = VaultBackend("http://127.0.0.1:8483", role_id=ROLE_ID)
    try:
        state = await backend.unlock(
            UnlockMaterial(unseal_key=UNSEAL_KEY, auth_secret=SECRET_ID)
        )
        assert state.usable
        assert stub.sealed is False
        assert stub.logins == 1
    finally:
        await backend.close()
        await stub.stop()


async def test_a_wrong_unseal_key_leaves_it_sealed_and_says_so() -> None:
    stub = StubVault(sealed=True)
    await stub.start(8484)
    backend = VaultBackend("http://127.0.0.1:8484", role_id=ROLE_ID)
    try:
        with pytest.raises(SecretBackendError) as caught:
            await backend.unlock(UnlockMaterial(unseal_key="wrong"))
        assert caught.value.sealed is True
        assert stub.sealed is True
    finally:
        await backend.close()
        await stub.stop()


async def test_a_malformed_unseal_key_also_reports_as_sealed() -> None:
    """A real Vault rejects a key that isn't base64 with a 400, before it ever
    tries it. That is still "sealed, try again" and not a different problem —
    the caller's remedy is the same."""
    stub = StubVault(sealed=True)
    stub.reject_unseal = True
    await stub.start(8531)
    backend = VaultBackend("http://127.0.0.1:8531", role_id=ROLE_ID)
    try:
        with pytest.raises(SecretBackendError) as caught:
            await backend.unlock(UnlockMaterial(unseal_key="not-base64!"))
        assert caught.value.sealed is True
        assert "unseal refused" in str(caught.value)
    finally:
        await backend.close()
        await stub.stop()


async def test_a_wrong_credential_is_refused_and_holds_no_token() -> None:
    stub = StubVault()
    await stub.start(8485)
    backend = VaultBackend("http://127.0.0.1:8485", role_id=ROLE_ID)
    try:
        with pytest.raises(SecretBackendError):
            await backend.unlock(UnlockMaterial(auth_secret="not-the-secret-id"))
        assert (await backend.status()).authenticated is False
    finally:
        await backend.close()
        await stub.stop()


async def test_values_round_trip_through_the_mount() -> None:
    stub, backend = await _live(8486)
    try:
        await backend.write("github-token", {"value": "ghp_xxx"})
        await backend.write("smtp", {"username": "bot", "password": "hunter2"})
        assert await backend.read(SecretRef("github-token")) == "ghp_xxx"
        assert await backend.read(SecretRef("smtp", "password")) == "hunter2"
        assert await backend.names() == ["github-token", "smtp"]
        await backend.delete("smtp")
        assert await backend.names() == ["github-token"]
    finally:
        await backend.close()
        await stub.stop()


async def test_an_empty_mount_lists_nothing_rather_than_failing() -> None:
    stub, backend = await _live(8487)
    try:
        assert await backend.names() == []
    finally:
        await backend.close()
        await stub.stop()


async def test_a_missing_secret_and_a_missing_field_both_raise() -> None:
    stub, backend = await _live(8488)
    try:
        with pytest.raises(SecretBackendError):
            await backend.read(SecretRef("nope"))
        await backend.write("smtp", {"username": "bot"})
        with pytest.raises(SecretBackendError):
            await backend.read(SecretRef("smtp", "password"))
    finally:
        await backend.close()
        await stub.stop()


async def test_an_expired_token_is_renewed_once_and_the_read_succeeds() -> None:
    """The engine outlives its AppRole token. A read that comes back forbidden
    logs in again and retries, so a long-running deployment doesn't need an
    unlock every time the TTL rolls over."""
    stub, backend = await _live(8489)
    try:
        await backend.write("github-token", {"value": "ghp_xxx"})
        assert stub.logins == 1
        stub.expire_next = True
        assert await backend.read(SecretRef("github-token")) == "ghp_xxx"
        assert stub.logins == 2
    finally:
        await backend.close()
        await stub.stop()


async def test_a_second_refusal_is_a_real_permission_problem() -> None:
    """Logging in again fixes an expired token but not a revoked policy, so the
    retry must be one attempt and not a loop."""
    stub, backend = await _live(8490)
    try:
        await backend.write("github-token", {"value": "ghp_xxx"})
        stub.deny_all = True
        with pytest.raises(SecretBackendError):
            await backend.read(SecretRef("github-token"))
        assert stub.logins == 2  # exactly one retry, then it gave up
    finally:
        await backend.close()
        await stub.stop()


async def test_reading_without_a_credential_refuses_before_the_network() -> None:
    stub = StubVault()
    await stub.start(8491)
    backend = VaultBackend("http://127.0.0.1:8491", role_id=ROLE_ID)
    try:
        with pytest.raises(SecretBackendError):
            await backend.read(SecretRef("github-token"))
        assert stub.logins == 0
    finally:
        await backend.close()
        await stub.stop()


async def test_bootstrap_initialises_and_leaves_a_usable_backend() -> None:
    stub = StubVault(initialized=False, sealed=True)
    await stub.start(8492)
    backend = VaultBackend("http://127.0.0.1:8492", mount=MOUNT)
    try:
        material = await backend.bootstrap()
        assert material.unseal_key == UNSEAL_KEY
        assert material.root_token == ROOT_TOKEN
        assert (material.role_id, material.secret_id) == (ROLE_ID, SECRET_ID)
        assert (await backend.status()).usable
        # Usable means usable: storing a value right after init must work.
        await backend.write("github-token", {"value": "ghp_xxx"})
        assert await backend.read(SecretRef("github-token")) == "ghp_xxx"
    finally:
        await backend.close()
        await stub.stop()


async def test_bootstrap_refuses_to_touch_an_initialised_vault() -> None:
    stub = StubVault(initialized=True)
    await stub.start(8493)
    backend = VaultBackend("http://127.0.0.1:8493", mount=MOUNT)
    try:
        with pytest.raises(SecretBackendError) as caught:
            await backend.bootstrap()
        assert "already initialized" in str(caught.value)
    finally:
        await backend.close()
        await stub.stop()


async def test_closing_forgets_the_credential() -> None:
    stub, backend = await _live(8494)
    try:
        assert (await backend.status()).authenticated
        await backend.close()
        assert (await backend.status()).authenticated is False
    finally:
        await backend.close()
        await stub.stop()
