"""The pi lockfile agrees with its manifest and pins every package by hash.

Run by `make pi-lock` after a refresh and by the test suite: a lock that lags
the manifest makes `npm ci` fail at image build, which is late; a lock entry
with no integrity hash is a package npm would install without checking.
"""

import json
import subprocess
import sys
from collections.abc import Callable
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


def fill_integrity(fetch: Callable[[str], str] | None = None) -> list[str]:
    """Add the integrity hash of every lock entry that lacks one, from the
    registry. The runtime's own sibling packages arrive through its
    shrinkwrap without hashes; the registry has them. Returns what was filled."""
    fetch = fetch or _registry_integrity
    path = PI_DIR / "package-lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    filled: list[str] = []
    for key, entry in lock.get("packages", {}).items():
        if key and not entry.get("link") and "integrity" not in entry:
            name = key.split("node_modules/")[-1]
            spec = f"{name}@{entry['version']}"
            integrity = fetch(spec)
            if not integrity.startswith("sha512-"):
                raise RuntimeError(f"{spec}: the registry gave no sha512 integrity ({integrity!r})")
            entry["integrity"] = integrity
            filled.append(spec)
    if filled:
        path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return filled


def _registry_integrity(spec: str) -> str:
    return subprocess.check_output(["npm", "view", spec, "dist.integrity"], text=True).strip()


def main() -> int:
    if "--fill" in sys.argv[1:]:
        for spec in fill_integrity():
            print(f"integrity filled from the registry: {spec}")
    found = problems()
    for line in found:
        print(f"✘ {line}")
    if not found:
        print("pi lock OK")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
