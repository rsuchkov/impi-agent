#!/usr/bin/env python3
"""Two naming rules from AGENTS.md, checked mechanically.

Both are about words appearing in *text* — a string, a comment, a docstring —
which is why neither import-linter nor ruff can see them. They had been enforced
by review alone, and review had already let a few through.

1. **The library names no application.** `crucible` is reusable; an application's
   name in a message it emits, a file it writes or a path it creates makes it
   quietly not reusable. Enforced against every app package in the workspace, so
   a second app would be covered without editing this file.

2. **The neutral layers name no runtime.** `pi` specifics belong in
   `runtimes/pi/` and the composition root; the neutral layers say "the
   runtime", and that applies to comments and docstrings as much as to imports.
   Which layers those are is listed below rather than inferred — the modules
   that legitimately know the runtime (its driver, the settings boundary whose
   knobs are spelled `pi_*`, the session CLI that deletes its files) are exactly
   the ones a broad "everything else" rule would get wrong.

Run by `make lint`. A finding prints the file, the line and the rule; if a
violation is genuinely too expensive to fix right now, it goes in ALLOWED below
with the reason, where it stays visible instead of being forgotten.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "packages" / "crucible" / "src" / "crucible"

# The layers the runtime rule applies to: the ones whose import contracts in
# pyproject.toml already forbid `crucible.runtimes.pi`. Naming them here rather
# than saying "everything except the driver" keeps the two halves of the rule —
# imports and words — describing the same set.
NEUTRAL = (
    "attachments.py", "builtin_tools.py", "flows", "gateways", "interactions",
    "ports", "profiles", "scheduler", "secrets", "skills", "store", "tools",
)

# Escape hatch for a violation that is genuinely not worth the change it would
# cost: "<path>:<line>" -> the reason a reader deserves. Empty, and worth keeping
# that way — an entry stops the check from failing, so it is a decision to leave
# something broken, not a way to quiet the linter.
ALLOWED: dict[str, str] = {}


def app_names() -> list[str]:
    """The workspace's application packages — everything except the library."""
    names = []
    for pyproject in sorted((ROOT / "packages").glob("*/pyproject.toml")):
        name = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["name"]
        if name != "crucible":
            names.append(name)
    return names


def sources() -> list[Path]:
    return sorted(p for p in LIBRARY.rglob("*.py") if "__pycache__" not in p.parts)


def is_neutral(path: Path) -> bool:
    return path.relative_to(LIBRARY).parts[0] in NEUTRAL


def violations() -> dict[str, str]:
    """Every offending line, as "<path>:<line>" -> what is wrong with it."""
    apps = app_names()
    if not apps:  # a workspace with no app is a bug in this script, not a pass
        raise SystemExit("no application package beside crucible — check_names is misconfigured")
    app_pattern = re.compile("|".join(re.escape(name) for name in apps))
    # Word-boundary, case-sensitive: `pi` the runtime, not "pip", "api" or
    # "mapping". Also catches the possessive, which is how it usually shows up.
    runtime_pattern = re.compile(r"\bpi\b|\bpi's\b")

    found: dict[str, str] = {}
    for path in sources():
        relative = path.relative_to(LIBRARY).as_posix()
        neutral = is_neutral(path)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if app_pattern.search(line):
                found[f"{relative}:{number}"] = (
                    f"the library names an application ({', '.join(apps)}) — "
                    f"crucible must not know what it is running\n    {line.strip()}"
                )
            elif neutral and runtime_pattern.search(line):
                found[f"{relative}:{number}"] = (
                    'a neutral layer names the runtime — say "the runtime" here; '
                    f"`pi` belongs in runtimes/pi\n    {line.strip()}"
                )
    return found


def main() -> int:
    found = violations()
    new = {where: why for where, why in found.items() if where not in ALLOWED}
    # An allowlisted line that no longer offends — because it was fixed, or
    # because the code moved and the entry now shields something else — has
    # stopped meaning anything, and left alone it would re-open the hole it
    # was pointing at.
    stale = sorted(set(ALLOWED) - set(found))

    for where, why in sorted(new.items()):
        print(f"\u2718 {where}: {why}", file=sys.stderr)
    for where in stale:
        print(
            f"\u2718 {where}: allowlisted, but that line does not offend any more "
            "— drop the entry",
            file=sys.stderr,
        )
    if new or stale:
        print(
            f"\n{len(new) + len(stale)} naming violation(s). See AGENTS.md "
            "(Development principles) and scripts/check_names.py.",
            file=sys.stderr,
        )
        return 1
    print(f"check-names OK ({len(ALLOWED)} allowlisted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
