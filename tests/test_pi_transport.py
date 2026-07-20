import logging
from unittest.mock import AsyncMock, patch

from crucible.runtimes.pi.transport import SubprocessTransport, _stderr_log_level


def test_settings_lock_noise_is_debug() -> None:
    # pi's advisory settings lock fails harmlessly on read-only profile dirs.
    line = (
        "Warning: (runtime creation, project settings) EROFS: read-only file "
        "system, mkdir '/agents/assistant/.pi/settings.json.lock'"
    )
    assert _stderr_log_level(line) == logging.DEBUG


def test_other_stderr_stays_warning() -> None:
    assert _stderr_log_level("TypeError: cannot read properties of undefined") == logging.WARNING
    # A different EROFS (not the settings lock) must still warn — could be real.
    assert _stderr_log_level("EROFS: read-only file system, open '/data/x'") == logging.WARNING


async def test_spawn_sets_large_stream_limit() -> None:
    # pi JSON event lines can exceed asyncio's 64 KiB default; spawn must raise
    # the StreamReader limit, otherwise readline() crashes with LimitOverrunError.
    fake_proc = type("P", (), {"stderr": None})()
    with patch(
        "crucible.runtimes.pi.transport.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=fake_proc),
    ) as mock_exec:
        await SubprocessTransport.spawn("pi", ["--mode", "rpc"])

    assert mock_exec.await_args is not None
    limit = mock_exec.await_args.kwargs["limit"]
    assert limit == SubprocessTransport._STREAM_LIMIT
    assert limit >= 1024 * 1024  # comfortably above the 64 KiB default
