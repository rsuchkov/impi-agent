"""What a consumer of the approval primitive exposes to the layer that calls it.

Declared here, with no dependencies of its own, so the tool layer can require an
approval without importing the store or the chat clients that producing one
needs.
"""

from typing import Any, Protocol


class ToolApproving(Protocol):
    """Ask a human whether a tool call may go ahead.

    The tool server holds one of these and consults it before executing a tool
    that declares ``requires_confirmation``. Returning False means the call does
    not happen — a refusal, a silence, or nobody available to ask.
    """

    async def confirm(
        self, agent: str, tool: str, args: dict[str, Any], *, runtime_session_id: str
    ) -> bool: ...
