"""http adapter — a request/turn API for programs that cannot hold a socket.

A caller posts a message to an agent's conversation and gets a turn id; it
polls the turn's events (tools running, cards to answer, the reply, how it
ended) and answers cards through the same API. One running turn per
conversation, idempotent on the client's message id, resumable after a reload.
See docs/http-gateway.md for the contract.

auth.py    — who the caller is: the CallerAuthenticator port + TokenCallers
journal.py — one turn's event log, read by cursor
turns.py   — the turns in flight (busy, idempotency, retention) + the flow's tracer
client.py  — ChatClient over the journal (one per agent)
hub.py     — the aiohttp server shared by every agent on the gateway
gateway.py — per-agent lifecycle shim (identity + park-until-stop)
"""

from crucible.gateways.http.auth import Caller, CallerAuthenticator, TokenCallers
from crucible.gateways.http.client import HttpChatClient
from crucible.gateways.http.gateway import HttpGateway
from crucible.gateways.http.hub import HttpHub
from crucible.gateways.http.turns import HttpTurns

# No formatting hint: replies carry Markdown as-is; rendering is the client's.
PROMPT_HINT = ""

__all__ = [
    "PROMPT_HINT",
    "Caller",
    "CallerAuthenticator",
    "HttpChatClient",
    "HttpGateway",
    "HttpHub",
    "HttpTurns",
    "TokenCallers",
]
