"""The tools that act as the person at a provider they connected: against a
local stand-in for the provider's API."""

import json
import logging
from pathlib import Path

import pytest
from aiohttp import web

from crucible.connections import OAuthConnections
from crucible.connections.sealing import FernetSealer, new_sealing_key
from crucible.ports.chat.types import KIND_CHANNEL, KIND_DM
from crucible.tools.base import CAP_CONNECTIONS, ToolContext, ToolError
from crucible.tools.registry import build_registry
from impi.connection_tools import (
    MAX_RESPONSE_CHARS,
    ConnectedAccounts,
    ConnectedApiRead,
    ConnectedApiSettings,
    ConnectedApiWrite,
    api_target,
)
from impi.connections import NarrowedConnections
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore
from tests.test_connections import FakeProvider
from tests.test_tools import AGENTS, FakeDirectory

PORT = 8621
API = f"http://127.0.0.1:{PORT}/api/v4"
OWNER = "mattermost:u1"


class ProviderApi:
    """What the provider's API was asked, and with which token."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str, object]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.seen.append((request.method, request.path_qs, request.headers.get("Authorization", ""), body))
        route = request.path.removeprefix("/api/v4/")
        if route == "expired":
            return web.json_response({"message": "401 Unauthorized"}, status=401)
        if route == "elsewhere":
            raise web.HTTPFound("http://127.0.0.1:1/steal")
        if route == "big":
            return web.Response(text="x" * (MAX_RESPONSE_CHARS + 5), content_type="application/json")
        if route == "plain":
            return web.Response(text="just text")
        return web.json_response({"route": route, "body": body})


@pytest.fixture
async def api():
    provider_api = ProviderApi()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", provider_api.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", PORT).start()
    try:
        yield provider_api
    finally:
        await runner.cleanup()


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: str) -> ConnectedApiSettings:
    env = tmp_path / ".env"
    env.write_text("\n".join(f"{k}={v}" for k, v in {
        "CONNECTION_CLIENT_ID__GITLAB": "c",
        "CONNECTION_CLIENT_SECRET__GITLAB": "s",
        "CONNECTION_BASE_URL__GITLAB": "https://gitlab.example.com",
        "CONNECTION_API_URL__GITLAB": API,
        **extra,
    }.items()))
    for key in ("CONNECTION_API_URL__GITLAB", "CONNECTIONS_COMMAND"):
        monkeypatch.delenv(key, raising=False)
    return ConnectedApiSettings(_env_file=str(env))  # type: ignore[call-arg]


async def _connected(provider: FakeProvider, *, connect: bool = True) -> OAuthConnections:
    service = OAuthConnections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]),
    )
    if connect:
        state, code = provider.consent(await service.begin(OWNER, "gitlab"))
        await service.finish(state, code)
    return service


def _ctx(service: OAuthConnections, settings: ConnectedApiSettings, *, kind: str = KIND_DM,
         allowed: tuple[str, ...] = ("gitlab",)) -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS), settings=settings,
        conversation_kind=kind, connections=NarrowedConnections(service.for_owner(OWNER), allowed),
    )


# --- where a token may go ------------------------------------------------------------


@pytest.mark.parametrize(("path", "url"), [
    ("projects?membership=true", f"{API}/projects?membership=true"),
    ("/user", f"{API}/user"),
    ("projects/group%2Fname", f"{API}/projects/group%2Fname"),
])
def test_a_path_lands_under_the_api(path: str, url: str) -> None:
    assert api_target(API, path) == url


@pytest.mark.parametrize("path", [
    "https://evil.example.com/x", "//evil.example.com/x", "../../oauth/token", "a/%2e%2e/%2E%2E/b",
    "a\\b", "a b", "a\nb", "a\x00b",
])
def test_a_path_that_would_leave_the_api_is_refused(path: str) -> None:
    with pytest.raises(ToolError):
        api_target(API, path)


# --- the settings ------------------------------------------------------------------


def test_the_settings_read_the_api_address_and_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path, monkeypatch, CONNECTIONS_COMMAND="/Accounts")
    assert settings.api_url("gitlab") == API and settings.api_url("jira") == ""
    assert settings.command == "/accounts"
    monkeypatch.setenv("CONNECTION_API_URL__GITLAB", "https://api.example.com")
    assert ConnectedApiSettings(_env_file=str(tmp_path / ".env")).api_url("gitlab") == "https://api.example.com"  # type: ignore[call-arg]


def test_the_tools_are_advertised_only_where_connections_are() -> None:
    for cls in (ConnectedAccounts, ConnectedApiRead, ConnectedApiWrite):
        assert cls.requires == frozenset({CAP_CONNECTIONS})
        assert build_registry().get(cls.name) is not None
    assert ConnectedApiWrite.requires_confirmation and not ConnectedApiRead.requires_confirmation
    assert ConnectedApiRead.returns_untrusted and ConnectedApiWrite.returns_untrusted


# --- acting as the person -------------------------------------------------------------


async def test_a_read_carries_the_persons_token_to_the_api(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    result = await ConnectedApiRead().execute(
        _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "path": "user"},
    )
    assert result == {"status": 200, "body": {"route": "user", "body": None}}
    assert api.seen == [("GET", "/api/v4/user", "Bearer access-1", None)]
    assert "access-1" not in json.dumps(result)


async def test_a_write_sends_its_method_and_body(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    ctx = _ctx(service, _settings(tmp_path, monkeypatch))
    result = await ConnectedApiWrite().execute(
        ctx, {"provider": "gitlab", "method": "post", "path": "projects/1/issues", "body": {"title": "t"}},
    )
    assert result["status"] == 200 and api.seen[-1] == ("POST", "/api/v4/projects/1/issues", "Bearer access-1", {"title": "t"})
    with pytest.raises(ToolError, match="method"):
        await ConnectedApiWrite().execute(ctx, {"provider": "gitlab", "method": "GET", "path": "x"})
    with pytest.raises(ToolError, match="JSON object"):
        await ConnectedApiWrite().execute(ctx, {"provider": "gitlab", "method": "PUT", "path": "x", "body": [1]})


async def test_outside_a_direct_message_nothing_is_called(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    ctx = _ctx(service, _settings(tmp_path, monkeypatch), kind=KIND_CHANNEL)
    for tool, args in (
        (ConnectedApiRead(), {"provider": "gitlab", "path": "user"}),
        (ConnectedApiWrite(), {"provider": "gitlab", "method": "POST", "path": "user"}),
        (ConnectedAccounts(), {}),
    ):
        with pytest.raises(ToolError, match="direct message"):
            await tool.execute(ctx, args)
    assert api.seen == []


async def test_not_connected_sends_the_person_to_the_screen_never_for_a_token(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider(), connect=False)
    with pytest.raises(ToolError) as raised:
        await ConnectedApiRead().execute(
            _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "path": "user"},
        )
    assert "/connections" in str(raised.value) and "Never ask them for a token" in str(raised.value)
    assert api.seen == []


async def test_a_provider_the_agent_may_not_use_is_refused_before_any_call(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    ctx = _ctx(service, _settings(tmp_path, monkeypatch, CONNECTION_API_URL__JIRA=API), allowed=("jira",))
    with pytest.raises(ToolError, match="not a provider you may use"):
        await ConnectedApiRead().execute(ctx, {"provider": "gitlab", "path": "user"})
    with pytest.raises(ToolError, match="no API address"):
        await ConnectedApiRead().execute(
            _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "unknown", "path": "user"},
        )
    assert api.seen == []


async def test_a_401_ends_the_connection_and_asks_for_a_new_one(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    with pytest.raises(ToolError, match="connect again with /connections"):
        await ConnectedApiRead().execute(
            _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "path": "expired"},
        )
    assert await service.connected(OWNER) == {}


async def test_a_redirect_is_answered_not_followed(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    result = await ConnectedApiRead().execute(
        _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "path": "elsewhere"},
    )
    assert result["status"] == 302 and len(api.seen) == 1


async def test_a_long_answer_is_cut_and_says_so_and_text_stays_text(
    api: ProviderApi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    ctx = _ctx(service, _settings(tmp_path, monkeypatch))
    big = await ConnectedApiRead().execute(ctx, {"provider": "gitlab", "path": "big"})
    assert len(big["body"]) == MAX_RESPONSE_CHARS and str(MAX_RESPONSE_CHARS + 5) in big["truncated"]
    plain = await ConnectedApiRead().execute(ctx, {"provider": "gitlab", "path": "plain"})
    assert plain == {"status": 200, "body": "just text"}


async def test_an_api_that_does_not_answer_is_a_retry_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    with pytest.raises(ToolError, match="did not answer"):
        await ConnectedApiRead().execute(
            _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "path": "user"},
        )  # nothing listens on the port


async def test_the_accounts_list_says_who_is_connected_and_how_to_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    settings = _settings(tmp_path, monkeypatch)
    listed = await ConnectedAccounts().execute(_ctx(service, settings, allowed=("gitlab", "jira")), {})
    assert listed == {
        "accounts": [
            {"provider": "gitlab", "connected": True, "account": "alice"},
            {"provider": "jira", "connected": False},
        ],
        "connect_with": "/connections",
    }


async def test_the_card_shows_where_the_call_goes_and_as_whom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider())
    ctx = _ctx(service, _settings(tmp_path, monkeypatch))
    preview = await ConnectedApiWrite().describe(
        ctx, {"provider": "gitlab", "method": "delete", "path": "projects/1", "body": {"why": "old"}},
    )
    assert preview is not None and preview.title == "DELETE at gitlab as you" and preview.danger
    rows = {row.label: row.value for row in preview.rows}
    assert rows == {
        "Account": "alice at gitlab",
        "Request": f"DELETE {API}/projects/1",
        "Body": '{\n  "why": "old"\n}',
    }
    post = await ConnectedApiWrite().describe(ctx, {"provider": "gitlab", "method": "POST", "path": "x"})
    assert post is not None and not post.danger and "Body" not in {r.label for r in post.rows}
    # A call execute() would refuse has no account of its own to show.
    for args in (
        {"provider": "gitlab", "method": "POST", "path": "https://evil.example.com/"},
        {"provider": "gitlab", "method": "GET", "path": "x"},
        {"provider": "unknown", "method": "POST", "path": "x"},
    ):
        assert await ConnectedApiWrite().describe(ctx, args) is None


async def test_the_card_says_when_there_is_no_account_to_act_as(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await _connected(FakeProvider(), connect=False)
    preview = await ConnectedApiWrite().describe(
        _ctx(service, _settings(tmp_path, monkeypatch)), {"provider": "gitlab", "method": "PUT", "path": "x"},
    )
    assert preview is not None and preview.rows[0].value == "not connected to gitlab"


# --- offered in the conversation, and told where it was offered -----------------------


async def test_not_connected_posts_the_connect_offer_and_the_connect_redraws_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole round as the person sees it: the agent's call finds no
    account, the engine posts that provider's Connect button in the DM, the
    click hands the link to the clicker alone, and once the connect is stored
    the same message says it worked."""
    from crucible.interactions import InteractionDispatcher
    from crucible.interactions.pending_ui import PendingUiRequests
    from crucible.interactions.screens import ScreenRegistry
    from crucible.interactions.service import InteractionService
    from crucible.store.sqlite import SqliteSessionStore
    from impi.connections import RedrawOnConnect
    from impi.connections_screen import ConnectionsScreen
    from tests.fakes.fake_chat import FakeChat
    from tests.fakes.presence import presence_of

    store = SqliteSessionStore(tmp_path / "db.sqlite")
    provider, chat, redraw = FakeProvider(), FakeChat(), RedrawOnConnect()
    service = OAuthConnections(
        connections=store, links=store, providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]), on_connected=redraw,
    )
    screens = ScreenRegistry()
    screens.register(ConnectionsScreen(
        service, owner_for=lambda agent, user: f"mattermost:{user}", providers_for=lambda agent: ("gitlab",),
    ))
    interactions = InteractionService(presence_of(chat), store, store, store, callback_url="cb", screens=screens)
    redraw.dispatcher = InteractionDispatcher(
        store, presence_of(chat), PendingUiRequests(), store, screens=screens, callback_url="cb",
    )
    try:
        dm, _ = await store.get_or_create("assistant", "dm1", "dm1", KIND_DM, user_id="u1")
        ctx = ToolContext(
            agent_name="assistant", directory=FakeDirectory(AGENTS), settings=_settings(tmp_path, monkeypatch),
            conversation_kind=KIND_DM, runtime_session_id=dm.runtime_session_id, user_id="u1",
            interaction_svc=interactions, connections=NarrowedConnections(service.for_owner(OWNER), ("gitlab",)),
        )

        with pytest.raises(ToolError, match="Connect button for it is now in this conversation"):
            await ConnectedApiRead().execute(ctx, {"provider": "gitlab", "path": "user"})
        ref, cards, _ = chat.posted_cards[0]
        assert ref.channel_id == "dm1" and len(cards) == 1 and "Connect gitlab" in cards[0].text
        connect = cards[0].actions[0]
        assert connect.label == "Connect" and connect.value == "connect:gitlab"

        redrawn = await redraw.dispatcher.redraw_screen(
            connect.context["state"], connect.value, post_id="offer-1", user_id="u1",
        )
        assert redrawn is not None and "yours alone" in redrawn.private_note
        assert all("http" not in card.text for _, cs in chat.updated for card in cs)

        state, code = provider.consent(redrawn.private_note.rsplit(" ", 1)[1])
        await service.finish(state, code)

        post_id, final = chat.updated[-1]
        assert post_id == "offer-1" and "✅ Connected to gitlab as **alice**" in final[0].text
        assert final[0].actions == ()  # nothing left to press
    finally:
        await store.close()


async def test_without_a_way_to_post_the_model_sends_the_person_to_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    service = await _connected(FakeProvider(), connect=False)

    class Refusing:
        async def open_screen(self, *args, **kwargs) -> bool:
            raise RuntimeError("chat is down")

    for interactions, warned in ((None, False), (Refusing(), True)):
        caplog.clear()
        ctx = _ctx(service, _settings(tmp_path, monkeypatch))
        ctx.interaction_svc = interactions  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING), pytest.raises(ToolError, match="Ask them to open /connections"):
            await ConnectedApiRead().execute(ctx, {"provider": "gitlab", "path": "user"})
        # No interactivity is a deployment's choice, not a failure to report.
        assert ("could not post" in caplog.text) is warned
