"""Where a callback receiver's endpoints live: under a secret path segment.

A platform calling back (a click, a slash command) carries no signature and
names its own user, so the path is what proves the call came from the party
that was given the URL. Standard library only: the settings that hold the
secret, the receiver that serves the routes and an application that builds
its own URLs all import it, and none of them should need the others for it.
"""

import re

# Path-safe by construction — letters, digits, `-` and `_`, sixteen at least.
RECEIVER_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{16,}")


def callback_path(endpoint: str, *, secret: str = "") -> str:
    """Where an endpoint of the receiver lives: ``/<secret>/<endpoint>`` when a
    secret is set, the bare ``/<endpoint>`` when none is. The one rule both the
    URLs handed to the platform and the receiver's own routes are built from."""
    return f"/{secret}/{endpoint}" if secret else f"/{endpoint}"
