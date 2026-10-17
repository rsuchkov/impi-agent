"""Every typed tool this engine offers, from the modules named here.

The composition root builds its registry from this and nothing else, so what
an agent may name in ``runtime.tools`` is what these modules mark ``@tool`` —
and ``impi escape``, which tells a profile started on the host which tools it
will not have, reads the same registry. A new tool module is added here once.
"""

from crucible import builtin_tools
from crucible.tools.registry import ToolRegistry, build_registry
from impi import agent_tools, chat_tools, connection_tools, skill_tools, task_tools


def engine_tool_registry() -> ToolRegistry:
    return build_registry(
        builtin_tools, chat_tools, agent_tools, skill_tools, task_tools, connection_tools
    )
