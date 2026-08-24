"""ward as an application: what ``build`` wires, and what it refuses to start
without (ward/app.py).

The composition *is* the boundary, written down. This container holds the
credential to the store, so what it does not build — a runtime, a gateway
factory, a scheduler — matters as much as what it does, and the parts it does
build are the ones a reader would otherwise have to take on faith.

Nothing here starts a listener or reaches a network: the questions are which
objects the settings produce, and what happens at startup when the unlock
material is (or is not) on disk.
"""

import os
from pathlib import Path

import pytest

from ward.app import OneBot, _sign_in, _unlock, build
from ward.ca import OPERATOR_CN as OPERATOR
from ward.ca import CertificateAuthority
from ward.cli import _hand_out
from ward.config import WardSettings
from ward.ports import BackendStatus, UnlockMaterial
from ward.vault import VaultBackend


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No developer's own .env or WARD_ variable decides what these build."""
    monkeypatch.chdir(tmp_path)
    for name in [key for key in os.environ if key.startswith("WARD_")]:
        monkeypatch.delenv(name, raising=False)


def _settings(root: Path, **over) -> WardSettings:
    return WardSettings(
        data_dir=str(root / "data"),
        tls_dir=str(root / "tls"),
        mattermost_url="http://mattermost.invalid:8065",
        mattermost_token="t",
        approvers="u1",
        **over,
    )


def _authority(settings: WardSettings) -> None:
    """What `ward init` leaves behind, without the Vault half of the ceremony."""
    settings.tls.mkdir(parents=True, exist_ok=True)
    ca, material = CertificateAuthority.create()
    material.write(settings.ca_cert, settings.ca_key)
    ca.issue_server(settings.names).write(settings.server_cert, settings.server_key)


class StubBroker:
    def __init__(self, *, opens: bool = True) -> None:
        self.material: UnlockMaterial | None = None
        self._opens = opens

    async def unlock(self, material: UnlockMaterial) -> BackendStatus:
        self.material = material
        return BackendStatus(
            reachable=True, sealed=not self._opens, authenticated=self._opens
        )


# -- what it refuses ------------------------------------------------------------


def test_without_a_certificate_authority_it_will_not_start(tmp_path: Path) -> None:
    """Starting anyway would mean a door with no way to tell its callers apart."""
    with pytest.raises(SystemExit) as raised:
        build(_settings(tmp_path))
    assert "ward init" in str(raised.value)


# -- what it wires --------------------------------------------------------------


async def test_it_builds_its_own_store_and_a_backend_at_the_configured_address(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, vault_addr="http://127.0.0.1:8200")
    _authority(settings)

    ward = build(settings)
    try:
        # Its own database, beside the credential — not the engine's.
        assert settings.db_path.parent.is_dir()
        backend = ward.broker.backend
        assert isinstance(backend, VaultBackend)
        # Pointed where the settings said, so a deployment that moves the store
        # off loopback cannot silently keep talking to loopback.
        assert backend._addr == "http://127.0.0.1:8200"
    finally:
        await ward.store.close()


def test_every_agent_is_answered_by_the_same_one_account() -> None:
    """ward runs no agents, so "which agent's client" has one answer — and a
    click on its card resolves a waiting request rather than starting a turn."""
    chat = object()
    bot = OneBot(chat)  # type: ignore[arg-type]
    assert bot.poster("assistant") is chat
    assert bot.poster("builder") is chat
    assert bot.get("anybody") is chat
    assert bot.sink("assistant") is None


# -- startup unlock -------------------------------------------------------------


async def test_mounted_material_is_handed_to_the_broker_at_startup(
    tmp_path: Path,
) -> None:
    unseal, secret_id = tmp_path / "unseal", tmp_path / "secret-id"
    unseal.write_text("key-1\n")
    secret_id.write_text("sid-1\n")
    settings = _settings(
        tmp_path, unseal_key_file=str(unseal), secret_id_file=str(secret_id)
    )
    _authority(settings)

    ward = build(settings)
    broker = StubBroker()
    ward.broker = broker  # type: ignore[assignment]
    try:
        await _unlock(ward)
    finally:
        await ward.store.close()
    assert broker.material == UnlockMaterial(unseal_key="key-1", auth_secret="sid-1")


async def test_without_the_files_it_starts_locked_rather_than_failing(
    tmp_path: Path,
) -> None:
    """A deployment that unlocks by hand is the normal case, not an error: ward
    comes up, refuses every request, and says which state it is in."""
    settings = _settings(tmp_path, unseal_key_file=str(tmp_path / "missing"))
    _authority(settings)

    ward = build(settings)
    broker = StubBroker()
    ward.broker = broker  # type: ignore[assignment]
    try:
        await _unlock(ward)  # must not raise
    finally:
        await ward.store.close()
    assert broker.material is None


async def test_material_that_does_not_open_the_store_does_not_stop_the_process(
    tmp_path: Path,
) -> None:
    """Same reasoning the other way round: the key was there and did not work.
    That is a warning in the log and a locked broker, not a container that
    restarts for ever."""
    key = tmp_path / "unseal"
    key.write_text("wrong-key")
    settings = _settings(tmp_path, unseal_key_file=str(key))
    _authority(settings)

    ward = build(settings)
    ward.broker = StubBroker(opens=False)  # type: ignore[assignment]
    try:
        await _unlock(ward)  # must not raise
    finally:
        await ward.store.close()


# -- what the ceremony hands out --------------------------------------------------


def test_the_operator_identity_lands_where_an_operator_can_reach_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ward init` runs inside the broker's container, and the operator does not.
    Without this the only certificate that may drive the broker would sit in the
    broker's own volume and every administrative command would answer 404."""
    settings = _settings(tmp_path, issued_dir=str(tmp_path / "issued"))
    ca, _ = CertificateAuthority.create()
    operator = ca.issue_client(OPERATOR)

    _hand_out(settings, ca, operator)

    issued = tmp_path / "issued"
    assert (issued / "operator.crt").read_text() == operator.certificate
    assert (issued / "ca.crt").read_text() == ca.certificate
    # The signing key is the one thing that must not travel: this directory is
    # mounted by whatever runs the clients, and a key here would let that side
    # mint an agent.
    assert not (issued / "ca.key").exists()
    assert str(issued) in capsys.readouterr().out


def test_an_unmounted_directory_says_what_to_copy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The material has already been printed by then, so this cannot raise — it
    tells the operator what to do instead."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    settings = _settings(tmp_path, issued_dir=str(blocked / "issued"))
    ca, _ = CertificateAuthority.create()

    _hand_out(settings, ca, ca.issue_client(OPERATOR))

    assert "by hand" in capsys.readouterr().err


# -- signing in -------------------------------------------------------------------


class StubDriver:
    def __init__(self, fails: bool = False) -> None:
        self.calls = 0
        self._fails = fails

    async def login(self) -> dict:
        self.calls += 1
        if self._fails:
            raise RuntimeError("Invalid or expired session, please login again")
        return {"id": "bot-1", "username": "ward"}


async def test_the_chat_account_is_signed_in_before_serving(tmp_path: Path) -> None:
    """A token in the driver's options is not a session. Without this call the
    first request for a credential fails to post its card, and the ledger fills
    with `no_approver` for a broker that looks configured."""
    settings = _settings(tmp_path)
    _authority(settings)
    ward = build(settings)
    driver = StubDriver()
    ward.driver = driver  # type: ignore[assignment]
    try:
        await _sign_in(ward)
    finally:
        await ward.store.close()
    assert driver.calls == 1


async def test_a_chat_account_that_will_not_sign_in_does_not_stop_the_broker(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It can still serve what needs no human, and still be driven by an
    operator — including to find out why."""
    settings = _settings(tmp_path)
    _authority(settings)
    ward = build(settings)
    ward.driver = StubDriver(fails=True)  # type: ignore[assignment]
    try:
        with caplog.at_level("ERROR"):
            await _sign_in(ward)  # must not raise
    finally:
        await ward.store.close()
    assert "WARD_MATTERMOST_TOKEN" in caplog.text
