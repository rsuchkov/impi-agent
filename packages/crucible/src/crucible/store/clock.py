"""When a store stamps a record, it stamps it through here.

Records carry their times as ISO strings and are ordered by comparing those
strings, so the format is a contract between backends rather than a detail of
any one of them: two implementations that disagree on precision or on the
offset spelling would sort the same rows differently. One function keeps them
honest, and gives tests a single place to hold time still.
"""

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
