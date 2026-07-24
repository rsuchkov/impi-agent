"""Tool layer: typed tools an agent calls through its runtime extension.

The runtime's tool extension registers each tool and forwards the call over
HTTP to :class:`ToolServer` here, which runs it against live engine state
(registry, the calling agent's chat-admin client). No python process is spawned
per call, and tools never import a platform SDK — they depend only on ports.
"""

from crucible.tools.base import Tool, ToolContext, ToolError
from crucible.tools.registry import ToolRegistry, build_registry, tool
from crucible.tools.server import ToolServer
from crucible.tools.wiring import ToolWiring

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "ToolServer",
    "ToolWiring",
    "build_registry",
    "tool",
]
