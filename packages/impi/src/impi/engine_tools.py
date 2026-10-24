"""Every typed tool this engine offers, from the modules named here.

The composition root builds its registry from this and nothing else, so what
an agent may name in ``runtime.tools`` is what these modules mark ``@tool`` —
and ``impi escape``, which tells a profile started on the host which tools it
will not have, reads the same registry. A new tool module is added here once.
"""

from collections.abc import Iterable

from crucible import builtin_tools
from crucible.tools.base import Tool
from crucible.tools.registry import ToolRegistry, build_registry
from impi import agent_tools, chat_tools, connection_tools, skill_tools, task_tools


def engine_tool_registry(extra: Iterable[Tool] = ()) -> ToolRegistry:
    """The engine's own tools, and ``extra`` — the deployment's MCP servers'
    tools, built by the composition root from their snapshots."""
    return build_registry(
        builtin_tools, chat_tools, agent_tools, skill_tools, task_tools, connection_tools, extra
    )
