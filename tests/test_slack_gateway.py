"""SlackGateway: the message decision, attachments, and the seam to its
interactive half (offline).

Constructs a real AsyncApp/socket handler (cheap, no network) inside the running
test loop, then drives the gateway's internal handlers directly. Clicks,
modals, shortcuts and slash commands are SlackInteractions' and are tested in
test_slack_interactions.py.
"""

import asyncio
import contextlib
from pathlib import Path

import pytest
from slack_bolt.async_app import AsyncApp

from crucible.attachments import AttachmentStore
from crucible.gateways.slack.gateway import SlackGateway
from crucible.gateways.slack.interactions import SlackInteractions
from tests.fakes.dispatch import FakeDispatcher, FakeSink

OWN = "UBOT"

# Each AsyncApp/socket handler opens an aiohttp session; close them after each test
# so the suite stays free of "Unclosed client session" noise.
_HANDLERS: list = []


@pytest.fixture(autouse=True)
async def _close_handlers():
    yield
    for handler in _HANDLERS:
        try:
            await handler.close_async()
        except Exception:
            pass
    _HANDLERS.clear()


async def _ack() -> None:
    return None


def _gateway(sink, dispatcher=None, poster=None, **kwargs):
    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)
    gw = SlackGateway(
        app, "xapp-fake", sink, object(), poster=poster, dispatcher=dispatcher, **kwargs  # type: ignore[arg-type]
    )
    gw._own_user_id = OWN
    return gw


async def test_dm_message_is_submitted() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "D1", "channel_type": "im", "ts": "1.0", "user": "U2", "text": "hi"}
    )
    assert len(sink.submitted) == 1
    assert sink.submitted[0].is_dm is True


async def test_channel_without_mention_is_ignored() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "C1", "channel_type": "channel", "ts": "1.0", "user": "U2", "text": "hello"}
    )
    assert sink.submitted == []


async def test_the_interactive_half_is_the_shared_one_and_only_with_a_dispatcher() -> None:
    """A gateway with a dispatcher hands clicks, modals, shortcuts and slash
    commands to the same class the broker drives alone; without one there is
    nobody to route them to, and only the message handler is registered."""
    routed = _gateway(FakeSink(), dispatcher=FakeDispatcher())
    assert isinstance(routed.interactions, SlackInteractions)
    # message + action + view + shortcut + command
    assert len(routed._app._async_listeners) == 5
    silent = _gateway(FakeSink(), dispatcher=None)
    assert silent.interactions is None
    assert len(silent._app._async_listeners) == 1


async def test_channel_mention_is_submitted() -> None:
    sink = FakeSink()
    gw = _gateway(sink)
    await gw._handle_message(
        {"channel": "C1", "channel_type": "channel", "ts": "1.0", "user": "U2", "text": f"<@{OWN}> hi"}
    )
    assert len(sink.submitted) == 1


# -- incoming attachments ----------------------------------------------------


class FakeHttpResponse:
    def __init__(self, *, status: int = 200, body: bytes = b"", content_type: str = "image/png"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeHttpSession:
    """Stands in for aiohttp.ClientSession: records the request, canned response."""

    def __init__(self, response: FakeHttpResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, dict]] = []

    def __call__(self) -> "FakeHttpSession":
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def get(self, url: str, headers: dict | None = None) -> FakeHttpResponse:
        self.requests.append((url, headers or {}))
        return self.response


def _file_event(**over) -> dict:
    event = {
        "channel": "D1", "channel_type": "im", "ts": "1.0", "user": "U2",
        "subtype": "file_share", "text": "",
        "files": [
            {
                "id": "F1", "name": "screen.png", "mimetype": "image/png", "size": 7,
                "url_private_download": "https://files.slack.com/f/F1",
            }
        ],
    }
    event.update(over)
    return event


async def test_slack_attachment_is_downloaded_with_the_bot_token(
    tmp_path, monkeypatch
) -> None:
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    http = FakeHttpSession(FakeHttpResponse(body=b"PNGDATA"))
    monkeypatch.setattr("crucible.gateways.slack.gateway.ClientSession", http)
    sink = FakeSink()
    gw = _gateway(sink, agent="assistant", attachments=store)

    await gw._handle_message(_file_event())

    (msg,) = sink.submitted
    (attachment,) = msg.attachments
    assert attachment.name == "screen.png"
    assert Path(attachment.path).read_bytes() == b"PNGDATA"
    url, headers = http.requests[0]
    assert url == "https://files.slack.com/f/F1"
    assert headers["Authorization"] == "Bearer xoxb-fake"


async def test_a_login_page_instead_of_a_file_leaves_the_message_intact(
    tmp_path, monkeypatch
) -> None:
    # Slack answers an unauthorized download with its sign-in HTML, not an error.
    store = AttachmentStore(tmp_path, max_bytes=1024, retention_days=14)
    http = FakeHttpSession(FakeHttpResponse(body=b"<html>", content_type="text/html"))
    monkeypatch.setattr("crucible.gateways.slack.gateway.ClientSession", http)
    sink = FakeSink()
    gw = _gateway(sink, agent="assistant", attachments=store)

    await gw._handle_message(_file_event(text="look"))

    (msg,) = sink.submitted
    assert msg.attachments == ()
    assert msg.text == "look"


async def test_a_gateway_stopped_and_run_again_opens_a_new_connection() -> None:
    """A lost lease stops the gateway; taking the lease back runs it again.
    slack_sdk's socket client stays closed once closed, so each run gets a
    handler of its own — the second run connects instead of waiting on a
    dead client forever."""

    class _Handler:
        made: list["_Handler"] = []

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False
            self.made.append(self)

        async def start_async(self) -> None:
            self.started.set()
            await asyncio.Event().wait()  # holds the connection until closed

        async def close_async(self) -> None:
            self.closed = True

    gw = _gateway(FakeSink())
    gw._new_handler = lambda: _Handler()  # type: ignore[method-assign]
    first = asyncio.create_task(gw.run())
    for _ in range(50):
        if _Handler.made:
            break
        await asyncio.sleep(0)
    await asyncio.wait_for(_Handler.made[0].started.wait(), 1)
    await gw.stop()
    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first
    assert _Handler.made[0].closed is True
    second = asyncio.create_task(gw.run())
    await asyncio.wait_for(asyncio.sleep(0.05), 1)
    assert len(_Handler.made) == 2 and _Handler.made[1].started.is_set()  # a fresh handler, connected
    assert _Handler.made[1] is not _Handler.made[0]
    second.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await second
