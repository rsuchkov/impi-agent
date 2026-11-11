"""The page a provider sends the person back to: ``GET /oauth/callback``.

A listener of its own, on its own port — the one part of the application a
person's browser reaches. It finishes the connect the state names (the state
is the proof: it was handed to one person, privately, and works once) and
answers with a short page saying how it went. Nothing else is served here.
The page is the application's to draw (``CallbackPage``): it gets how the
connect ended (``CallbackOutcome``) and answers with a response of its own —
its words, its picture of the provider, its own content-security policy; the
flow around it — the state, the refusals, the headers that keep the query out
of caches and referrers — stays here. ``DefaultCallbackPage`` is the plain one.

What the request carries is never written down: the access log is off, since
aiohttp's would print the query — the state and the code — and the page
neither repeats them nor lets a browser send them on (no referrer, no cache).
"""

import html
import logging
from dataclasses import dataclass
from typing import Protocol

from aiohttp import web

from crucible.connections.errors import (
    ACCOUNT_REFUSED,
    LINK_DENIED,
    LINK_EXPIRED,
    LINK_FOREIGN,
    LINK_REJECTED,
    LinkRefused,
)
from crucible.connections.service import OAuthConnections
from crucible.connections.types import Connected
from crucible.ports.connections import ProviderUnavailable

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/oauth/callback"

# The plain page's content-security policy: nothing loads but its inline
# style. A page of the application's that set no policy of its own gets it.
_DEFAULT_CSP = "default-src 'none'; style-src 'unsafe-inline'"
# On every answer, whoever draws the page, where the page set none: the query
# carried the state and the code, and the page must neither be kept nor send
# them on.
_SAFE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _DEFAULT_CSP,
}
_HEADERS = dict(_SAFE_HEADERS)

# How a connect that did not go through ended, beside the link's own words
# (``connections.errors``: expired, foreign, denied, rejected, account_refused).
OUTCOME_NOT_A_CONNECT = "not_a_connect"  # no state: this address finishes connects and nothing else
OUTCOME_NO_CODE = "no_code"  # the provider sent no code
OUTCOME_PROVIDER_UNAVAILABLE = "provider_unavailable"
OUTCOME_FAILED = "failed"  # something on our side; the log has it
# The HTTP status each ending answers with, as the plain page does.
OUTCOME_STATUS = {
    OUTCOME_NOT_A_CONNECT: 400, OUTCOME_NO_CODE: 400, LINK_DENIED: 200, LINK_EXPIRED: 400, LINK_FOREIGN: 403,
    LINK_REJECTED: 400, ACCOUNT_REFUSED: 403, OUTCOME_PROVIDER_UNAVAILABLE: 503, OUTCOME_FAILED: 500,
}


@dataclass(frozen=True)
class CallbackOutcome:
    """How the connect the person came back from ended, for the page to say.
    ``connected`` when it went through; otherwise ``refusal`` is the word for
    why (``OUTCOME_*`` here, the link's words from ``connections.errors``),
    with ``account`` and ``detail`` filled for an account the application's
    rule refused. ``start_again`` is what the application said to tell a
    person who should start over; ``status`` the HTTP status the plain page
    would answer with."""

    connected: Connected | None = None
    refusal: str = ""
    account: str = ""
    detail: str = ""
    start_again: str = ""

    @property
    def status(self) -> int:
        return 200 if self.connected is not None else OUTCOME_STATUS.get(self.refusal, 400)


class CallbackPage(Protocol):
    """What the person sees when they come back: the application's page for
    the outcome. Its response gets the flow's safety headers added where it
    set none (no-store, no-referrer, nosniff, and the strict content-security
    policy — nothing but inline style — where the page set no policy of its
    own; one with a picture sets what it needs). The outcome's ``account``,
    ``detail`` and the connected account's names are the provider's words:
    a page that puts them in HTML escapes them."""

    def render(self, outcome: CallbackOutcome) -> web.Response: ...
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#222}}
h1{{font-size:1.3rem}}</style></head>
<body><h1>{title}</h1><p>{text}</p></body></html>
"""
# What a page that did not connect says to do next. The application knows
# where a connect starts; this default names nowhere in particular.
DEFAULT_START_AGAIN = "Start the connect again from the chat."


def _page(status: int, title: str, text: str) -> web.Response:
    body = _PAGE.format(title=html.escape(title), text=html.escape(text))
    return web.Response(status=status, text=body, content_type="text/html", headers=_HEADERS)


class DefaultCallbackPage:
    """The plain page: a title and a sentence, the provider by its id, the
    account by its username — what a deployment with no page of its own gets."""

    def render(self, outcome: CallbackOutcome) -> web.Response:
        connected, again = outcome.connected, outcome.start_again
        if connected is not None:
            who = connected.account.username or connected.account.name or connected.account.id
            return _page(
                200, "Connected",
                f"Your {connected.provider} account {who} is connected. You can close this tab and go back to the chat.",
            )
        status, word = outcome.status, outcome.refusal
        if word == OUTCOME_NOT_A_CONNECT:
            return _page(status, "Not a connect", "This address only finishes a connect started in the chat.")
        if word == OUTCOME_NO_CODE:
            return _page(status, "Not a connect", "The provider sent no code. " + again)
        if word == LINK_DENIED:
            return _page(status, "Not connected", "You declined, so nothing was connected. " + again)
        if word == OUTCOME_PROVIDER_UNAVAILABLE:
            return _page(status, "Try again shortly", "The provider did not answer as it should. " + again)
        if word == OUTCOME_FAILED:
            return _page(status, "Not connected", "Something went wrong on our side. " + again)
        if word == ACCOUNT_REFUSED:
            return _page(
                status, "Not connected",
                f"The account {outcome.account} cannot be connected to you here: {outcome.detail}. "
                "Sign in to the provider as yourself. " + again,
            )
        if word == LINK_FOREIGN:
            return _page(status, "Not connected", "This connect was started by somebody else. " + again)
        if word == LINK_REJECTED:
            return _page(status, "Not connected", "The provider would not confirm the sign-in. " + again)
        return _page(status, "Link expired", "This link has expired or was already used. " + again)


class CallbackServer:
    def __init__(
        self, connections: OAuthConnections, *, host: str = "0.0.0.0", port: int = 8429,
        start_again: str = DEFAULT_START_AGAIN, page: CallbackPage | None = None,
    ) -> None:
        self._connections = connections
        self._start_again = start_again
        self._page: CallbackPage = page if page is not None else DefaultCallbackPage()
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get(CALLBACK_PATH, self._callback)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self._host, self._port).start()
        logger.info("connections callback on %s:%d%s", self._host, self._port, CALLBACK_PATH)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _callback(self, request: web.Request) -> web.Response:
        outcome = await self._outcome(request)
        response = self._page.render(outcome)
        for name, value in _SAFE_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    async def _outcome(self, request: web.Request) -> CallbackOutcome:
        """Finish the connect the request names, or say why it did not."""
        again = self._start_again
        state = request.query.get("state", "")
        if not state:
            return CallbackOutcome(refusal=OUTCOME_NOT_A_CONNECT, start_again=again)
        if request.query.get("error"):
            await self._connections.refuse(state)
            return CallbackOutcome(refusal=LINK_DENIED, start_again=again)
        code = request.query.get("code", "")
        if not code:
            return CallbackOutcome(refusal=OUTCOME_NO_CODE, start_again=again)
        try:
            connected = await self._connections.finish(state, code)
        except LinkRefused as exc:
            return CallbackOutcome(refusal=exc.reason, account=exc.account or "", detail=exc.detail or "", start_again=again)
        except ProviderUnavailable:
            return CallbackOutcome(refusal=OUTCOME_PROVIDER_UNAVAILABLE, start_again=again)
        except Exception:
            logger.exception("a connect could not be finished")
            return CallbackOutcome(refusal=OUTCOME_FAILED, start_again=again)
        return CallbackOutcome(connected=connected, start_again=again)
