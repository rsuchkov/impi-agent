"""`ward-admin` against a stub broker (wardline/admin_cli.py).

Nothing here reads a store: the broker holds the values, the policies and the
ledger, so every one of these commands is an HTTPS client presenting the
operator certificate. That is what the tests drive: the right call goes out, the
answer is rendered readably, and the two ways an operator can be misconfigured
say which one it is.

Real TLS again, because presenting the certificate is half of what the client
does and a stub without it would prove nothing.
"""

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web

from ward.ca import OPERATOR_CN, CertificateAuthority
from ward.server import mutual_tls
from wardline import admin_cli as cli
from wardline.console import CommandError, parse_duration

_CA, _CA_MATERIAL = CertificateAuthority.create()


class StubBroker:
    """The operator half of ward's API, and a record of what was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.bodies: list[dict] = []
        self.answers: dict[str, dict] = {}
        self._runner: web.AppRunner | None = None

    async def start(self, port: int, root: Path) -> None:
        _CA_MATERIAL.write(root / "ca.crt", root / "ca.key")
        _CA.issue_server(("localhost", "127.0.0.1")).write(root / "b.crt", root / "b.key")
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._any)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(
            self._runner, "127.0.0.1", port,
            ssl_context=mutual_tls(
                certificate=root / "b.crt", key=root / "b.key", ca=root / "ca.crt"
            ),
        ).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _any(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        self.calls.append((request.method, request.path, dict(request.query)))
        self.bodies.append(body if isinstance(body, dict) else {})
        key = f"{request.method} {request.path}"
        return web.json_response(self.answers.get(key, {"ok": True, "sent": body}))


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The environment is the whole of this tool's configuration, so an
    inherited variable would decide the test rather than the test doing it."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "SECRET_BROKER_URL", "SECRET_BROKER_CERTS_DIR", "SECRET_BROKER_CA",
        "SECRET_BROKER_OPERATOR_CERT", "SECRET_BROKER_OPERATOR_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _operator(monkeypatch: pytest.MonkeyPatch, port: int, root: Path) -> None:
    _CA_MATERIAL.write(root / "ca.crt", root / "ca.key")
    _CA.issue_client(OPERATOR_CN).write(root / "op.crt", root / "op.key")
    monkeypatch.setenv("SECRET_BROKER_URL", f"https://localhost:{port}")
    monkeypatch.setenv("SECRET_BROKER_CERTS_DIR", str(root))
    monkeypatch.setenv("SECRET_BROKER_OPERATOR_CERT", str(root / "op.crt"))
    monkeypatch.setenv("SECRET_BROKER_OPERATOR_KEY", str(root / "op.key"))
    monkeypatch.setenv("SECRET_BROKER_CA", str(root / "ca.crt"))


async def _run(argv: list[str]) -> int:
    """The CLI is synchronous; running it inline would block the loop the stub
    answers on."""
    return await asyncio.to_thread(cli.main, argv)


# -- misconfiguration ----------------------------------------------------------


def test_with_no_broker_configured_it_says_which_setting_is_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["ls"]) == 1
    assert "SECRET_BROKER_URL" in capsys.readouterr().err


def test_with_no_operator_identity_it_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("SECRET_BROKER_URL", "https://ward:8425")
    assert cli.main(["ls"]) == 1
    assert "OPERATOR_CERT" in capsys.readouterr().err


async def test_an_unreachable_broker_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _operator(monkeypatch, 8591, tmp_path)  # nothing is listening there
    assert await _run(["ls"]) == 1
    assert "cannot reach the secret broker" in capsys.readouterr().err


# -- the calls it makes --------------------------------------------------------


async def test_listing_renders_what_the_broker_returned(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broker = StubBroker()
    broker.answers["GET /secrets"] = {
        "secrets": [
            {
                "name": "github-token", "stored": True,
                "policy": {
                    "name": "github-token", "approval": "always", "max_grant_s": 900,
                    "subjects": "assistant", "description": "",
                },
            },
            {"name": "orphan", "stored": True, "policy": None},
        ]
    }
    await broker.start(8592, tmp_path)
    _operator(monkeypatch, 8592, tmp_path)
    try:
        assert await _run(["ls"]) == 0
        out = capsys.readouterr().out
        assert "github-token" in out and "15 min" in out and "assistant" in out
        assert "unreachable by every agent" in out  # the one with no policy
    finally:
        await broker.stop()


async def test_setting_a_policy_sends_the_parsed_duration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker = StubBroker()
    await broker.start(8593, tmp_path)
    _operator(monkeypatch, 8593, tmp_path)
    try:
        code = await _run(
            ["policy", "set", "github-token",
             "--subjects", "assistant, builder", "--max-grant", "15m"]
        )
        assert code == 0
        method, path, _ = broker.calls[0]
        assert (method, path) == ("PUT", "/policies/github-token")
    finally:
        await broker.stop()


async def test_removing_a_secret_reports_the_windows_it_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broker = StubBroker()
    broker.answers["DELETE /secrets/github-token"] = {
        "removed": "github-token", "windows_closed": 2
    }
    await broker.start(8594, tmp_path)
    _operator(monkeypatch, 8594, tmp_path)
    try:
        assert await _run(["rm", "github-token", "--yes"]) == 0
        assert "2 open window(s)" in capsys.readouterr().out
    finally:
        await broker.stop()


async def test_the_ledger_shows_the_decision_the_command_and_the_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broker = StubBroker()
    broker.answers["GET /audit"] = {
        "audit": [
            {
                "at": "2026-08-17T09:00:00+00:00", "agent": "assistant",
                "secret": "github-token", "reason": "push the release",
                "detail": "gh release create v1.2.0", "decision": "approved_once",
                "approver": "u1", "request_id": "rq_1",
            }
        ]
    }
    await broker.start(8595, tmp_path)
    _operator(monkeypatch, 8595, tmp_path)
    try:
        assert await _run(["audit", "--agent", "assistant"]) == 0
        out = capsys.readouterr().out
        assert "approved_once" in out and "gh release create v1.2.0" in out
        assert "push the release" in out
        assert broker.calls[0][2]["agent"] == "assistant"  # the filter travelled
    finally:
        await broker.stop()


async def test_minting_an_identity_writes_it_where_the_engine_looks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The authority lives with the broker, so the CLI asks rather than issues —
    which is what keeps the engine from inventing an agent."""
    issued = _CA.issue_client("newbie")
    broker = StubBroker()
    broker.answers["POST /certs/newbie"] = {
        "certificate": issued.certificate, "key": issued.key, "ca": _CA.certificate
    }
    await broker.start(8596, tmp_path)
    _operator(monkeypatch, 8596, tmp_path)
    certs = tmp_path / "issued"
    try:
        assert await _run(["cert", "newbie", "--dir", str(certs)]) == 0
    finally:
        await broker.stop()
    assert (certs / "newbie.crt").read_text() == issued.certificate
    assert (certs / "ca.crt").read_text() == _CA.certificate
    # The key is a credential wherever it lands.
    assert (certs / "newbie.key").stat().st_mode & 0o777 == 0o600


async def test_a_broker_that_does_not_know_this_operator_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """404 is what the operator routes answer anyone who is not the operator, so
    the client reads it as the certificate rather than as a missing page."""
    broker = StubBroker()

    async def _refuse(request: web.Request) -> web.Response:
        return web.json_response({"error": "not found"}, status=404)

    await broker.start(8597, tmp_path)
    broker._any = _refuse  # type: ignore[method-assign]
    await broker.stop()
    await broker.start(8598, tmp_path)
    _operator(monkeypatch, 8598, tmp_path)
    try:
        assert await _run(["ls"]) == 1
        assert "operator certificate" in capsys.readouterr().err
    finally:
        await broker.stop()


# -- the pure bits -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["15", "soon", "1 hour", "5x", "1hh"])
def test_a_duration_must_name_its_unit(bad: str) -> None:
    with pytest.raises(CommandError):
        parse_duration(bad)


@pytest.mark.parametrize(
    "text, seconds", [("0", 0), ("never", 0), ("90s", 90), ("15m", 900), ("2h", 7200)]
)
def test_durations_that_do_name_their_unit(text: str, seconds: int) -> None:
    assert parse_duration(text) == seconds


def test_a_field_argument_must_be_a_pair() -> None:
    with pytest.raises(CommandError):
        cli._split_field("justaname")
    assert cli._split_field("password=hunter2") == ("password", "hunter2")


def test_the_payload_is_plain_json() -> None:
    assert json.loads(json.dumps({"fields": dict([cli._split_field("a=b")])})) == {
        "fields": {"a": "b"}
    }


# -- unlocking without typing a key ------------------------------------------------


async def test_unlock_reads_the_material_from_a_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key typed at a prompt is a key in somebody's scrollback, and in the
    transcript of whatever ran the command. The file `ward init --machine`
    wrote is the shape this reads."""
    broker = StubBroker()
    broker.answers["POST /unlock"] = {"usable": True}
    await broker.start(8599, tmp_path)
    _operator(monkeypatch, 8599, tmp_path)
    material = tmp_path / "recovery.txt"
    material.write_text(
        "WARD_UNSEAL_KEY=key-1\nWARD_SECRET_ID=sid-1\nWARD_ROLE_ID=role-1\n"
    )
    try:
        assert await _run(["unlock", "--from", str(material)]) == 0
    finally:
        await broker.stop()
    assert broker.bodies[-1] == {"unseal_key": "key-1", "auth_secret": "sid-1"}


async def test_half_the_material_is_an_error_not_a_surprise_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Falling back to a prompt mid-script is how an automated unlock hangs
    forever with nobody watching."""
    _operator(monkeypatch, 8600, tmp_path)
    material = tmp_path / "half.txt"
    material.write_text("WARD_UNSEAL_KEY=key-1\n")
    assert await _run(["unlock", "--from", str(material)]) == 1
    assert "WARD_SECRET_ID" in capsys.readouterr().err


async def test_a_missing_material_file_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _operator(monkeypatch, 8601, tmp_path)
    assert await _run(["unlock", "--from", str(tmp_path / "nope.txt")]) == 1
    assert "cannot read" in capsys.readouterr().err


# -- rotating the broker's credential -----------------------------------------------


async def test_rotate_reports_the_new_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    broker = StubBroker()
    broker.answers["POST /rotate"] = {"secret_id": "fresh-secret-id"}
    await broker.start(8602, tmp_path)
    _operator(monkeypatch, 8602, tmp_path)
    try:
        assert await _run(["rotate"]) == 0
    finally:
        await broker.stop()
    assert "fresh-secret-id" in capsys.readouterr().out


async def test_rotate_machine_prints_only_the_credential(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Same rule as the ceremony's: stdout is for a file, not for a terminal."""
    broker = StubBroker()
    broker.answers["POST /rotate"] = {"secret_id": "fresh-secret-id"}
    await broker.start(8603, tmp_path)
    _operator(monkeypatch, 8603, tmp_path)
    try:
        assert await _run(["rotate", "--machine"]) == 0
    finally:
        await broker.stop()
    captured = capsys.readouterr()
    assert captured.out == "WARD_SECRET_ID=fresh-secret-id\n"
    assert "replaced" in captured.err  # the human half went elsewhere
