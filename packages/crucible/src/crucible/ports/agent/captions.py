"""CallCaptions: the one-line account of a tool call for a person watching.

A turn's trace shows "Running 2 tools…"; a caption makes it "Running 2 tools… ·
Reading IPA-12436". The caption comes from whoever knows the tool — the tool
itself (``tools.base.Captioning``), or the runtime driver for the runtime's own
built-ins — never from the model, and never as the raw arguments: an argument
is as often a whole script as a key. The flow asks once per call and puts the
answer on the ``ToolStarted`` event every consumer reads.
"""

from collections.abc import Mapping
from typing import Any, Protocol


class CallCaptions(Protocol):
    """``caption`` answers with a short line for the call, or "" when it has
    none to give. It is called inside the runtime's event stream, so it must be
    cheap, synchronous and free of side effects."""

    def caption(self, tool: str, args: Mapping[str, Any]) -> str: ...


class ChainedCaptions:
    """Several sources, the first non-empty answer wins: the deployment's tool
    registry for its typed tools, then the runtime's own for its built-ins."""

    def __init__(self, *sources: CallCaptions) -> None:
        self._sources = sources

    def caption(self, tool: str, args: Mapping[str, Any]) -> str:
        for source in self._sources:
            line = source.caption(tool, args)
            if line:
                return line
        return ""
