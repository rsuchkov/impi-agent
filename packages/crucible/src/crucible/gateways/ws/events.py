"""Pure frame normalization for the ws gateway: JSON frames from a client
service become neutral ``IncomingMessage``s.

A client service holds ONE socket and addresses any allowed agent per frame
(the ``agent`` field). Conversation keys are namespaced with the service name
(``"<service>:<client conversation id>"``) before they reach the engine, so
two services reusing the same conversation id at the same agent can never
share a session; replies are routed back by that prefix and the namespace is
stripped again on the way out.
"""

import base64
from uuid import uuid4

from crucible.attachments import IncomingFile
from crucible.ports.chat.types import (
    KIND_CHANNEL,
    KIND_DM,
    KIND_THREAD,
    ConversationRef,
    IncomingMessage,
)

_KINDS = {"dm": KIND_DM, "thread": KIND_THREAD, "channel": KIND_CHANNEL}
_SEP = ":"


def internal_conversation(service: str, conversation_id: str) -> str:
    """The engine-side conversation key: service-namespaced."""
    return f"{service}{_SEP}{conversation_id}"


def split_conversation(internal_id: str) -> tuple[str, str]:
    """(service, client conversation id) back out of an internal key."""
    service, _, client_id = internal_id.partition(_SEP)
    return service, client_id


def frame_error(data: dict) -> str | None:
    """Why a ``message`` frame is invalid, or None when it is well-formed."""
    for field in ("agent", "conversation_id"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"'{field}' must be a non-empty string"
    text = data.get("text")
    if not isinstance(text, str):
        return "'text' must be a string"
    files = data.get("files")
    # A photo with no caption is a message; text is only required without files.
    if not text.strip() and not files:
        return "'text' must be a non-empty string unless the frame carries files"
    if files is not None and not isinstance(files, list):
        return "'files' must be a list"
    for file in files or []:
        if not isinstance(file, dict):
            return "each entry of 'files' must be an object"
        if not isinstance(file.get("name"), str) or not file["name"].strip():
            return "each file needs a non-empty 'name'"
        if not isinstance(file.get("data"), str):
            return "each file needs base64 bytes in 'data'"
    kind = data.get("kind")
    if kind is not None and kind not in _KINDS:
        return f"'kind' must be one of {sorted(_KINDS)}"
    return None


def frame_files(data: dict) -> list[IncomingFile]:
    """Decode a frame's inline files. The bytes travel base64-encoded inside the
    frame — a client service may sit behind NAT with no filesystem in common with
    the engine, so there is nothing to fetch. Undecodable data raises."""
    files: list[IncomingFile] = []
    for file in data.get("files") or []:
        try:
            payload = base64.b64decode(file["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"file {file.get('name')!r}: 'data' is not valid base64") from exc
        files.append(
            IncomingFile(
                name=str(file["name"]),
                data=payload,
                mime=str(file.get("mime") or ""),
            )
        )
    return files


def frame_to_incoming(service: str, data: dict) -> IncomingMessage:
    """Map a validated ``message`` frame onto the neutral vocabulary. The
    message id is namespaced like the conversation so replayed deliveries
    from one service dedupe without colliding with another service's ids."""
    conversation = internal_conversation(service, data["conversation_id"].strip())
    message_id = internal_conversation(
        service, str(data.get("message_id") or uuid4().hex)
    )
    kind = _KINDS[data.get("kind") or "dm"]
    user_id = str(data.get("user_id") or "")
    return IncomingMessage(
        ref=ConversationRef(
            channel_id=conversation,
            conversation_id=conversation,
            message_id=message_id,
            thread_root_id="",
        ),
        text=data["text"],
        user_id=user_id,
        username=str(data.get("username") or user_id or "user"),
        kind=kind,
        is_dm=kind == KIND_DM,
        # The service forwarded this message specifically to this agent — that
        # IS the mention; there is no channel chatter to stay silent in.
        mentioned=True,
        raw=data,
    )
