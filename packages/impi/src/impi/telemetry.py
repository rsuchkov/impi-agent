"""The telemetry receiver impi runs with: one JSON line per event on the logger
``impi.telemetry``, for whatever reads the engine's log — a log pipeline, a
dashboard that tails it, a person with grep. An application that wants an
exporter of its own (a metrics endpoint, a table) builds the flows with its
own ``Telemetry`` instead; the port is ``crucible.ports.telemetry``.
"""

import dataclasses
import json
import logging
import re

from crucible.ports.telemetry import TelemetryEvent

logger = logging.getLogger("impi.telemetry")


class LogTelemetry:
    """Every event as one JSON line: ``{"event": "turn_ended", ...fields}``.
    Flat fields only, as the port promises; a nested record (token usage)
    comes as its own object."""

    def record(self, event: TelemetryEvent) -> None:
        line = {"event": _event_name(event), **dataclasses.asdict(event)}
        logger.info("%s", json.dumps(line, ensure_ascii=False, default=str))


def _event_name(event: TelemetryEvent) -> str:
    """``TurnEnded`` -> ``turn_ended``: the event, in the log's own case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", type(event).__name__).lower()
