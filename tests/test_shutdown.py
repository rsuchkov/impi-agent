"""How the engine goes down (impi/app.py, impi/main.py).

SIGTERM is what `make stop`, a container runtime and Kubernetes send. It has to
end the main task and nothing else, or the cleanup that means to wait for the
turns in flight finds them already cancelled.
"""

import asyncio
import logging
import os
import signal

import pytest

from impi.app import stop_on_sigterm


async def test_sigterm_ends_the_main_task_and_leaves_the_others_to_the_cleanup() -> None:
    worker_alive_at_cleanup = None
    cleanup_could_wait = False

    async def worker() -> None:
        await asyncio.sleep(10)

    async def main() -> None:
        nonlocal worker_alive_at_cleanup, cleanup_could_wait
        running = asyncio.create_task(worker())
        stop_on_sigterm()
        try:
            await asyncio.sleep(10)
        finally:
            worker_alive_at_cleanup = not running.done()
            await asyncio.sleep(0)  # the cleanup itself can still await
            cleanup_could_wait = True
            running.cancel()

    task = asyncio.create_task(main())
    await asyncio.sleep(0.01)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
    assert worker_alive_at_cleanup is True and cleanup_could_wait is True


def test_the_engine_starts_with_a_umask_that_keeps_its_files_to_itself(monkeypatch) -> None:
    """Whatever the engine and the runtimes it starts create is theirs alone
    unless something chooses otherwise."""
    import os
    from types import SimpleNamespace

    import impi.main as entry

    async def no_run(settings) -> None:
        return None

    monkeypatch.setattr(entry, "load_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(entry, "run", no_run)
    from crucible.redaction import ScrubbingFilter

    before = os.umask(0o022)
    root = logging.getLogger()
    try:
        entry.main()
        assert os.umask(0o022) == 0o077
        # And what it logs is scrubbed before it is written.
        assert root.handlers and all(
            any(isinstance(f, ScrubbingFilter) for f in handler.filters) for handler in root.handlers
        )
    finally:
        os.umask(before)
        for handler in root.handlers:
            for scrubber in [f for f in handler.filters if isinstance(f, ScrubbingFilter)]:
                handler.removeFilter(scrubber)
