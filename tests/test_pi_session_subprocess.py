"""Integration smoke test of the real subprocess transport plumbing."""

import sys
from pathlib import Path

import pytest

from crucible.runtimes.pi.errors import PiProcessError
from crucible.runtimes.pi.session import PiRpcSession
from crucible.runtimes.pi.transport import SubprocessTransport

FAKE_PI = str(Path(__file__).parent / "fakes" / "fake_pi.py")
DYING_PI = str(Path(__file__).parent / "fakes" / "dying_pi.py")


async def test_real_subprocess_round_trip() -> None:
    transport = await SubprocessTransport.spawn(sys.executable, [FAKE_PI])
    session = PiRpcSession(transport)
    session.start()
    try:
        result = await session.prompt("ping", timeout=5.0)
        assert result.text == "echo: ping"
    finally:
        await session.close()


async def test_process_death_error_carries_exit_code_and_stderr() -> None:
    # A config-level pi failure prints the cause to stderr and dies; the turn
    # error must surface it — an opaque "exited unexpectedly" is undebuggable.
    transport = await SubprocessTransport.spawn(sys.executable, [DYING_PI])
    session = PiRpcSession(transport)
    session.start()
    try:
        with pytest.raises(PiProcessError) as excinfo:
            await session.prompt("ping", timeout=5.0)
        message = str(excinfo.value)
        assert "exit code 7" in message
        assert "Invalid URL: $LLM_BASE_URL" in message
    finally:
        await session.close()
