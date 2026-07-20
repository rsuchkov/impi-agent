"""Integration smoke test of the real subprocess transport plumbing."""

import sys
from pathlib import Path

from crucible.runtimes.pi.session import PiRpcSession
from crucible.runtimes.pi.transport import SubprocessTransport

FAKE_PI = str(Path(__file__).parent / "fakes" / "fake_pi.py")


async def test_real_subprocess_round_trip() -> None:
    transport = await SubprocessTransport.spawn(sys.executable, [FAKE_PI])
    session = PiRpcSession(transport)
    session.start()
    try:
        result = await session.prompt("ping", timeout=5.0)
        assert result.text == "echo: ping"
    finally:
        await session.close()
