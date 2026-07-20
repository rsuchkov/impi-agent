"""pi driver: spawns and drives `pi --mode rpc` over line-delimited JSONL.

Layers (each testable in isolation): protocol (pure encode/decode) -> transport
(subprocess bytes) -> session (one turn at a time) -> runtime (session pool).
"""

from pathlib import Path

from crucible.runtimes.pi.errors import PiError, PiProcessError, PiProtocolError, PiTimeout
from crucible.runtimes.pi.profiles import PiProfile, build_pi_profile
from crucible.runtimes.pi.runtime import PiRuntime
from crucible.runtimes.pi.session import PiResult, PiRpcSession

# The pi tool bridge ships WITH the driver (it is the client half of the engine's
# tool-server contract, not agent content); the composition root loads it via -e.
EXTENSION_PATH = Path(__file__).parent / "extension" / "index.ts"

__all__ = [
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
]
