"""The vocabulary this driver speaks to a runtime host that runs elsewhere.

One WebSocket per session. Control travels as TEXT frames — JSON objects with a
``type`` — and the line-delimited byte stream travels as BINARY frames, one line
per frame. Keeping the two apart is what stops every runtime line from being
JSON-encoded inside another JSON object: a single tool result can be megabytes,
and paying to escape it twice would be paying for nothing.

The order is fixed: the client sends ``spawn`` first and waits for ``ready``
before writing any line. After that either side may send lines until the host
sends ``exit`` (the process ended, with the detail that says why) or the socket
closes.

The host declares this same vocabulary in its own package instead of importing
it from here. It ships inside the agent's image, and making it depend on this
library would put the engine's whole dependency set back into that image — which
is the thing a separate image exists to avoid. A contract test holds the two
copies to each other, so the duplication cannot drift silently.

This module is constants only. That is what makes that test possible.
"""

# Bumped when a field changes meaning or becomes required. A host that answers
# with a different number is refused rather than half-driven: a spawn that
# quietly loses its tool allowlist is worse than one that does not happen.
PROTOCOL_VERSION = 1

# Endpoints.
SESSION_PATH = "/session"
HEALTH_PATH = "/health"

# The shared secret, per agent. The host is reachable only from the engine's
# side of a private network, but the agent's own shell is on that network too —
# and without a token it could ask its host for a process with an allowlist
# nobody granted it.
TOKEN_HEADER = "X-Runtime-Token"

# Control frame types.
MSG_SPAWN = "spawn"
MSG_READY = "ready"
MSG_ERROR = "error"
MSG_EXIT = "exit"

# Control frame keys.
KEY_TYPE = "type"
KEY_VERSION = "v"
KEY_AGENT = "agent"
KEY_SESSION_ID = "session_id"
KEY_TOOLS = "tools"
KEY_SKILLS = "skills"
KEY_PROVIDER = "provider"
KEY_MODEL = "model"
KEY_SYSTEM_SUFFIX = "append_system_prompt"
KEY_ENV = "env"
KEY_ENV_FILES = "env_files"
KEY_MESSAGE = "message"
KEY_DETAIL = "detail"

# A skill reference on the wire: which mounted root it lives under, and where
# beneath it. Never an absolute path — the two sides do not share a filesystem,
# and a path that happens to exist on both would be the worst outcome of all.
KEY_ROOT = "root"
KEY_PATH = "path"
ROOT_PROFILE = "profile"
ROOT_LIBRARY = "library"

# One event can be a large tool result; the local transport gives the reader the
# same room, and a mismatch here would surface as a dead socket mid-answer.
MAX_FRAME_BYTES = 16 * 1024 * 1024

# Close codes, in the private range. Distinct because they mean different things
# to an operator: a wrong token is a deployment mistake, a version mismatch is a
# half-finished update, a protocol violation is a bug.
CLOSE_UNAUTHORIZED = 4401
CLOSE_PROTOCOL = 4400
CLOSE_VERSION = 4426
