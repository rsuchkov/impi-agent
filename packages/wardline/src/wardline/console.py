"""The terminal half of ``ward-admin``: colour, prompts, durations, timestamps.

Deliberately a copy of the engine CLI's helpers rather than an import of them.
Sixty lines of ANSI and a duration formatter are not worth a dependency from
this package on the engine — the whole reason it exists is that the two know
nothing about each other. If they drift, nothing breaks but the shade of a tick.

Stdlib only, like the CLI it serves.
"""

import getpass
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CommandError(Exception):
    """Something the operator can fix, printed without a traceback."""


# -- colour --------------------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _sgr(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def bold(text: str) -> str:
    return _sgr("1", text)


def dim(text: str) -> str:
    return _sgr("2", text)


def ok(text: str) -> None:
    print(_sgr("32", "✔ ") + text)


def fail(text: str) -> None:
    print(_sgr("31", "✘ ") + text, file=sys.stderr)


# -- asking --------------------------------------------------------------------


def prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = dim(f" [{default}]") if default else ""
    while True:
        if secret:
            value = getpass.getpass(f"{bold(label)}: ")
        else:
            value = input(f"{bold(label)}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print(dim("  (a value is required)"))


def confirm(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{bold(question)} {dim(hint)} ").strip().lower()
    except EOFError:
        # Nobody is there to answer (a pipe, a container with no TTY). Refusing
        # is the safe reading of silence; --yes is how a script says yes.
        print(dim("  (nothing to read the answer from — pass --yes)"))
        return False
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


# -- durations and moments -------------------------------------------------------


def humanize(seconds: int) -> str:
    """A duration as a human would say it — the same words the approval card
    uses, so a policy and the card it produces read alike."""
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds}s"


def parse_duration(text: str) -> int:
    """``15m`` / ``1h`` / ``0`` -> seconds. The unit is required above zero,
    because a bare number is exactly the kind of thing that means minutes to one
    person and seconds to another."""
    raw = text.strip().lower()
    if raw in ("0", "none", "never"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(raw) > 1 and raw[-1] in units and raw[:-1].isdigit():
        return int(raw[:-1]) * units[raw[-1]]
    raise CommandError(f"not a duration: {text!r} (try 15m, 1h, or 0)")


def local_time(raw: str) -> str:
    """A stored timestamp as the reader would say it, in the zone ``TZ`` names.

    The ledger stores UTC. Which zone to show it in is the reader's business,
    and ``TZ`` is where a process is told that — this tool has no config file to
    put it in. The zone is printed alongside, so a bare time is never ambiguous.
    """
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw  # not ours to interpret; show what the broker said
    moment = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    name = os.environ.get("TZ", "").strip() or "UTC"
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        name, zone = "UTC", ZoneInfo("UTC")
    return f"{moment.astimezone(zone):%Y-%m-%d %H:%M} ({name})"
