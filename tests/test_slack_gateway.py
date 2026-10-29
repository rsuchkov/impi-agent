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
    assert sink.submitted[0].conversation_id == "D1" and sink.submitted[0].ref.thread_root_id == ""


async def test_a_direct_message_may_be_answered_in_a_thread_under_it() -> None:
    """``dm_thread``: a top-level direct message is answered in a thread under
    the person's message — where a Slack agent session lives — while the
    conversation stays the DM, memory and all. The DM is direct as before."""
    sink = FakeSink()
    gw = _gateway(sink, dm_thread=True, dispatcher=FakeDispatcher())
    await gw._handle_message({"channel": "D1", "channel_type": "im", "ts": "1.0", "user": "U2", "text": "hi"})
    msg = sink.submitted[0]
    assert (msg.conversation_id, msg.kind, msg.ref.thread_root_id, msg.is_dm) == ("D1", "dm", "1.0", True)
    assert gw.interactions is not None and gw.interactions._dm_thread is True  # shortcuts and reactions follow the rule


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
    # message + the agent panel's four events + action + view + shortcut + command
    assert len(routed._app._async_listeners) == 9
    silent = _gateway(FakeSink(), dispatcher=None)
    assert silent.interactions is None
    assert len(silent._app._async_listeners) == 5


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
    store = AttachmentStore(tmp_path, max_bytes=1024)
    http = FakeHttpSession(FakeHttpResponse(body=b"PNGDATA"))
    monkeypatch.setattr("crucible.gateways.slack.gateway.ClientSession", http)
    sink = FakeSink()
    gw = _gateway(sink, agent="assistant", attachments=store)

    await gw._handle_message(_file_event())

    (msg,) = sink.submitted
    (attachment,) = msg.attachments
    assert attachment.name == "screen.png"
    assert Path(attachment.path).read_bytes() == b"PNGDATA"
    assert Path(attachment.path).parent == store.conversation_dir("assistant", msg.conversation_id)  # the platform's key
    url, headers = http.requests[0]
    assert url == "https://files.slack.com/f/F1"
    assert headers["Authorization"] == "Bearer xoxb-fake"


async def test_a_file_is_saved_under_the_conversation_as_the_application_keys_it(tmp_path, monkeypatch) -> None:
    """An application that keys a Slack thread and a panel as one conversation
    gives the gateway its key: the file lands where the flow's record — and so
    the tool's files directory — says, not under the Slack id."""

    class _TaskKey:
        def key(self, agent: str, msg) -> str:
            return "task-7"

    store = AttachmentStore(tmp_path, max_bytes=1024)
    http = FakeHttpSession(FakeHttpResponse(body=b"PNGDATA"))
    monkeypatch.setattr("crucible.gateways.slack.gateway.ClientSession", http)
    sink = FakeSink()
    gw = _gateway(sink, agent="assistant", attachments=store, keying=_TaskKey())

    await gw._handle_message(_file_event())

    (msg,) = sink.submitted
    (attachment,) = msg.attachments
    assert Path(attachment.path).parent == store.conversation_dir("assistant", "task-7")
    assert msg.conversation_id != "task-7"  # the message still names the platform's conversation


async def test_a_login_page_instead_of_a_file_leaves_the_message_intact(
    tmp_path, monkeypatch
) -> None:
    # Slack answers an unauthorized download with its sign-in HTML, not an error.
    store = AttachmentStore(tmp_path, max_bytes=1024)
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


# -- the agent panel ----------------------------------------------------------------


async def test_the_stop_button_of_the_agent_session_stops_the_threads_turn() -> None:
    dispatcher = FakeDispatcher()
    gw = _gateway(FakeSink(), dispatcher=dispatcher, agent="assistant")
    await gw._on_session_stopped({"channel": "D1", "thread_ts": "1782234671.392669", "user": "U2"})
    assert dispatcher.stopped == [("assistant", "1782234671.392669")]  # the thread is the conversation
    await gw._on_session_stopped({"channel": "D1", "user": "U2"})  # no thread: nothing to stop
    assert len(dispatcher.stopped) == 1
    quiet = _gateway(FakeSink(), dispatcher=None)
    await quiet._on_session_stopped({"channel": "D1", "thread_ts": "1.0", "user": "U2"})  # nobody to route to


async def test_the_home_and_context_events_are_taken_and_handed_to_the_applications_home() -> None:
    """Without a SlackHome the events are acknowledged and nothing is shown
    (no "unhandled request" in the log); with one, its blocks are published as
    the Home view and context changes reach it."""

    class Home:
        def __init__(self) -> None:
            self.opened_by: list[tuple[str, str]] = []
            self.contexts: list[tuple[str, dict]] = []
            self.blocks: list[dict] | None = [{"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}]

        async def opened(self, user_id: str, tab: str, publish) -> None:
            self.opened_by.append((user_id, tab))
            if self.blocks:
                # The hook publishes itself: its own metadata, its own fallback.
                await publish({"type": "home", "blocks": self.blocks, "private_metadata": tab})

        async def context_changed(self, user_id: str, context: dict) -> None:
            self.contexts.append((user_id, context))

    class Client:
        def __init__(self) -> None:
            self.published: list[dict] = []

        async def views_publish(self, **kwargs) -> None:
            self.published.append(kwargs)

    home, client = Home(), Client()
    gw = _gateway(FakeSink(), home=home)
    await gw._on_home_opened({"user": "U2", "tab": "home"}, client)  # type: ignore[arg-type]
    assert home.opened_by == [("U2", "home")]
    assert client.published == [{"user_id": "U2", "view": {"type": "home", "blocks": home.blocks, "private_metadata": "home"}}]
    home.blocks = None
    await gw._on_home_opened({"user": "U2", "tab": "messages"}, client)  # type: ignore[arg-type]
    assert len(client.published) == 1  # nothing to show, nothing published
    await gw._on_context_changed({"user": "U2", "app_context": {"channel_id": "C9"}})
    assert home.contexts == [("U2", {"channel_id": "C9"})]
    bare = _gateway(FakeSink())
    await bare._on_home_opened({"user": "U2", "tab": "home"}, client)  # type: ignore[arg-type]  # taken, nothing done
    await bare._on_context_changed({"user": "U2"})
    assert len(client.published) == 1


async def test_an_applications_own_listener_registered_first_is_the_one_bolt_asks() -> None:
    """The contract an application with a Slack app of its own stands on: Bolt
    gives a request to the first matching listener in order of registration, so
    what the application registered before handing the app over is its own —
    the gateway's catch-all for slash commands comes after it."""
    from slack_bolt.request.async_request import AsyncBoltRequest
    from slack_bolt.response import BoltResponse

    app = AsyncApp(token="xoxb-fake", signing_secret="x" * 16)

    async def mine(ack):
        await ack()

    app.command("/mine")(mine)
    gw = SlackGateway(app, "xapp-fake", FakeSink(), object(), dispatcher=FakeDispatcher())  # type: ignore[arg-type]
    _HANDLERS.append(gw._new_handler())
    body = {"command": "/mine", "text": "", "user_id": "U2", "channel_id": "D1", "channel_name": "directmessage"}
    request = AsyncBoltRequest(body=body, mode="socket_mode")
    matching = [
        listener for listener in app._async_listeners
        if await listener.async_matches(req=request, resp=BoltResponse(status=200))
    ]
    assert len(matching) == 2  # the application's and the gateway's catch-all both match...
    assert matching[0].ack_function is mine  # ...and the application's comes first
