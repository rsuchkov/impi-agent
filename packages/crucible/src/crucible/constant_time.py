"""Finding who a presented secret belongs to without telling a guesser how
close it came.

Every registered secret is compared, each in constant time: a lookup that
stops at the first match times how far a guess got. Bytes, not str —
``hmac.compare_digest`` refuses a str that is not ASCII, and a header is
whatever the caller put there. Standard library only, so any door can use it.
"""

import hmac
from collections.abc import Iterable


def find_by_secret[T](entries: Iterable[tuple[str, T]], presented: str) -> T | None:
    """The value registered under ``presented``, or None."""
    given = presented.encode("utf-8", "surrogateescape")
    found: T | None = None
    for secret, value in entries:
        if hmac.compare_digest(secret.encode("utf-8"), given):
            found = value
    return found


def matches_any(secrets: Iterable[str], presented: str) -> bool:
    """Whether ``presented`` is one of ``secrets``."""
    return find_by_secret(((secret, True) for secret in secrets), presented) is not None
