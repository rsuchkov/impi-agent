"""Entrypoint: settings + logging + the event loop. All wiring lives in app.py."""

from __future__ import annotations

import asyncio
import logging
import signal

from impi.app import run
from impi.config import load_settings

logger = logging.getLogger(__name__)


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # `make stop` sends SIGTERM; route it through the same graceful shutdown as
    # Ctrl+C so run()'s finally closes the runtime and terminates every pi
    # subprocess instead of orphaning them (the CPU-runaway lesson).
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        logger.info("bye")


if __name__ == "__main__":
    main()
