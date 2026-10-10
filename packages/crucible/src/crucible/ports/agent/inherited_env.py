"""What a runtime process may be told to inherit from the engine's environment.

A runtime process is started with the environment the engine grants it, never
with everything the engine holds (the host that starts it applies this). What it
inherits beyond a base set is named twice over: per agent in ``runtime.env``
(``AgentSpec.inherit``) and for every agent in ``RUNTIME_ENV`` / ``RUNTIME_ENV__*``.
Both lists are checked here, by one rule, because both end in the same place:
the process's environment.

A name is a variable name. A family is a literal prefix of three characters or
more and a ``*`` — ``MYSERVICE_*`` — and nothing broader: ``A*`` would take every
``AGENTS_*`` token. The engine's own families (its chat tokens, its callers'
tokens, its store, its model key) are never taken by a pattern, only one name
at a time, so a typo or a hopeful glob cannot hand an agent the engine's
secrets.
"""

import re
from collections.abc import Collection

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FAMILY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}\*")

# The families of keys ``crucible.config`` reads its secrets and addresses
# from. Kept here rather than there because the profile loader may not import
# the settings; ``tests/test_inherited_env.py`` holds the two in step.
RESERVED_PREFIXES = (
    "AGENTS_", "MATTERMOST_", "SLACK_", "TOOL_", "INTEGRATIONS_",
    "HTTP_CALLER_", "WS_SERVICE_", "COMMAND_TOKENS", "DB_", "LLM_",
)


def check_inherited_env(pattern: str, *, reserved: Collection[str] = ()) -> None:
    """Refuse ``pattern`` as something a runtime process inherits, with the
    reason, unless it is a variable name or a family with a real prefix that is
    not one of the engine's own — ``RESERVED_PREFIXES``, and the application's
    own families it names in ``reserved``. Raises ``ValueError``."""
    if _NAME.fullmatch(pattern):
        return
    if not _FAMILY.fullmatch(pattern):
        raise ValueError(
            f"{pattern!r}: a variable name, or a prefix of three characters or more "
            "followed by *"
        )
    prefix = pattern[:-1]
    if any(prefix.startswith(r) or r.startswith(prefix) for r in (*RESERVED_PREFIXES, *reserved)):
        raise ValueError(
            f"{pattern!r}: the engine's own variables are named one by one, never by a pattern"
        )
