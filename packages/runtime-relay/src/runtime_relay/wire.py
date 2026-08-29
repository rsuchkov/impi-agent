"""The vocabulary this host speaks to the engine that drives it.

One WebSocket per session. Control travels as TEXT frames — JSON objects with a
``type`` — and the line-delimited byte stream travels as BINARY frames, one line
per frame. Keeping the two apart is what stops every runtime line from being
JSON-encoded inside another JSON object: a single tool result can be megabytes,
and paying to escape it twice would be paying for nothing.

The order is fixed: the client sends ``spawn`` first and waits for ``ready``
before writing any line. After that either side may send lines until this host
sends ``exit`` (the process ended, with the detail that says why) or the socket
closes.

This is a copy. The engine's driver declares the same constants, and this
package deliberately does not import them: it ships inside the agent's image,
and depending on the engine's library would put the engine's whole dependency
set back into that image — which is what a separate image exists to avoid. A
contract test in the workspace holds the two copies to each other, so the
duplication cannot drift silently.

This module is constants only. That is what makes that test possible.
"""

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
