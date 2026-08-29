"""pi driver: spawns and drives `pi --mode rpc` over line-delimited JSONL.

Layers (each testable in isolation): protocol (pure encode/decode) -> transport
(the bytes) -> session (one turn at a time) -> runtime (session pool). Crossing
those, ``hosts/`` decides WHERE the process runs: as a child of this one, or in
a container of the agent's own.
"""

from pathlib import Path

from crucible.runtimes.pi.errors import (
    PiError,
    PiHostError,
    PiProcessError,
    PiProtocolError,
    PiTimeout,
)
from crucible.runtimes.pi.hosts import (
    HostRouter,
    LocalHost,
    RemoteHost,
    RuntimeHost,
)
from crucible.runtimes.pi.profiles import PiProfile, build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.runtimes.pi.session import PiResult, PiRpcSession
from crucible.runtimes.pi.spawn import SpawnRequest, safe_session_id

# The pi tool bridge ships WITH the driver (it is the client half of the engine's
# tool-server contract, not agent content); the composition root loads it via -e.
EXTENSION_PATH = Path(__file__).parent / "extension" / "index.ts"

__all__ = [
    "HostRouter",
    "LocalHost",
    "RemoteHost",
    "RuntimeHost",
    "SpawnRequest",
    "PiHostError",
    "PiError",
    "PiProcessError",
    "PiProtocolError",
    "PiTimeout",
    "PiProfile",
    "PiResult",
    "PiRpcSession",
    "PiRuntime",
    "EXTENSION_PATH",
    "build_pi_profile",
    "safe_session_id",
]
