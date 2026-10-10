"""A stand-in OAuth 2.0 provider for the connected-accounts e2e leg.

Runs inside the engine's container (stdlib only), so the engine, the "browser"
and the provider all meet on 127.0.0.1 and nothing has to be published:

    python e2e_fake_oauth.py <port> <email>

- ``/oauth/authorize`` consents at once: a 302 to ``redirect_uri`` with a
  single-use code bound to the PKCE challenge;
- ``/oauth/token`` exchanges the code only with the matching verifier;
- ``/user`` answers who the token is (``<email>``, which the engine matches
  against the chat profile);
- ``/oauth/revoke`` records the revoke;
- ``/api/projects`` answers a project list to a live token, 401 otherwise;
- ``/log`` is what happened, for the test to assert on.
"""

import base64
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

PORT, EMAIL = int(sys.argv[1]), sys.argv[2]
CODES: dict[str, str] = {}  # code -> PKCE challenge
LIVE: set[str] = set()
LOG: list[dict] = []
PROJECT = "e2e-marker-project"


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, status: int, body: object, headers: dict | None = None) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict[str, str]:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _bearer(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/oauth/authorize":
            code = f"code-{len(CODES) + 1}"
            CODES[code] = query.get("code_challenge", "")
            LOG.append({"event": "authorize", "pkce": query.get("code_challenge_method") == "S256"})
            target = query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"]})
            self._send(302, {}, {"Location": target})
        elif url.path == "/user":
            ok = self._bearer() in LIVE
            self._send(200, {"id": 42, "username": "e2e-person", "name": "E2E Person", "email": EMAIL}) if ok \
                else self._send(401, {"error": "invalid_token"})
        elif url.path == "/api/projects":
            token = self._bearer()
            LOG.append({"event": "api", "authorized": token in LIVE})
            self._send(200, [{"name": PROJECT}]) if token in LIVE else self._send(401, {"error": "invalid_token"})
        elif url.path == "/log":
            self._send(200, LOG)
        else:
            self._send(404, {})

    def do_POST(self) -> None:
        form = self._form()
        if self.path == "/oauth/token":
            expected = CODES.pop(form.get("code", ""), None)
            verified = expected is not None and expected == challenge(form.get("code_verifier", ""))
            LOG.append({"event": "token", "verified": verified})
            if not verified:
                self._send(400, {"error": "invalid_grant"})
                return
            token = f"token-{len(LIVE) + 1}"
            LIVE.add(token)
            self._send(200, {"access_token": token, "refresh_token": f"refresh-{token}", "expires_in": 3600,
                             "token_type": "Bearer"})
        elif self.path == "/oauth/revoke":
            LOG.append({"event": "revoke"})
            LIVE.discard(form.get("token", ""))
            self._send(200, {})
        else:
            self._send(404, {})


ThreadingHTTPServer(("127.0.0.1", PORT), Provider).serve_forever()
