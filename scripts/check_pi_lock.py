"""The pi lockfile agrees with its manifest and pins every package by hash.

Run by `make pi-lock` after a refresh and by the test suite: a lock that lags
the manifest makes `npm ci` fail at image build, which is late; a lock entry
with no integrity hash is a package npm would install without checking.
"""

import json
import sys
from pathlib import Path

PI_DIR = Path(__file__).resolve().parent.parent / "deploy" / "pi"


def problems() -> list[str]:
    manifest = json.loads((PI_DIR / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((PI_DIR / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages", {})
    found: list[str] = []
    for name, wanted in manifest["dependencies"].items():
        entry = packages.get(f"node_modules/{name}")
        if entry is None:
            found.append(f"{name} is in package.json but not in the lock — run `make pi-lock`")
        elif entry.get("version") != wanted:
            found.append(
                f"{name}: package.json pins {wanted}, the lock has {entry.get('version')} "
                "— run `make pi-lock`"
            )
    for key, entry in packages.items():
        if key and not entry.get("link") and "integrity" not in entry:
            found.append(f"{key} has no integrity hash in the lock")
    return found


def main() -> int:
    found = problems()
    for line in found:
        print(f"✘ {line}")
    if not found:
        print("pi lock OK")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
