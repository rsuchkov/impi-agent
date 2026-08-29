"""The host's command line. One command: run.

Everything else it needs is in the environment, because everything else it needs
is a mount, and mounts are the deployment's business rather than an operator's
to retype.
"""

import argparse
import asyncio
import logging
import sys

from runtime_relay.config import ConfigError, from_env
from runtime_relay.server import RelayServer

EXIT_USAGE = 64  # sysexits.h: the command line was wrong
EXIT_CONFIG = 78  # sysexits.h: this process was not told what it is


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="runtime-relay",
        description="Run one agent's runtime on request from the engine.",
    )
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="listen for the engine and run the runtime")
    serve.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.print_help()
        return EXIT_USAGE

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = from_env()
    except ConfigError as exc:
        print(f"runtime-relay: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        asyncio.run(RelayServer(config).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
