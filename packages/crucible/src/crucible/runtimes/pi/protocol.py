"""pi RPC JSONL protocol: command encoders and event decoders.

Pure functions only (no I/O), so this layer is trivially unit-testable. pi's
RPC mode is a line-delimited protocol: each command (stdin) and each event or
response (stdout) is one JSON object on a single `\\n`-terminated line.

Commands carry an optional ``id`` that pi echoes back in the matching
``{"type": "response", ...}`` line, which is how we correlate request/response.
Events (agent_start, message_update, tool_execution_*, agent_end, ...) stream
asynchronously without an ``id``.
"""

import json
import uuid
from dataclasses import dataclass
from typing import Any

from crucible.runtimes.pi.errors import PiProtocolError

# --- Command types (bot -> pi, written to stdin) ---------------------------
CMD_PROMPT = "prompt"
CMD_FOLLOW_UP = "follow_up"
CMD_ABORT = "abort"

# --- Event/response types (pi -> bot, read from stdout) --------------------
EV_RESPONSE = "response"
EV_AGENT_START = "agent_start"
EV_AGENT_END = "agent_end"
EV_TURN_START = "turn_start"
EV_TURN_END = "turn_end"
EV_MESSAGE_UPDATE = "message_update"
EV_TOOL_EXECUTION_START = "tool_execution_start"
EV_TOOL_EXECUTION_END = "tool_execution_end"
EV_EXTENSION_UI_REQUEST = "extension_ui_request"

# Assistant streaming sub-events (inside message_update.assistantMessageEvent).
# Current pi emits text_start/text_delta/text_end (completed text in
# text_end.content); older builds emitted a single text_done. We aggregate
# completed text only and accept both; deltas are the raw material for a future
# streaming UX and are ignored here.
ASSISTANT_TEXT_END = "text_end"
ASSISTANT_TEXT_DONE = "text_done"
ASSISTANT_TEXT_DELTA = "text_delta"

# Unicode line separators are not valid JSONL framing and must be rejected.
_FORBIDDEN_LINE_SEPARATORS = ("\u2028", "\u2029")


def new_command_id() -> str:
    return uuid.uuid4().hex


def _encode(command: dict[str, Any]) -> str:
    """Serialize a command to a single JSONL line (with trailing newline)."""
    return json.dumps(command, ensure_ascii=False) + "\n"


def encode_prompt(message: str, *, command_id: str) -> str:
    return _encode({"id": command_id, "type": CMD_PROMPT, "message": message})


def encode_follow_up(message: str, *, command_id: str) -> str:
    return _encode({"id": command_id, "type": CMD_FOLLOW_UP, "message": message})


def encode_abort(*, command_id: str) -> str:
    return _encode({"id": command_id, "type": CMD_ABORT})


def encode_extension_ui_response(
    request_id: str,
    *,
    value: Any = None,
    confirmed: bool | None = None,
    cancelled: bool | None = None,
) -> str:
    """Reply to an `extension_ui_request` (e.g. an approval dialog)."""
    command: dict[str, Any] = {"type": "extension_ui_response", "id": request_id}
    if value is not None:
        command["value"] = value
    if confirmed is not None:
        command["confirmed"] = confirmed
    if cancelled is not None:
        command["cancelled"] = cancelled
    return _encode(command)


@dataclass(frozen=True)
class PiEvent:
    """A parsed inbound line from pi (an event or a command response)."""

    type: str
    raw: dict[str, Any]

    @property
    def id(self) -> str | None:
        return self.raw.get("id")

    @property
    def is_response(self) -> bool:
        return self.type == EV_RESPONSE

    @property
    def success(self) -> bool | None:
        return self.raw.get("success")

    @property
    def error(self) -> str | None:
        return self.raw.get("error")


def parse_line(line: str) -> PiEvent:
    """Parse one stdout line into a ``PiEvent``.

    Strips an optional trailing ``\\r`` (CRLF tolerance) but rejects Unicode
    line separators, which are not valid JSONL framing.
    """
    if any(sep in line for sep in _FORBIDDEN_LINE_SEPARATORS):
        raise PiProtocolError("Line contains a forbidden Unicode line separator")

    stripped = line.rstrip("\r\n")
    if not stripped.strip():
        raise PiProtocolError("Empty line")

    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise PiProtocolError(f"Invalid JSON line: {exc}") from exc

    if not isinstance(obj, dict):
        raise PiProtocolError(f"Expected a JSON object, got {type(obj).__name__}")

    event_type = obj.get("type")
    if not isinstance(event_type, str):
        raise PiProtocolError("Line is missing a string 'type' field")

    return PiEvent(type=event_type, raw=obj)


def assistant_event(event: PiEvent) -> dict[str, Any] | None:
    """Return the assistantMessageEvent payload of a message_update, if any."""
    if event.type != EV_MESSAGE_UPDATE:
        return None
    payload = event.raw.get("assistantMessageEvent")
    return payload if isinstance(payload, dict) else None


def completed_text(event: PiEvent) -> str | None:
    """Completed text of one content block (text_end / legacy text_done)."""
    payload = assistant_event(event)
    if payload and payload.get("type") in (ASSISTANT_TEXT_END, ASSISTANT_TEXT_DONE):
        content = payload.get("content")
        return content if isinstance(content, str) else None
    return None


def tool_name(event: PiEvent) -> str | None:
    """Tool name carried by tool_execution_start/end events."""
    if event.type not in (EV_TOOL_EXECUTION_START, EV_TOOL_EXECUTION_END):
        return None
    # pi nests tool metadata under a few possible keys depending on build.
    for key in ("toolName", "name"):
        value = event.raw.get(key)
        if isinstance(value, str):
            return value
    tool = event.raw.get("tool")
    if isinstance(tool, dict):
        name = tool.get("name")
        if isinstance(name, str):
            return name
    return None
