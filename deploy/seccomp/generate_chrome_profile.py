#!/usr/bin/env python3
"""Derive docker/seccomp/chrome.json from Docker's upstream default profile.

Chrome's namespace sandbox calls clone(2) with CLONE_NEWUSER, which Docker's
default seccomp profile denies. The usual advice is `--no-sandbox`, which turns
off renderer isolation — precisely the boundary that matters when the renderer
is parsing untrusted HTML from a login page. This instead relaxes exactly the
four namespace flags Chrome needs and leaves everything else as upstream has it.

Regenerate after a Docker upgrade so the profile does not drift from upstream:

    python3 docker/seccomp/generate_chrome_profile.py
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

UPSTREAM = "https://raw.githubusercontent.com/moby/profiles/main/seccomp/default.json"

CLONE_NEWNS = 0x00020000
CLONE_NEWCGROUP = 0x02000000
CLONE_NEWUTS = 0x04000000
CLONE_NEWIPC = 0x08000000
CLONE_NEWUSER = 0x10000000
CLONE_NEWPID = 0x20000000
CLONE_NEWNET = 0x40000000

# Docker's mask covering every CLONE_NEW* flag. The rule reads
# "(arg & MASK) == 0", i.e. allow clone only when no namespace is requested.
UPSTREAM_MASK = (
    CLONE_NEWNS
    | CLONE_NEWCGROUP
    | CLONE_NEWUTS
    | CLONE_NEWIPC
    | CLONE_NEWUSER
    | CLONE_NEWPID
    | CLONE_NEWNET
)

# What Chrome's sandbox actually needs. Dropping these from the mask permits
# them; CGROUP/UTS/IPC stay denied, so this is tighter than the chrome.json
# profile that circulates online (which drops the argument filter entirely).
CHROME_NEEDS = CLONE_NEWNS | CLONE_NEWUSER | CLONE_NEWPID | CLONE_NEWNET
RELAXED_MASK = UPSTREAM_MASK & ~CHROME_NEEDS


def patch(profile: dict) -> dict:
    """Relax the clone mask and allow a matching unshare. Returns the profile."""
    patched_clone = 0
    for rule in profile["syscalls"]:
        if "clone" not in rule.get("names", []):
            continue
        for arg in rule.get("args") or []:
            if arg.get("op") == "SCMP_CMP_MASKED_EQ" and arg.get("value") == UPSTREAM_MASK:
                arg["value"] = RELAXED_MASK
                patched_clone += 1

    # Both the mainstream (arg 0) and s390 (arg 1) variants must be patched, or
    # the profile silently keeps blocking Chrome on one of the two.
    if patched_clone != 2:
        raise SystemExit(
            f"expected 2 clone rules with mask {UPSTREAM_MASK}, patched {patched_clone}. "
            "Upstream layout changed — re-read the profile before trusting this script."
        )

    # unshare is only allowed with CAP_SYS_ADMIN upstream; Chrome uses it on some
    # sandbox paths. Same flag restriction as clone above.
    profile["syscalls"].append(
        {
            "names": ["unshare"],
            "action": "SCMP_ACT_ALLOW",
            "args": [{"index": 0, "value": RELAXED_MASK, "op": "SCMP_CMP_MASKED_EQ"}],
            "excludes": {"caps": ["CAP_SYS_ADMIN"]},
        }
    )
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=UPSTREAM, help="upstream default.json URL or path")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).parent / "chrome.json",
        help="where to write the derived profile",
    )
    args = parser.parse_args()

    if args.source.startswith(("http://", "https://")):
        with urllib.request.urlopen(args.source, timeout=30) as response:
            profile = json.load(response)
    else:
        profile = json.loads(Path(args.source).read_text())

    args.output.write_text(json.dumps(patch(profile), indent=2) + "\n")
    print(
        f"wrote {args.output} (clone mask 0x{UPSTREAM_MASK:08X} -> 0x{RELAXED_MASK:08X})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
