"""AuthRefresher: keeps the runtime's OAuth login fresh from a process that may
write the credential file.

A confined runtime process may read the credential file and nothing more: a
process that could rewrite it could plant a value the runtime runs as a
command (``auth_file.py``), and every other process reading the file — the
runtime re-reads it before each model request — would run it. But a login
expires, and the runtime refreshes an expiring one by writing the file. So the
application refreshes it instead, ahead of time: every ``interval_s`` it has
the runtime's own code refresh any login that expires within ``margin_s`` —
wider than the runtime's own margin, so a confined process always finds a
login it can use as it is.
"""

import asyncio
import json
import logging
import shutil
from pathlib import Path

from crucible.runtimes.pi.auth_file import auth_file_path, executable_entries

logger = logging.getLogger(__name__)

REFRESH_SCRIPT = Path(__file__).with_name("refresh_auth.mjs")
# The runtime refreshes a login that expires within five minutes of use; this
# refreshes it an hour ahead, every quarter of an hour.
DEFAULT_INTERVAL_S = 900.0
DEFAULT_MARGIN_S = 3600.0
_SCRIPT_TIMEOUT_S = 60.0


def runtime_package_dir(program: str) -> Path | None:
    """The installed runtime package the ``program`` on PATH runs: the first
    directory above its real entry point that holds a ``package.json``."""
    found = shutil.which(program)
    if not found:
        return None
    return next((p for p in Path(found).resolve().parents if (p / "package.json").is_file()), None)


def oauth_providers(path: Path) -> list[str]:
    """The providers the credential file holds an OAuth login for."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    return [name for name, entry in data.items() if isinstance(entry, dict) and entry.get("type") == "oauth"]


class AuthRefresher:
    def __init__(
        self,
        *,
        program: str = "pi",
        node: str = "node",
        interval_s: float = DEFAULT_INTERVAL_S,
        margin_s: float = DEFAULT_MARGIN_S,
    ) -> None:
        self._program = program
        self._node = node
        self._interval_s = interval_s
        self._margin_s = margin_s

    async def run(self) -> None:
        while True:
            await self.refresh_once()
            await asyncio.sleep(self._interval_s)

    async def refresh_once(self) -> bool:
        """Refresh what is about to expire. False when something stood in the
        way — said in the log; a login that cannot be refreshed here is one the
        runtime will fail to use once it expires."""
        path = auth_file_path()
        if not oauth_providers(path):
            return True  # nothing that expires: an API key, or no login yet
        planted = executable_entries(path)
        if planted:
            # Resolving the file would run them. The runtime refuses to start
            # over the same file (hosts/local.py); a fresh login replaces it.
            logger.error("not refreshing the runtime's login: %s holds a command value (%s)",
                         path, ", ".join(planted))
            return False
        package = runtime_package_dir(self._program)
        if package is None:
            logger.warning("not refreshing the runtime's login: %s is not installed where it can be found",
                           self._program)
            return False
        process = await asyncio.create_subprocess_exec(
            self._node, str(REFRESH_SCRIPT), str(package), str(path), str(int(self._margin_s * 1000)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(process.communicate(), _SCRIPT_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("refreshing the runtime's login took over %.0fs; trying again later", _SCRIPT_TIMEOUT_S)
            return False
        if process.returncode != 0:
            detail = (err or out).decode("utf-8", errors="replace").strip()[-500:]
            logger.warning("refreshing the runtime's login failed: %s", detail or f"exit {process.returncode}")
            return False
        logger.debug("runtime login: %s", out.decode("utf-8", errors="replace").strip())
        return True
