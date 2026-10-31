"""What a runtime process this host starts may touch, beyond what the engine
names, and whether the kernel can hold it to that.

The same rules the engine's local host applies (``crucible.runtimes.pi.hosts.
local``), kept here because this package does not depend on the library; a
test in the workspace holds the two to each other. The rules are applied by
the library's own launcher, copied into the image.
"""

import importlib.util
import logging
import shutil
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# What a confined process reads wherever its agent's files are: the system,
# the device nodes, the process table (another process's private entries stay
# out of reach — the kernel refuses them outside the domain).
SYSTEM_READABLE = ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt", "/proc", "/dev")
# Device nodes a shell writes to: discarding output, a terminal, randomness.
DEVICE_WRITABLE = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/ptmx")
# The launcher's options, in the order its rules are given.
READABLE, WRITABLE, WRITABLE_FILE, MKDIR_ONLY = "--readable", "--writable", "--writable-file", "--mkdir-only"
CONNECT_TCP = "--connect-tcp"
# The services a process here reaches for the engine, by the variable that
# names each: the engine's tool server (the bridge calls it) and the secret
# broker (``secret-exec`` calls it). Their ports join the ones the engine
# named whenever the network is restricted.
SERVICE_URL_VARIABLES = ("TOOL_URL", "SECRET_BROKER_URL")


def url_port(url: str) -> int | None:
    """The TCP port a URL is reached on — the one it names, else its scheme's
    — or None for anything else."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if port is not None:
        return port
    return {"https": 443, "http": 80}.get(parts.scheme)


def install_roots(program: str) -> list[Path]:
    """Where the runtime and node live when that is outside the system paths:
    the package tree the program resolves into, and node's prefix."""
    roots: list[Path] = []
    for name in (program, "node"):
        found = shutil.which(name)
        if not found:
            continue
        real = Path(found).resolve()
        package_root = next((p.parent for p in real.parents if p.name == "node_modules"), real.parent)
        roots += [package_root, real.parent.parent]
    return roots


def landlock_abi(launcher: Path) -> int | None:
    """The kernel's Landlock version as the launcher reads it — the check that
    will run — or None when there is no launcher to ask, or it cannot answer."""
    if not launcher.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("confinement_launcher", launcher)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return int(module.landlock_abi())
    except Exception:
        logger.exception("confinement: %s could not say whether the kernel has Landlock", launcher)
        return None


def log_confinement(launcher: Path) -> None:
    """One line at startup: whether the processes this host starts can be confined."""
    abi = landlock_abi(launcher)
    if abi is None:
        logger.warning("confinement: no working launcher at %s — a confined spawn will be refused", launcher)
    elif abi:
        logger.info("confinement: Landlock ABI %d", abi)
    else:
        logger.warning("confinement: this kernel has no Landlock — processes run unconfined")
