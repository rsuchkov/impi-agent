"""The page a provider sends the person back to: ``GET /oauth/callback``.

A listener of its own, on its own port — the one part of the application a
person's browser reaches. It finishes the connect the state names (the state
is the proof: it was handed to one person, privately, and works once) and
answers with a short page saying how it went. Nothing else is served here.

What the request carries is never written down: the access log is off, since
aiohttp's would print the query — the state and the code — and the page
neither repeats them nor lets a browser send them on (no referrer, no cache).
"""

import html
import logging

from aiohttp import web

from crucible.connections.errors import (
    ACCOUNT_REFUSED,
    LINK_FOREIGN,
    LINK_REJECTED,
    LinkRefused,
    ProviderUnavailable,
)
from crucible.connections.service import OAuthConnections

logger = logging.getLogger(__name__)

CALLBACK_PATH = "/oauth/callback"

_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
}
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


class CallbackServer:
    def __init__(
        self, connections: OAuthConnections, *, host: str = "0.0.0.0", port: int = 8429,
        start_again: str = DEFAULT_START_AGAIN,
    ) -> None:
        self._connections = connections
        self._start_again = start_again
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
        state = request.query.get("state", "")
        if not state:
            return _page(400, "Not a connect", "This address only finishes a connect started in the chat.")
        if request.query.get("error"):
            await self._connections.refuse(state)
            return _page(200, "Not connected", "You declined, so nothing was connected. " + self._start_again)
        code = request.query.get("code", "")
        if not code:
            return _page(400, "Not a connect", "The provider sent no code. " + self._start_again)
        try:
            connected = await self._connections.finish(state, code)
        except LinkRefused as exc:
            return _refused(exc, self._start_again)
        except ProviderUnavailable:
            return _page(503, "Try again shortly", "The provider did not answer as it should. " + self._start_again)
        except Exception:
            logger.exception("a connect could not be finished")
            return _page(500, "Not connected", "Something went wrong on our side. " + self._start_again)
        who = connected.account.username or connected.account.name or connected.account.id
        return _page(
            200, "Connected",
            f"Your {connected.provider} account {who} is connected. You can close this tab and go back to the chat.",
        )


def _refused(exc: LinkRefused, start_again: str) -> web.Response:
    if exc.reason == ACCOUNT_REFUSED:
        return _page(
            403, "Not connected",
            f"The account {exc.account} cannot be connected to you here: {exc.detail}. "
            "Sign in to the provider as yourself. " + start_again,
        )
    if exc.reason == LINK_FOREIGN:
        return _page(403, "Not connected", "This connect was started by somebody else. " + start_again)
    if exc.reason == LINK_REJECTED:
        return _page(400, "Not connected", "The provider would not confirm the sign-in. " + start_again)
    return _page(400, "Link expired", "This link has expired or was already used. " + start_again)
