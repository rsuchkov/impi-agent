"""The default audit trail: one ``key=value`` line per event on its own logger.

Each value is plain or JSON-quoted, and cut, so no value can add a field or a
line. The logger is ``crucible.connections.audit`` — an application routes it
where its audit goes, and keeps it at INFO when the rest of its log is quieter.
"""

import json
import logging
import re

AUDIT_LOGGER = "crucible.connections.audit"

# What happened…
EVENT_CONNECT_START = "connect_start"
EVENT_CONNECT = "connect"
EVENT_REFRESH = "refresh"
EVENT_FORGET = "forget"
EVENT_DISCONNECT = "disconnect"
EVENT_MOVE = "move"  # a connection handed to another owner
# …and how it ended.
RESULT_OK = "ok"
RESULT_UNAVAILABLE = "unavailable"
RESULT_REJECTED = "rejected"
RESULT_NOT_CONNECTED = "not_connected"
RESULT_REVOKE_FAILED = "revoke_failed"

_PLAIN = re.compile(r"[\w.@/:+~#-]+")
_LIMIT = 300


def _quoted(value: str) -> str:
    value = value[:_LIMIT]
    return value if _PLAIN.fullmatch(value) else json.dumps(value, ensure_ascii=False)


class LoggingAudit:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger(AUDIT_LOGGER)

    def record(self, event: str, result: str, **fields: str) -> None:
        parts = [f"event={_quoted(event)}", f"result={_quoted(result)}"]
        parts += [f"{key}={_quoted(value)}" for key, value in fields.items() if value]
        self._logger.info(" ".join(parts))
