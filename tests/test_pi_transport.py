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


def test_a_new_conversations_session_notice_is_debug() -> None:
    # Every conversation's first turn: pi creates the session it was asked for.
    line = (
        "Warning: No project session found with id 'c0ffee'; "
        "creating a new session with that id."
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


async def test_exit_detail_carries_exit_code_and_stderr_tail() -> None:
    import sys

    code = "import sys; print('boom: real cause', file=sys.stderr); sys.exit(3)"
    transport = await SubprocessTransport.spawn(sys.executable, ["-c", code])
    async for _ in transport.lines():  # drain stdout to EOF
        pass
    detail = await transport.exit_detail()
    assert "exit code 3" in detail
    assert "boom: real cause" in detail
    await transport.aclose()


async def test_exit_detail_excludes_settings_lock_noise() -> None:
    import sys

    code = (
        "import sys;"
        "print('EROFS mkdir /x/.pi/settings.json.lock', file=sys.stderr);"
        "sys.exit(1)"
    )
    transport = await SubprocessTransport.spawn(sys.executable, ["-c", code])
    async for _ in transport.lines():
        pass
    detail = await transport.exit_detail()
    assert "exit code 1" in detail
    assert "settings.json.lock" not in detail  # noise stays out of the tail
    await transport.aclose()


async def test_exit_detail_clips_long_tails() -> None:
    import sys

    code = "import sys\nfor i in range(200): print('e' * 100, file=sys.stderr)\nsys.exit(1)"
    transport = await SubprocessTransport.spawn(sys.executable, ["-c", code])
    async for _ in transport.lines():
        pass
    detail = await transport.exit_detail()
    assert len(detail) < 2000  # bounded: 40-line deque + char clip
    await transport.aclose()


def test_a_move_to_a_fallback_model_is_info() -> None:
    # Worth seeing, and the turn goes on: not a failure.
    line = "fallback: dead/m1 failed (Connection error.); switching to openai-codex/gpt-5.5"
    assert _stderr_log_level(line) == logging.INFO
    # Only as the extension's own line, not wherever the word appears.
    assert _stderr_log_level("Error: no fallback: available") == logging.WARNING


def test_the_relay_reads_the_runtimes_stderr_the_same_way() -> None:
    from runtime_relay.server import _stderr_level

    for line in (
        "fallback: dead/m1 failed (Connection error.); switching to b/y",
        "Warning: No project session found with id 'x'; creating a new session with that id.",
        "Warning: EROFS mkdir '/agents/a/.pi/settings.json.lock'",
        "TypeError: undefined",
    ):
        assert _stderr_level(line) == _stderr_log_level(line), line


async def test_the_relay_keeps_only_what_went_wrong_in_the_stderr_tail() -> None:
    """The tail travels back with an exit as its cause: a switch to a fallback
    or a new conversation's notice is not one."""
    import asyncio
    import collections
    from types import SimpleNamespace

    from runtime_relay.server import RelayServer

    stderr = asyncio.StreamReader()
    for line in (
        "fallback: dead/m1 failed (Connection error.); switching to b/y",
        "Warning: No project session found with id 'x'; creating a new session with that id.",
        "TypeError: undefined is not a function",
    ):
        stderr.feed_data((line + "\n").encode())
    stderr.feed_eof()
    tail: collections.deque[str] = collections.deque(maxlen=20)
    await RelayServer._drain_stderr(None, SimpleNamespace(stderr=stderr), tail)  # type: ignore[arg-type]
    assert list(tail) == ["TypeError: undefined is not a function"]

