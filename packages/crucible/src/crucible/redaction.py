"""Keeping secrets out of what is logged and what is reported.

A log line, an exception's text, an error report sent to a tracker: each may
carry a value nobody should read there — a request's headers, a URL with a
token in it, the environment a process was started with. ``scrub`` replaces
the value and keeps the name, so the line still says what was there.

Built in are the secrets this library itself hands out or receives: the
tool server's token and session proof (by header and by variable), a
callback receiver's secret path segment, bearer and chat-platform tokens,
OAuth values, credentials in a URL, private keys. An application adds its
own patterns; a pattern's first group, when it has one, is kept and the rest
of the match replaced.

    for handler in logging.getLogger().handlers:              # every record
        handler.addFilter(ScrubbingFilter())
    sentry_sdk.init(before_send=lambda event, hint: scrub_data(event))

On a handler, not a logger: a logger's filters see only what is logged to
that logger itself, not what its children pass up.

A heuristic over text, like every redaction: it narrows what leaks, it does
not make logging a secret safe. Standard library only.
"""

import logging
import re
from collections.abc import Iterable
from typing import Any

REDACTED = "***"

_SECRET_WORDS = r"(?:token|secret|password|passwd|proof|api[_-]?key|private[_-]?key|client[_-]?secret|code[_-]?verifier|refresh[_-]?token|access[_-]?token|id[_-]?token|cookie|authorization)"

# A secret's name ends in one of these words — ``max_tokens`` is not one —
# perhaps followed by a per-agent suffix (``TOKEN__ASSISTANT``).
_NAME_END = _SECRET_WORDS + r"(?:__[a-z0-9_]+)?"

# Each: the part kept (group 1), then the value that goes.
BUILTIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # A private key, whole.
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # Headers, by their names — X-Tool-Token, X-Session-Proof, X-Api-Key —
    # not any word before a colon in a sentence.
    re.compile(
        r"(?i)\b((?:x-[a-z0-9-]*?(?:token|secret|proof|key|auth[a-z]*))\s*:\s*)[^\s,;]+"
    ),
    # Authorization and Cookie carry a value that may itself hold spaces and
    # semicolons (``Basic …``, ``a=1; session=…``): the whole value goes, to
    # the end of the line or the quote that closes it.
    re.compile(r"""(?i)(\b(?:(?:proxy-)?authorization|(?:set-)?cookie)\s*:\s*)[^\r\n"']+"""),
    # A bearer token on its own.
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    # Variables and query parameters: TOOL_TOKEN=…, TOOL_SESSION_PROOF=…,
    # AGENTS_MM_TOKEN__SUPPORT=…, ?access_token=….
    re.compile(r"(?i)\b([a-z0-9_]*?" + _NAME_END + r"\s*=\s*)[^\s&,;]+"),
    # JSON and dict members: "access_token": "…", 'client_secret': '…'.
    re.compile(r"""(?i)(["'][a-z0-9_-]*?""" + _NAME_END + r"""["']\s*:\s*["'])[^"']*"""),
    # A URL's credentials: scheme://user:password@host.
    re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^/\s@]+(?=@)"),
    # A callback receiver's secret path segment (receiver_paths.py).
    re.compile(r"(/)[A-Za-z0-9_-]{16,}(?=/(?:interact|dialog|command)\b)"),
    # Slack tokens.
    re.compile(r"()\bx(?:ox[abposr]|app)-[A-Za-z0-9-]{10,}"),
)


def scrub(text: str, extra: Iterable[re.Pattern[str] | str] = ()) -> str:
    """``text`` with every secret the patterns find replaced by ``REDACTED``."""
    for pattern in (*BUILTIN_PATTERNS, *(re.compile(p) if isinstance(p, str) else p for p in extra)):
        text = pattern.sub(_keep_first_group, text)
    return text


def scrub_data(value: Any, extra: Iterable[re.Pattern[str] | str] = ()) -> Any:
    """``value`` with every string in it scrubbed, through dicts, lists and
    tuples — an error report as a tracker builds it. A key that names a
    secret has its whole value replaced, whatever its type."""
    extra = tuple(extra)
    if isinstance(value, str):
        return scrub(value, extra)
    if isinstance(value, dict):
        return {
            key: REDACTED if isinstance(key, str) and _names_a_secret(key) else scrub_data(item, extra)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_data(item, extra) for item in value)
    return value


class ScrubbingFilter(logging.Filter):
    """Scrubs every record it sees: its message with the arguments in, and
    the traceback text. Add it to each handler once at startup."""

    def __init__(self, extra: Iterable[re.Pattern[str] | str] = ()) -> None:
        super().__init__()
        self._extra = tuple(extra)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage(), self._extra)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text, self._extra)
        return True


_SECRET_KEY = re.compile(r"(?i)(?:^|[_.-])" + _NAME_END + "$")
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _names_a_secret(key: str) -> bool:
    """``access_token``, ``X-Tool-Token``, ``clientSecret`` — not ``max_tokens``."""
    return bool(_SECRET_KEY.search(_CAMEL_HUMP.sub("_", key)))


def _keep_first_group(match: re.Match[str]) -> str:
    kept = match.group(1) if match.re.groups else ""
    return f"{kept or ''}{REDACTED}"
