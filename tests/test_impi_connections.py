"""impi's connections: the CONNECTION_*__<NAME> settings, whose connections a
turn uses and which account may be bound to whom, the /connections screen, and
what health and startup say about it all."""

from pathlib import Path

import aiohttp
import pytest

from crucible.connections import (
    Connected,
    ConnectionsError,
    OAuthConnections,
    ProviderAccount,
)
from crucible.connections.sealing import FernetSealer, new_sealing_key
from crucible.gateways import GatewayConfig
from crucible.interactions.screens import ScreenState
from crucible.ports.chat.types import UserProfile
from crucible.ports.turn import ENGINE_TURN_ATTRIBUTE, TurnScope
from crucible.skills import SkillLibrary
from impi import cli
from impi.config import ImpiSettings
from impi.connections import (
    ChatEmailPolicy,
    Connections,
    NarrowedConnections,
    RedrawOnConnect,
    build_connections,
    owner_of,
)
from impi.connections_config import connection_providers
from impi.connections_screen import ConnectionsScreen, ConnectReturn
from impi.deployment_report import connections_report
from tests.fakes.connections import MemoryConnectionStore, MemoryLinkStore
from tests.test_connections import ALICE, FakeProvider

GITLAB = {
    "CONNECTION_CLIENT_ID__GITLAB": "client",
    "CONNECTION_CLIENT_SECRET__GITLAB": "secret",
    "CONNECTION_BASE_URL__GITLAB": "https://gitlab.example.com/",
}


def _settings(tmp_path: Path, **values) -> ImpiSettings:
    env = tmp_path / ".env"
    if not env.exists():
        env.write_text("")
    return ImpiSettings(_env_file=None, dotenv_path=str(env), **values)  # pyright: ignore[reportCallIssue]


# --- settings --------------------------------------------------------------------


def test_a_gitlab_provider_needs_only_its_client_and_address() -> None:
    gitlab = connection_providers(GITLAB)["gitlab"]

    assert (gitlab.kind, gitlab.base_url) == ("gitlab", "https://gitlab.example.com")
    assert gitlab.api_url == "https://gitlab.example.com/api/v4"
    assert gitlab.match_email is True and gitlab.agents is None and gitlab.allows("anyone")
    config = gitlab.oauth2_config("https://engine.example.com/oauth/callback")
    assert config.authorize_url == "https://gitlab.example.com/oauth/authorize"
    assert config.redirect_uri == "https://engine.example.com/oauth/callback"


def test_a_name_is_the_suffix_and_each_field_is_read_under_it() -> None:
    values = {
        "CONNECTION_CLIENT_ID__SELF_HOSTED": "c",
        "CONNECTION_CLIENT_SECRET__SELF_HOSTED": "s",
        "CONNECTION_KIND__SELF_HOSTED": "OAuth2",
        "CONNECTION_AUTHORIZE_URL__SELF_HOSTED": "https://id.example.com/authorize",
        "CONNECTION_TOKEN_URL__SELF_HOSTED": "https://id.example.com/token",
        "CONNECTION_USERINFO_URL__SELF_HOSTED": "https://id.example.com/userinfo",
        "CONNECTION_API_URL__SELF_HOSTED": "https://api.example.com",
        "CONNECTION_SCOPE__SELF_HOSTED": "read",
        "CONNECTION_MATCH_EMAIL__SELF_HOSTED": "off",
        "CONNECTION_AGENTS__SELF_HOSTED": "assistant, helper,",
    }
    provider = connection_providers(values)["self-hosted"]

    assert provider.kind == "oauth2" and provider.api_url == "https://api.example.com"
    assert provider.match_email is False and provider.agents == ("assistant", "helper")
    assert provider.allows("helper") and not provider.allows("support")
    assert provider.oauth2_config("r").token_url == "https://id.example.com/token"
    assert provider.oauth2_config("r").scope == "read"


def test_an_empty_agents_list_lets_no_agent_use_it() -> None:
    provider = connection_providers({**GITLAB, "CONNECTION_AGENTS__GITLAB": ""})["gitlab"]
    assert provider.agents == () and not provider.allows("assistant")


def test_a_client_id_left_empty_configures_nothing() -> None:
    assert connection_providers({**GITLAB, "CONNECTION_CLIENT_ID__GITLAB": " "}) == {}


@pytest.mark.parametrize(("change", "names"), [
    ({"CONNECTION_CLIENT_SECRET__GITLAB": ""}, "CONNECTION_CLIENT_SECRET__GITLAB"),
    ({"CONNECTION_BASE_URL__GITLAB": ""}, "CONNECTION_BASE_URL__GITLAB"),
    ({"CONNECTION_KIND__GITLAB": "github"}, "CONNECTION_KIND__GITLAB"),
    ({"CONNECTION_KIND__GITLAB": "oauth2"}, "CONNECTION_AUTHORIZE_URL__GITLAB"),
    ({"CONNECTION_MATCH_EMAIL__GITLAB": "maybe"}, "CONNECTION_MATCH_EMAIL__GITLAB"),
])
def test_a_provider_that_cannot_work_names_the_key(change: dict, names: str) -> None:
    with pytest.raises(ValueError, match=names):
        connection_providers({**GITLAB, **change})


def test_the_environment_wins_over_the_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("\n".join(f"{k}={v}" for k, v in GITLAB.items()))
    monkeypatch.setenv("CONNECTION_BASE_URL__GITLAB", "https://other.example.com")
    settings = _settings(tmp_path, connections_keys=" k1 , k2,", connections_public_url="https://e.example.com/")

    assert settings.connection_providers()["gitlab"].base_url == "https://other.example.com"
    assert settings.sealing_keys() == ("k1", "k2")
    assert settings.connections_redirect_uri == "https://e.example.com/oauth/callback"
    assert "k1" not in repr(settings)


# --- whose, and which account ------------------------------------------------------


class ProfileChat:
    def __init__(self, email: str | None) -> None:
        self.email = email
        self.asked: list[str] = []

    async def get_user_profile(self, user_id: str) -> UserProfile | None:
        self.asked.append(user_id)
        return None if self.email is None else UserProfile(username="u", email=self.email)


def test_an_owner_is_the_user_on_the_gateway_they_were_seen_on() -> None:
    assert owner_of("mattermost", "u1") == "mattermost:u1"
    assert owner_of("mattermost", "") is None and owner_of("", "u1") is None


@pytest.mark.parametrize(("chat_email", "account_email", "refused"), [
    ("Alice@Example.com ", "alice@example.com", None),
    ("alice@example.com", " ALICE@example.COM", None),
    ("bob@example.com", "alice@example.com", "its email is not the one your chat account has"),
    ("alice@example.com", "", "its email is not the one your chat account has"),
    ("", "alice@example.com", "your chat account shows no email to compare it with"),
    (None, "alice@example.com", "your chat account shows no email to compare it with"),
])
async def test_the_accounts_email_must_be_the_chat_users(
    chat_email: str | None, account_email: str, refused: str | None,
) -> None:
    chat = ProfileChat(chat_email)
    policy = ChatEmailPolicy(connection_providers(GITLAB), {"mattermost": chat}.get)  # type: ignore[arg-type]
    account = ProviderAccount(id="7", username="alice", email=account_email)

    assert await policy.refusal("mattermost:u1", "gitlab", account, {}) == refused
    assert chat.asked == ["u1"]


async def test_no_client_for_the_gateway_is_no_email() -> None:
    policy = ChatEmailPolicy(connection_providers(GITLAB), lambda gateway: None)
    assert await policy.refusal("slack:U1", "gitlab", ALICE, {}) == "your chat account shows no email to compare it with"


async def test_a_provider_that_does_not_match_emails_takes_any_account() -> None:
    chat = ProfileChat("bob@example.com")
    providers = connection_providers({**GITLAB, "CONNECTION_MATCH_EMAIL__GITLAB": "false"})
    policy = ChatEmailPolicy(providers, {"mattermost": chat}.get)  # type: ignore[arg-type]

    assert await policy.refusal("mattermost:u1", "gitlab", ALICE, {}) is None
    assert await policy.refusal("mattermost:u1", "unknown", ALICE, {}) is None
    assert chat.asked == []  # nobody's profile read for nothing


class RecordingAccess:
    providers = ("gitlab", "jira")

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def grant(self, provider):
        self.calls.append(("grant", provider))
        return "grant"

    async def forget(self, grant):
        self.calls.append(("forget", grant))

    async def account(self, provider):
        self.calls.append(("account", provider))
        return ALICE


async def test_an_agent_sees_only_the_providers_it_may_use() -> None:
    inner = RecordingAccess()
    narrowed = NarrowedConnections(inner, ("gitlab",))  # type: ignore[arg-type]

    assert narrowed.providers == ("gitlab",)
    assert await narrowed.grant("gitlab") == "grant" and await narrowed.account("gitlab") == ALICE
    with pytest.raises(ConnectionsError, match="jira"):
        await narrowed.grant("jira")
    with pytest.raises(ConnectionsError, match="jira"):
        await narrowed.account("jira")
    assert inner.calls == [("grant", "gitlab"), ("account", "gitlab")]


# --- building it -------------------------------------------------------------------


MM = GatewayConfig(kind="mattermost")


def test_no_provider_builds_nothing(tmp_path: Path) -> None:
    assert build_connections(_settings(tmp_path), MemoryConnectionStore(), {}, lambda a: None) is None  # type: ignore[arg-type]


def test_a_provider_without_keys_or_a_callback_address_stops_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in GITLAB.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="CONNECTIONS_KEYS"):
        build_connections(_settings(tmp_path, connections_public_url="https://e.example.com"),
                          object(), {}, lambda a: None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="CONNECTIONS_PUBLIC_URL"):
        build_connections(_settings(tmp_path, connections_keys=new_sealing_key()),
                          object(), {}, lambda a: None)  # type: ignore[arg-type]


class Both(MemoryConnectionStore, MemoryLinkStore):
    def __init__(self) -> None:
        MemoryConnectionStore.__init__(self)
        MemoryLinkStore.__init__(self)


async def test_a_built_service_acts_for_the_chat_user_of_the_agent_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {**GITLAB, "CONNECTION_AGENTS__GITLAB": "assistant"}.items():
        monkeypatch.setenv(key, value)
    chat = ProfileChat("alice@example.com")
    configs = {"assistant": MM, "helper": MM, "idle": None}
    connections = build_connections(
        _settings(tmp_path, connections_keys=new_sealing_key(), connections_public_url="https://e.example.com",
                  connections_host="127.0.0.1", connections_port=8620),
        Both(), configs, {"helper": chat}.get,  # type: ignore[arg-type]
    )
    assert connections is not None

    assert connections.owner_for("assistant", "u1") == "mattermost:u1"
    assert connections.owner_for("idle", "u1") is None
    assert connections.providers_for("assistant") == ("gitlab",) and connections.providers_for("helper") == ()
    access = connections.tool_access()
    granted = access("assistant", "u1", None)
    assert granted is not None and granted.providers == ("gitlab",)
    assert access("helper", "u1", None) is None  # no provider for it
    assert access("idle", "u1", None) is None  # nobody it can name
    await connections.callback.start()  # where the settings put it
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:8620/oauth/callback") as resp:
                assert resp.status == 400
    finally:
        await connections.callback.stop()
    # The email is read through the client of any agent on the owner's gateway.
    policy = connections.service._policy  # noqa: SLF001
    assert policy is not None and await policy.refusal("mattermost:u9", "gitlab", ALICE, {}) is None
    assert chat.asked == ["u9"]


# --- the screen --------------------------------------------------------------------


def _text(view) -> str:
    return "\n".join(c.text for c in view.cards)


def _screen(provider: FakeProvider, *, providers=("gitlab",), owner="mattermost:u1"):
    service = OAuthConnections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": provider},
        sealer=FernetSealer([new_sealing_key()]),
    )
    screen = ConnectionsScreen(
        service, owner_for=lambda agent, user: owner if user else None,
        providers_for=lambda agent: providers, command="/Accounts",
    )
    return service, screen


def _state(value: str = "") -> ScreenState:
    return ScreenState(screen="accounts", agent="assistant", data={"value": value} if value else {})


async def test_the_screen_opens_only_in_a_direct_message_and_binds_its_command() -> None:
    _, screen = _screen(FakeProvider())
    assert screen.direct_only is True and screen.command == "accounts"


async def test_an_agent_with_no_provider_says_so() -> None:
    _, screen = _screen(FakeProvider(), providers=())
    view = await screen.render(_state(), user_id="u1")
    assert "uses no connected accounts" in _text(view) and not view.cards[0].actions


async def test_connect_hands_the_link_to_the_clicker_alone() -> None:
    provider = FakeProvider()
    service, screen = _screen(provider)

    index = await screen.render(_state(), user_id="u1")
    assert "**gitlab** — not connected" in _text(index)
    connect = index.cards[1].actions[0]
    assert connect.label == "Connect" and connect.value == "connect:gitlab"

    view = await screen.render(_state("connect:gitlab"), user_id="u1")
    assert "oauth" not in _text(view).lower()  # the link is not in the message
    assert "yours alone, works once, and expires in 10 minutes" in view.private_note
    url = view.private_note.rsplit(" ", 1)[1]
    state, code = provider.consent(url)
    await service.finish(state, code)

    connected = await screen.render(_state(), user_id="u1")
    assert "connected as **alice**" in _text(connected)
    assert connected.cards[1].actions[0].value == "disconnect:gitlab" and connected.private_note == ""


async def test_a_provider_the_agent_may_not_use_is_not_acted_on() -> None:
    service, screen = _screen(FakeProvider(), providers=("jira",))
    view = await screen.render(_state("connect:gitlab"), user_id="u1")
    assert view.private_note == "" and "gitlab" not in _text(view)


@pytest.mark.parametrize(("revoke_error", "says"), [
    (None, "Disconnected from gitlab."),
    (ConnectionsError("down"), "did not confirm the revoke"),
])
async def test_disconnect_says_whether_the_provider_confirmed(revoke_error, says: str) -> None:
    provider = FakeProvider()
    service, screen = _screen(provider)
    state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
    await service.finish(state, code)
    provider.revoke_error = revoke_error

    view = await screen.render(_state("disconnect:gitlab"), user_id="u1")

    assert says in _text(view) and "**gitlab** — not connected" in _text(view)
    assert "Not connected" in _text(await screen.render(_state("disconnect:gitlab"), user_id="u1"))


async def test_a_provider_that_does_not_answer_is_a_note_not_a_crash() -> None:
    service, screen = _screen(FakeProvider())

    async def down(owner, provider, **kwargs):
        raise ConnectionsError("down")

    service.begin = down  # type: ignore[method-assign]
    view = await screen.render(_state("connect:gitlab"), user_id="u1")
    assert "gitlab did not answer" in _text(view) and view.private_note == ""


# --- what health and startup say -----------------------------------------------------


def test_the_report_names_providers_and_the_redirect_to_register(tmp_path: Path) -> None:
    settings = _settings(tmp_path, connections_keys="k", connections_public_url="https://e.example.com")
    providers = connection_providers({**GITLAB, "CONNECTION_AGENTS__GITLAB": "assistant"})

    report = connections_report(settings, providers, {"assistant": MM}, tools_on=True, widgets_on=True)

    assert report.line is not None and "gitlab (agents: assistant)" in report.line
    assert "https://e.example.com/oauth/callback" in report.line and "/connections" in report.line
    assert report.errors == [] and report.warnings == []
    assert connections_report(settings, {}, {}, tools_on=True, widgets_on=True).line is None


def test_the_report_says_what_keeps_a_connect_from_working(tmp_path: Path) -> None:
    settings = _settings(tmp_path, connections_public_url="http://engine.example.com")
    providers = connection_providers({**GITLAB, "CONNECTION_AGENTS__GITLAB": "assistant,gone"})
    configs = {"assistant": GatewayConfig(kind="http")}

    report = connections_report(settings, providers, configs, tools_on=False, widgets_on=False)

    assert [e.split(" ")[0] for e in report.errors] == ["CONNECTIONS_KEYS"]
    text = "\n".join(report.warnings)
    assert "travels in clear" in text and "INTEGRATIONS_ENABLED" in text and "TOOL_ENABLED" in text
    assert "does not run: gone" in text and "gitlab is not offered to assistant" in text
    assert len(report.warnings) == 5
    assert "CONNECTIONS_PUBLIC_URL" in "\n".join(
        connections_report(_settings(tmp_path, connections_keys="k"), providers, {},
                           tools_on=True, widgets_on=True).errors
    )


def test_a_loopback_callback_is_not_warned_about(tmp_path: Path) -> None:
    settings = _settings(tmp_path, connections_keys="k", connections_public_url="http://localhost:8429")
    report = connections_report(settings, connection_providers(GITLAB), {}, tools_on=True, widgets_on=True)
    assert report.warnings == []


# --- impi connections key ----------------------------------------------------------


def test_a_new_key_goes_first_and_the_old_ones_keep_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / ".env"
    old = new_sealing_key()
    env.write_text(f"CONNECTIONS_KEYS={old}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DOTENV_PATH", str(env))
    monkeypatch.delenv("CONNECTIONS_KEYS", raising=False)
    sealed = FernetSealer([old]).seal("v")

    assert cli.main(["connections", "key"]) == 0

    keys = next(line for line in env.read_text().splitlines() if line.startswith("CONNECTIONS_KEYS="))
    new, kept = keys.split("=", 1)[1].strip("'\"").split(",")
    assert kept == old and new != old
    assert FernetSealer([new, kept]).open(sealed) == "v"  # nobody connects again
    assert FernetSealer([new]).open(FernetSealer([new, kept]).seal("w")) == "w"  # the new one seals


# --- the offer, and saying it worked ----------------------------------------------------


async def test_an_offer_shows_one_provider_and_its_connect_remembers_the_message() -> None:
    provider = FakeProvider()
    service, screen = _screen(provider, providers=("gitlab", "jira"))
    begun: list[str] = []
    real_begin = service.begin

    async def begin(owner, name, *, return_to=""):
        begun.append(return_to)
        return await real_begin(owner, name, return_to=return_to)

    service.begin = begin  # type: ignore[method-assign]
    offer = ScreenState(screen="accounts", agent="assistant", data={"focus": "gitlab"})

    view = await screen.render(offer, user_id="u1")
    assert len(view.cards) == 1 and "Connect gitlab" in view.cards[0].text and "jira" not in _text(view)

    clicked = ScreenState(screen="accounts", agent="assistant", data={"focus": "gitlab", "value": "connect:gitlab"},
                          post_id="p1")
    await screen.render(clicked, user_id="u1")
    back = ConnectReturn.decode(begun[-1])
    assert back is not None and back.post_id == "p1"
    redrawn_from = ScreenState.decode(back.state)
    assert redrawn_from is not None and redrawn_from.data == {"focus": "gitlab", "value": ""}

    await screen.render(ScreenState(screen="accounts", agent="assistant", data={"value": "connect:gitlab"}),
                        user_id="u1")
    assert begun[-1] == ""  # no message to come back to: nothing remembered


async def test_connected_is_said_only_when_the_store_agrees() -> None:
    provider = FakeProvider()
    service, screen = _screen(provider)
    said = ScreenState(screen="accounts", agent="assistant", data={"focus": "gitlab", "value": "connected:gitlab"})

    forged = await screen.render(said, user_id="u1")
    assert "Connected" not in _text(forged) and forged.cards[0].actions  # still an offer

    state, code = provider.consent(await service.begin("mattermost:u1", "gitlab"))
    await service.finish(state, code)
    offer = await screen.render(said, user_id="u1")
    assert "✅ Connected to gitlab as **alice**" in _text(offer) and offer.cards[0].actions == ()
    panel = await screen.render(_state("connected:gitlab"), user_id="u1")
    assert "✅ Connected to gitlab as **alice**" in panel.cards[0].text  # the panel says it in its header


async def test_a_connect_is_redrawn_where_it_started_and_nowhere_else() -> None:
    class Dispatcher:
        def __init__(self) -> None:
            self.redraws: list[tuple] = []

        async def redraw_screen(self, state, value, *, post_id, user_id):
            self.redraws.append((state, value, post_id, user_id))

    redraw = RedrawOnConnect()
    back = ConnectReturn(post_id="p1", state='{"n":"connections"}').encode()
    connected = Connected(owner="mattermost:u1", provider="gitlab", account=ALICE, return_to=back)
    await redraw(connected)  # not wired yet: nothing to draw with
    redraw.dispatcher = dispatcher = Dispatcher()  # type: ignore[assignment]

    await redraw(connected)
    await redraw(Connected(owner="mattermost:u1", provider="gitlab", account=ALICE))
    await redraw(Connected(owner="mattermost:u1", provider="gitlab", account=ALICE, return_to="{not json"))

    assert dispatcher.redraws == [('{"n":"connections"}', "connected:gitlab", "p1", "u1")]
    assert ConnectReturn.decode('{"p":"","s":"x"}') is None and ConnectReturn.decode("[]") is None


# --- nobody's connections where nobody can be told apart, or nobody is there -----


def _connections(**gateways: str):
    service = OAuthConnections(
        connections=MemoryConnectionStore(), links=MemoryLinkStore(), providers={"gitlab": FakeProvider()},
        sealer=FernetSealer([new_sealing_key()]),
    )
    return Connections(
        service=service, callback=None,  # type: ignore[arg-type]  # not started here
        providers=connection_providers(GITLAB), gateways=gateways,
    )


def test_on_a_gateway_whose_user_ids_the_caller_asserts_nobody_is_an_owner() -> None:
    connections = _connections(chat="mattermost", web="http", program="ws", team="slack")
    assert connections.owner_for("chat", "alice") == "mattermost:alice"
    assert connections.owner_for("team", "U1") == "slack:U1"
    assert connections.owner_for("web", "alice") is None
    assert connections.owner_for("program", "alice") is None
    assert connections.owner_for("unknown", "alice") is None
    access = connections.tool_access()
    assert access("web", "alice", None) is None and access("chat", "alice", None) is not None


def test_a_scheduled_turn_acts_as_nobody() -> None:
    connections = _connections(chat="mattermost")
    scheduled = TurnScope(turn_id="t", attributes={ENGINE_TURN_ATTRIBUTE: "1"})
    assert connections.owner_for("chat", "alice", scheduled) is None
    assert connections.tool_access()("chat", "alice", scheduled) is None
    assert connections.owner_for("chat", "alice", TurnScope(turn_id="t")) == "mattermost:alice"


async def test_a_scheduler_turn_says_it_is_one() -> None:
    from crucible.flows.coalescer import MessageCoalescer
    from crucible.interactions import AgentSink
    from crucible.ports.engine_turn import TurnRequest
    from impi.scheduling import SinkTurnDispatcher

    class Tracked(MessageCoalescer):
        def __init__(self) -> None:
            self.messages: list = []

        async def submit_tracked(self, message, chat):  # type: ignore[override]
            self.messages.append(message)
            return "ok"

    sink = Tracked()
    dispatcher = SinkTurnDispatcher({"chat": AgentSink(sink=sink, gateways={"mattermost": object()}, default="mattermost")})  # type: ignore[arg-type]
    await dispatcher.run_turn(TurnRequest(
        agent="chat", channel_id="d1", conversation_id="d1", kind="dm", text="do it",
        message_id="sched-1", user_id="u1", username="u",
    ))
    turn = sink.messages[0].turn
    assert turn is not None and turn.attributes.get(ENGINE_TURN_ATTRIBUTE)


def test_an_agent_may_not_inherit_the_connection_secrets_by_pattern(tmp_path: Path) -> None:
    from crucible.ports.agent.inherited_env import check_inherited_env

    for pattern in ("CONNECTION_*", "CONNECTIONS_*", "CONN*"):
        with pytest.raises(ValueError, match="never by a pattern"):
            check_inherited_env(pattern, reserved=ImpiSettings.APP_SECRET_PREFIXES)
    check_inherited_env("CONNECT_ME", reserved=ImpiSettings.APP_SECRET_PREFIXES)  # a name, not a family
    with pytest.raises(ValueError, match="RUNTIME_ENV"):
        _settings(tmp_path, runtime_env="CONNECTION*")
    with pytest.raises(ValueError, match="AGENTS_ENV__ASSISTANT"):
        (tmp_path / ".env").write_text("AGENTS_ENV__ASSISTANT=CONNECTIONS_*\n")
        _settings(tmp_path).inherited_env_for("assistant")


def test_a_profile_may_not_name_them_either(tmp_path: Path) -> None:
    from crucible.profiles.errors import ProfileError
    from impi.profiles import open_profile_stores

    agent = tmp_path / "profiles" / "agents" / "assistant"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("name: assistant\nruntime:\n  tools: [read]\n  env: [CONNECTION*]\n")
    with pytest.raises(ProfileError, match="never by a pattern"):
        user_store, _ = open_profile_stores(
            _settings(tmp_path, agents_path=str(tmp_path / "profiles")), SkillLibrary(tmp_path / "skills"),
        )
        user_store.get("assistant")
