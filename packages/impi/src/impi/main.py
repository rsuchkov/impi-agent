"""Entrypoint: settings + logging + the event loop. All wiring lives in app.py."""

from __future__ import annotations

import asyncio
import logging
import os

from crucible.redaction import ScrubbingFilter
from impi.app import run
from impi.config import load_settings

logger = logging.getLogger(__name__)


def main() -> None:
    # Whatever this process and the runtimes it starts create is theirs alone
    # unless something chooses otherwise: profiles an agent scaffolds, files a
    # skill writes, a session's memory. Every mount this deployment shares is
    # shared between processes of ONE user, so nothing is lost by it.
    os.umask(0o077)
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # What a line may carry by accident — a header, an environment, a URL
    # with a token in it — goes before it is written.
    for handler in logging.getLogger().handlers:
        handler.addFilter(ScrubbingFilter())
    # Ctrl+C and SIGTERM (`make stop`, a container runtime) both end run()'s
    # main task — see `stop_on_sigterm` in app.py — so its finally closes the
    # runtime and terminates every pi subprocess instead of orphaning them (the
    # CPU-runaway lesson), with the turns in flight given their drain time.
    try:
        asyncio.run(run(settings))
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("bye")


if __name__ == "__main__":
    main()
