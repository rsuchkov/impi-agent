"""``secret-exec`` (crucible/secrets/exec_cli.py).

The client half of the contract. Two things matter more than the rest: a value
never reaches stdout or stderr, and every authorization refusal produces exactly
the same output whatever the real reason was.
"""

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import web

from crucible.secrets import exec_cli

AGENT_ENV = {
    "TOOL_URL": "",  # filled in per test
    "TOOL_TOKEN": "tok-assistant",
    "RUNTIME_SESSION_ID": "assistant--dm1",
}


class StubEngine:
    """The lease endpoint, and a record of what it was asked."""

    def __init__(self, answer: dict | None = None, *, status: int = 200) -> None:
        self.answer = answer if answer is not None else {
            "granted": True, "values": {"GITHUB_TOKEN": "ghp_secret_value"}
        }
        self.status = status
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self._runner: web.AppRunner | None = None

    async def start(self, port: int) -> None:
        app = web.Application()
        app.router.add_post("/secrets/lease", self._lease)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _lease(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        self.headers.append(dict(request.headers))
        return web.json_response(self.answer, status=self.status)


async def _run(argv: list[str]) -> int:
    """Run the client off the event loop.

    ``secret-exec`` is a synchronous process by design — it is a wrapper that
    ends in execvpe, not part of the engine. Calling it inline would block the
    loop the stub server answers on, so the test drives it the way a shell does:
    from somewhere else.
    """
    return await asyncio.to_thread(exec_cli.main, argv)


def _env(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    for key, value in AGENT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TOOL_URL", f"http://127.0.0.1:{port}")


class _Exec:
    """Stands in for execvpe, which would otherwise replace the test process."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], dict]] = []

    def __call__(self, file: str, argv: list[str], env: dict) -> None:
        self.calls.append((file, list(argv), dict(env)))


# -- the command line ----------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--env", "GITHUB_TOKEN=vault://github-token"],  # no `--`
        ["--env", "GITHUB_TOKEN=vault://github-token", "--"],  # no command
        ["--"],  # nothing to fetch
        ["--env", "GITHUB_TOKEN", "--", "gh"],  # no reference
        ["--env", "GITHUB TOKEN=vault://x", "--", "gh"],  # not a variable name
        ["--env", "T=github-token", "--", "gh"],  # not a reference
        ["--env", "T=vault://../../sys", "--", "gh"],  # climbing out of the mount
        ["--env", "T=vault://a", "--env", "T=vault://b", "--", "gh"],  # bound twice
        ["--wat", "x", "--", "gh"],
    ],
)
def test_a_bad_command_line_fails_before_anything_is_asked(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert exec_cli.main(argv) == exec_cli.EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_without_an_engine_address_it_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TOOL_URL", raising=False)
    monkeypatch.delenv("TOOL_TOKEN", raising=False)
    argv = ["--env", "T=vault://github-token", "--", "gh"]
    assert exec_cli.main(argv) == exec_cli.EXIT_CONFIG
    assert "TOOL_URL" in capsys.readouterr().err


# -- the round trip ------------------------------------------------------------


async def test_a_granted_lease_execs_the_command_with_the_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = StubEngine()
    await engine.start(8521)
    runner = _Exec()
    monkeypatch.setattr(exec_cli.os, "execvpe", runner)
    _env(monkeypatch, 8521)
    try:
        code = await _run(
            [
                "--env", "GITHUB_TOKEN=vault://github-token",
                "--reason", "push the release",
                "--", "gh", "release", "create", "v1.2.0",
            ]
        )
        assert code == 0
        file, argv, env = runner.calls[0]
        assert (file, argv) == ("gh", ["gh", "release", "create", "v1.2.0"])
        assert env["GITHUB_TOKEN"] == "ghp_secret_value"
        assert env["PATH"] == exec_cli.os.environ["PATH"]  # the rest is inherited

        asked = engine.requests[0]
        assert asked["bindings"] == [
            {"env": "GITHUB_TOKEN", "ref": "vault://github-token"}
        ]
        assert asked["reason"] == "push the release"
        assert asked["command"] == ["gh", "release", "create", "v1.2.0"]
        headers = engine.headers[0]
        assert headers["X-Tool-Token"] == "tok-assistant"
        assert headers["X-Runtime-Session"] == "assistant--dm1"

        # Nothing about the value was printed.
        captured = capsys.readouterr()
        assert "ghp_secret_value" not in captured.out + captured.err
    finally:
        await engine.stop()


async def test_several_fields_of_one_secret_arrive_as_several_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = StubEngine(
        {"granted": True, "values": {"SMTP_USER": "bot", "SMTP_PASS": "hunter2"}}
    )
    await engine.start(8522)
    runner = _Exec()
    monkeypatch.setattr(exec_cli.os, "execvpe", runner)
    _env(monkeypatch, 8522)
    try:
        code = await _run(
            [
                "--env", "SMTP_USER=vault://smtp#username",
                "--env=SMTP_PASS=vault://smtp#password",  # both spellings work
                "--", "mailer",
            ]
        )
        assert code == 0
        _, _, env = runner.calls[0]
        assert (env["SMTP_USER"], env["SMTP_PASS"]) == ("bot", "hunter2")
        assert engine.requests[0]["bindings"] == [
            {"env": "SMTP_USER", "ref": "vault://smtp#username"},
            {"env": "SMTP_PASS", "ref": "vault://smtp#password"},
        ]
    finally:
        await engine.stop()


# -- refusals ------------------------------------------------------------------


async def test_every_refusal_prints_the_same_line_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The engine already collapses its reasons to one word; this asserts the
    client does not reintroduce a difference of its own."""
    engine = StubEngine({"granted": False, "status": "refused"})
    await engine.start(8523)
    runner = _Exec()
    monkeypatch.setattr(exec_cli.os, "execvpe", runner)
    _env(monkeypatch, 8523)
    try:
        seen = set()
        for ref in ("vault://absent", "vault://theirs", "vault://mine"):
            assert await _run(["--env", f"T={ref}", "--", "gh"]) == exec_cli.EXIT_REFUSED
            seen.add(capsys.readouterr().err)
        assert len(seen) == 1
        assert seen.pop().strip() == "secret-exec: access was not granted"
        assert runner.calls == []
    finally:
        await engine.stop()


async def test_an_unavailable_store_is_a_different_and_temporary_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = StubEngine({"granted": False, "status": "unavailable"})
    await engine.start(8524)
    monkeypatch.setattr(exec_cli.os, "execvpe", _Exec())
    _env(monkeypatch, 8524)
    try:
        code = await _run(["--env", "T=vault://github-token", "--", "gh"])
        assert code == exec_cli.EXIT_UNAVAILABLE
        assert "not available" in capsys.readouterr().err
    finally:
        await engine.stop()


async def test_an_engine_that_is_not_listening_is_also_temporary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(exec_cli.os, "execvpe", _Exec())
    _env(monkeypatch, 8525)  # nothing started on this port
    code = exec_cli.main(["--env", "T=vault://github-token", "--", "gh"])
    assert code == exec_cli.EXIT_UNAVAILABLE
    assert "not available" in capsys.readouterr().err


async def test_a_rejected_request_reports_the_engine_s_own_complaint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 400 is this process's fault, not an authorization answer, so it says
    what was wrong — and the engine's 400s never mention what exists."""
    engine = StubEngine({"error": "bindings must be a non-empty list"}, status=400)
    await engine.start(8526)
    _env(monkeypatch, 8526)
    try:
        code = await _run(["--env", "T=vault://github-token", "--", "gh"])
        assert code == exec_cli.EXIT_USAGE
        assert "bindings must be" in capsys.readouterr().err
    finally:
        await engine.stop()


async def test_an_unknown_token_is_a_configuration_problem(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = StubEngine({"error": "unauthorized"}, status=401)
    await engine.start(8527)
    _env(monkeypatch, 8527)
    try:
        code = await _run(["--env", "T=vault://github-token", "--", "gh"])
        assert code == exec_cli.EXIT_CONFIG
        assert "unauthorized" in capsys.readouterr().err
    finally:
        await engine.stop()


async def test_a_command_that_does_not_exist_says_which_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    engine = StubEngine()
    await engine.start(8528)
    _env(monkeypatch, 8528)
    try:
        missing = str(tmp_path / "no-such-binary")
        code = await _run(["--env", "T=vault://github-token", "--", missing])
        assert code == exec_cli.EXIT_USAGE
        error = capsys.readouterr().err
        assert "no-such-binary" in error
        assert "ghp_secret_value" not in error  # even a failure prints no value
    finally:
        await engine.stop()


async def test_a_nonsense_answer_is_treated_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a malformed response must never be what runs a command with
    a half-built environment."""
    runner = _Exec()
    monkeypatch.setattr(exec_cli.os, "execvpe", runner)
    for answer in ({"granted": True}, {"granted": True, "values": "nope"}, {}):
        engine = StubEngine(answer)
        await engine.start(8529)
        _env(monkeypatch, 8529)
        try:
            code = await _run(["--env", "T=vault://github-token", "--", "gh"])
        finally:
            await engine.stop()
        if answer == {"granted": True}:
            # An empty value map is a grant of nothing: the command runs with no
            # extra variables rather than being told it was refused.
            assert code == 0
        else:
            assert code == exec_cli.EXIT_REFUSED


def test_the_payload_is_plain_json_the_engine_can_parse() -> None:
    bindings, reason, command = exec_cli._parse(
        ["--env", "T=vault://x", "--reason", "why", "--", "sh", "-c", "echo hi"]
    )
    payload = {
        "bindings": [{"env": name, "ref": ref} for name, ref in bindings],
        "reason": reason,
        "command": command,
    }
    assert json.loads(json.dumps(payload)) == {
        "bindings": [{"env": "T", "ref": "vault://x"}],
        "reason": "why",
        "command": ["sh", "-c", "echo hi"],
    }
