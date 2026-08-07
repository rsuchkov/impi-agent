import aiohttp
import pytest

import crucible.builtin_tools  # noqa: F401  # registers the generic ask/form tools
from crucible.ports.chat.admin import ChannelMember
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.types import PostSnippet
from crucible.tools.base import ToolContext, ToolError
from crucible.tools.registry import build_registry
from crucible.tools.server import ToolServer
from impi.agent_tools import CreateAgent
from impi.chat_tools import (
    CreateChannel,
    CreateChannelSettings,
    InviteToChannel,
    ListAgents,
    ReadChannel,
    SendMessage,
)
from impi.provisioning import BotCredentials, CreateAgentSettings


class FakeDirectory:
    def __init__(self, agents: list[AgentInfo]) -> None:
        self._agents = agents

    def agent_user_ids(self):
        return frozenset(a.user_id for a in self._agents)

    def list_agents(self):
        return list(self._agents)


class FakeAdmin:
    def __init__(self) -> None:
        self.created: list[tuple] = []
        self.invited: list[tuple] = []
        self.posted: list[tuple] = []  # (channel_id, message, hop_depth)
        self.ephemeral: list[tuple] = []  # (channel_id, user_id, message)
        self.channel_posts: list[PostSnippet] = []

    async def create_channel(self, name, display_name, *, private=True, purpose=""):
        self.created.append((name, display_name, private, purpose))
        return "chan-123"

    async def invite_to_channel(self, channel_id, user_id):
        self.invited.append((channel_id, user_id))

    async def get_channel_members(self, channel_id):
        return [ChannelMember(user_id="u1", username="roman")]

    async def resolve_username(self, username):
        return "uid-" + username.lstrip("@")

    async def post_message(self, channel_id, message, *, hop_depth=0):
        self.posted.append((channel_id, message, hop_depth))
        return "post-1"

    async def get_channel_posts(self, channel_id, limit=20):
        return self.channel_posts[:limit]

    async def post_ephemeral(self, channel_id, user_id, message):
        self.ephemeral.append((channel_id, user_id, message))


AGENTS = [
    AgentInfo("assistant", "personal-assistant", "helps", "r42-assistant", "uid-a"),
    AgentInfo("developer", "developer", "codes", "r42-developer", "uid-d"),
]


def _ctx(admin: FakeAdmin) -> ToolContext:
    return ToolContext(agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=admin)


async def test_list_agents_returns_registry() -> None:
    result = await ListAgents().execute(_ctx(FakeAdmin()), {})
    names = [a["name"] for a in result["agents"]]
    assert names == ["assistant", "developer"]
    assert result["agents"][0]["username"] == "r42-assistant"


async def test_create_channel_uses_calling_admin() -> None:
    admin = FakeAdmin()
    result = await CreateChannel().execute(
        _ctx(admin), {"display_name": "Проект X", "private": True}
    )
    assert result["channel_id"] == "chan-123"
    assert admin.created[0][1] == "Проект X"


async def test_create_channel_requires_display_name() -> None:
    try:
        await CreateChannel().execute(_ctx(FakeAdmin()), {})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_invite_resolves_agent_name_to_user_id() -> None:
    admin = FakeAdmin()
    await InviteToChannel().execute(
        _ctx(admin), {"channel_id": "chan-123", "target": "developer"}
    )
    assert admin.invited == [("chan-123", "uid-d")]  # resolved via registry


async def test_invite_falls_back_to_username_lookup() -> None:
    admin = FakeAdmin()
    await InviteToChannel().execute(
        _ctx(admin), {"channel_id": "chan-123", "target": "someone-else"}
    )
    assert admin.invited == [("chan-123", "uid-someone-else")]  # via resolve_username


async def test_send_message_posts_with_seed_hop() -> None:
    admin = FakeAdmin()
    result = await SendMessage().execute(
        _ctx(admin), {"channel_id": "chan-1", "message": "@r42-developer привет"}
    )
    assert result == {"posted": True, "post_id": "post-1"}
    # Seeds the cascade one hop in so an agent it @-mentions stays loop-bounded.
    assert admin.posted == [("chan-1", "@r42-developer привет", 1)]


async def test_send_message_requires_channel_and_text() -> None:
    for bad in ({"channel_id": "c"}, {"message": "hi"}):
        try:
            await SendMessage().execute(_ctx(FakeAdmin()), bad)
            raise AssertionError("expected ToolError")
        except ToolError:
            pass


async def test_send_message_wraps_post_failure_as_tool_error() -> None:
    class Boom(FakeAdmin):
        async def post_message(self, channel_id, message, *, hop_depth=0):
            raise RuntimeError("403 not a member")

    try:
        await SendMessage().execute(_ctx(Boom()), {"channel_id": "c", "message": "hi"})
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "member" in str(exc)


async def test_read_channel_returns_author_and_text() -> None:
    admin = FakeAdmin()
    admin.channel_posts = [
        PostSnippet(message_id="p1", username="roman", text="a question"),
        PostSnippet(message_id="p2", username="r42-developer", text="the answer"),
    ]
    result = await ReadChannel().execute(_ctx(admin), {"channel_id": "chan-1"})
    assert result["messages"] == [
        {"author": "roman", "text": "a question"},
        {"author": "r42-developer", "text": "the answer"},
    ]


async def test_read_channel_clamps_bad_limit_to_default() -> None:
    admin = FakeAdmin()
    admin.channel_posts = [PostSnippet(message_id=f"p{i}", username="u", text=str(i)) for i in range(30)]
    # limit 0 is out of range -> falls back to 20.
    result = await ReadChannel().execute(_ctx(admin), {"channel_id": "c", "limit": 0})
    assert len(result["messages"]) == 20


# --- HTTP server (auth + dispatch) -----------------------------------------


async def _server(port: int) -> tuple[ToolServer, FakeAdmin]:
    admin = FakeAdmin()
    reg = build_registry()
    server = ToolServer(
        reg,
        directory=FakeDirectory(AGENTS),
        admins={"assistant": admin},
        tokens={"secret-tok": "assistant"},
        allowlists={"assistant": frozenset(reg.names())},  # assistant may call all
        port=port,
    )
    await server.start()
    return server, admin


async def test_server_runs_tool_with_valid_token() -> None:
    server, admin = await _server(8461)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8461/tool/create_channel",
                json={"display_name": "War Room"},
                headers={"X-Tool-Token": "secret-tok"},
            ) as resp:
                assert resp.status == 200
                body = await resp.json()
                assert body["result"]["channel_id"] == "chan-123"
        assert admin.created[0][1] == "War Room"
    finally:
        await server.stop()


async def test_server_rejects_bad_token() -> None:
    server, _ = await _server(8462)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8462/tool/list_agents",
                json={},
                headers={"X-Tool-Token": "wrong"},
            ) as resp:
                assert resp.status == 401
    finally:
        await server.stop()


async def test_server_unknown_tool_is_404() -> None:
    server, _ = await _server(8463)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8463/tool/nope",
                json={},
                headers={"X-Tool-Token": "secret-tok"},
            ) as resp:
                assert resp.status == 404
    finally:
        await server.stop()


async def test_server_forbids_tool_outside_agent_allowlist() -> None:
    # A valid token only authorizes the agent's OWN tools — an existing tool the
    # agent doesn't allowlist is 403, not run (server-side enforcement).
    admin = FakeAdmin()
    server = ToolServer(
        build_registry(),
        directory=FakeDirectory(AGENTS),
        admins={"assistant": admin},
        tokens={"tok": "assistant"},
        allowlists={"assistant": frozenset({"list_agents"})},  # NOT create_channel
        port=8466,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8466/tool/list_agents", json={},
                headers={"X-Tool-Token": "tok"},
            ) as resp:
                assert resp.status == 200  # allowlisted -> runs
            async with s.post(
                "http://127.0.0.1:8466/tool/create_channel", json={"display_name": "X"},
                headers={"X-Tool-Token": "tok"},
            ) as resp:
                assert resp.status == 403  # exists but not allowlisted -> refused
        assert admin.created == []  # never reached execute
    finally:
        await server.stop()


async def test_server_tool_error_is_400() -> None:
    server, _ = await _server(8464)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8464/tool/create_channel",
                json={},  # missing display_name -> ToolError
                headers={"X-Tool-Token": "secret-tok"},
            ) as resp:
                assert resp.status == 400
                assert "display_name" in (await resp.json())["error"]
    finally:
        await server.stop()


def _ctx_owner(admin: FakeAdmin) -> ToolContext:
    return ToolContext(
        agent_name="assistant",
        directory=FakeDirectory(AGENTS),
        chat_admin=admin,
        settings=CreateChannelSettings(auto_invite_owner=True, owner_username="r42x"),
    )


async def test_create_private_channel_auto_invites_owner() -> None:
    admin = FakeAdmin()
    result = await CreateChannel().execute(_ctx_owner(admin), {"display_name": "Секрет", "private": True})
    assert result["owner_invited"] is True
    assert admin.invited == [("chan-123", "uid-r42x")]  # owner resolved + added


async def test_public_channel_does_not_auto_invite_owner() -> None:
    admin = FakeAdmin()
    result = await CreateChannel().execute(_ctx_owner(admin), {"display_name": "Открытый", "private": False})
    assert "owner_invited" not in result
    assert admin.invited == []


async def test_auto_invite_skipped_when_owner_unset() -> None:
    admin = FakeAdmin()
    ctx = ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=admin,
        settings=CreateChannelSettings(auto_invite_owner=True, owner_username=""),  # no owner
    )
    result = await CreateChannel().execute(ctx, {"display_name": "X", "private": True})
    assert "owner_invited" not in result
    assert admin.invited == []


# --- registry manifest (single source of truth) ----------------------------


def test_registry_manifest_carries_schema() -> None:
    reg = build_registry()
    manifest = reg.manifest(("list_agents", "create_channel", "unknown"))
    names = [e["name"] for e in manifest]
    assert names == ["list_agents", "create_channel"]  # unknown skipped
    cc = next(e for e in manifest if e["name"] == "create_channel")
    assert cc["parameters"]["required"] == ["display_name"]
    assert cc["description"]
    assert cc["requires_confirmation"] is False  # no default tool is sensitive


def test_manifest_carries_requires_confirmation() -> None:
    # The confirmation gate is enforced from the manifest, so the flag must travel
    # verbatim — tested with throwaway tools rather than a permanently-gated one.
    from typing import ClassVar

    from crucible.tools.base import Tool
    from crucible.tools.registry import ToolRegistry

    class _Sensitive(Tool):
        name: ClassVar[str] = "danger"
        description: ClassVar[str] = "d"
        parameters: ClassVar[dict] = {}
        requires_confirmation: ClassVar[bool] = True

        async def execute(self, ctx, args):
            return None

    class _Plain(Tool):
        name: ClassVar[str] = "plain"
        description: ClassVar[str] = "d"
        parameters: ClassVar[dict] = {}

        async def execute(self, ctx, args):
            return None

    reg = ToolRegistry((_Sensitive, _Plain))  # type: ignore[arg-type]  # structural test doubles
    entries = {e["name"]: e for e in reg.manifest(("danger", "plain"))}
    assert entries["danger"]["requires_confirmation"] is True
    assert entries["plain"]["requires_confirmation"] is False


def test_registry_knows_all_default_tools() -> None:
    reg = build_registry()
    assert set(reg.names()) == {
        "list_agents", "create_channel", "invite_to_channel", "get_channel_members",
        "send_message", "read_channel", "create_agent",
        "ask_user_buttons", "ask_user_select", "open_form", "send_ephemeral",
        "send_file",
        "list_skills", "install_skill", "assign_skill", "remove_skill",
    }


async def test_server_injects_per_tool_config() -> None:
    admin = FakeAdmin()
    server = ToolServer(
        build_registry(),
        directory=FakeDirectory(AGENTS),
        admins={"assistant": admin},
        tokens={"tok": "assistant"},
        allowlists={"assistant": frozenset({"create_channel"})},
        port=8465,
        tool_configs={"create_channel": CreateChannelSettings(auto_invite_owner=True, owner_username="r42x")},
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8465/tool/create_channel",
                json={"display_name": "Секрет", "private": True},
                headers={"X-Tool-Token": "tok"},
            ) as resp:
                body = await resp.json()
        assert body["result"]["owner_invited"] is True  # per-tool config reached create_channel
        assert admin.invited == [("chan-123", "uid-r42x")]
    finally:
        await server.stop()


def test_registry_load_configs_is_generic() -> None:
    # create_channel declares settings_cls -> its config is loaded; tools without
    # settings are absent. Adding a configured tool needs no wiring change.
    reg = build_registry()
    configs = reg.load_configs(env_file="/nonexistent.env")  # isolate from real .env
    assert "create_channel" in configs
    assert isinstance(configs["create_channel"], CreateChannelSettings)
    assert "list_agents" not in configs  # no settings_cls


def test_require_accessors_raise_when_capability_absent() -> None:
    # A context with no admin/widgets/forms: each require_* raises a ToolError
    # (rather than an AttributeError on None), so a tool fails cleanly.
    ctx = ToolContext(agent_name="assistant", directory=FakeDirectory(AGENTS))
    for require in (ctx.require_chat_admin, ctx.require_interactions):
        try:
            require()
            raise AssertionError("expected ToolError")
        except ToolError:
            pass


async def test_tool_runs_without_an_admin_client() -> None:
    # A gateway without channel administration (e.g. Slack) registers no admin;
    # a tool that doesn't need one must still run — not 500 on a missing admin.
    reg = build_registry()
    server = ToolServer(
        reg,
        directory=FakeDirectory(AGENTS),
        admins={},  # no admin client for this agent
        tokens={"tok": "assistant"},
        allowlists={"assistant": frozenset({"list_agents"})},
        port=8469,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8469/tool/list_agents",
                json={},
                headers={"X-Tool-Token": "tok"},
            ) as resp:
                assert resp.status == 200
                assert "agents" in (await resp.json())["result"]
    finally:
        await server.stop()


# --- ask_user_buttons -------------------------------------------------------


class FakeWidgets:
    """A fake InteractionService that records ask() calls (open_form unused here)."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple] = []
        self._ok = ok

    async def ask(self, agent, runtime_session_id, prompt, options, *, style="buttons") -> bool:
        self.calls.append((agent, runtime_session_id, prompt, tuple(options), style))
        return self._ok

    async def open_form(self, agent, runtime_session_id, form) -> bool:
        return self._ok


def _ctx_widgets(widgets, *, session="assistant--c1") -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=FakeAdmin(),
        runtime_session_id=session, interaction_svc=widgets,
    )


async def test_ask_user_buttons_posts_via_widgets() -> None:
    from crucible.builtin_tools import AskUserButtons

    widgets = FakeWidgets()
    result = await AskUserButtons().execute(
        _ctx_widgets(widgets), {"prompt": "Обед?", "options": ["Да", "Нет"]}
    )
    assert result["status"] == "posted"
    assert widgets.calls == [("assistant", "assistant--c1", "Обед?", ("Да", "Нет"), "buttons")]


async def test_ask_user_buttons_validates_option_count() -> None:
    from crucible.builtin_tools import AskUserButtons

    try:
        await AskUserButtons().execute(_ctx_widgets(FakeWidgets()), {"prompt": "x", "options": ["one"]})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_ask_user_buttons_errors_without_widgets() -> None:
    from crucible.builtin_tools import AskUserButtons

    ctx = ToolContext(agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=FakeAdmin())
    try:
        await AskUserButtons().execute(ctx, {"prompt": "x", "options": ["a", "b"]})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


# --- ask_user_select --------------------------------------------------------


async def test_ask_user_select_posts_via_widgets_with_select_style() -> None:
    from crucible.builtin_tools import AskUserSelect

    widgets = FakeWidgets()
    result = await AskUserSelect().execute(
        _ctx_widgets(widgets), {"prompt": "City?", "options": ["A", "B", "C"]}
    )
    assert result["status"] == "posted"
    assert widgets.calls == [("assistant", "assistant--c1", "City?", ("A", "B", "C"), "select")]


async def test_ask_user_select_validates_option_count() -> None:
    from crucible.builtin_tools import AskUserSelect

    for bad in (["only-one"], [str(i) for i in range(21)]):  # too few / too many
        try:
            await AskUserSelect().execute(_ctx_widgets(FakeWidgets()), {"prompt": "x", "options": bad})
            raise AssertionError("expected ToolError")
        except ToolError:
            pass


async def test_ask_user_select_can_pick_from_the_workspace() -> None:
    from crucible.builtin_tools import AskUserSelect

    widgets = FakeWidgets()
    for source in ("users", "channels"):
        await AskUserSelect().execute(_ctx_widgets(widgets), {"prompt": "Who?", "source": source})
    # No options of our own: the platform supplies the people / channels.
    assert [(c[3], c[4]) for c in widgets.calls] == [((), "users"), ((), "channels")]


async def test_ask_user_select_rejects_a_bad_or_conflicting_source() -> None:
    from crucible.builtin_tools import AskUserSelect

    for args in (
        {"prompt": "x", "source": "aliens"},  # unknown source
        {"prompt": "x", "source": "users", "options": ["a", "b"]},  # options aren't ours to set
    ):
        try:
            await AskUserSelect().execute(_ctx_widgets(FakeWidgets()), args)
            raise AssertionError("expected ToolError")
        except ToolError:
            pass


# --- open_form --------------------------------------------------------------


class FakeForms:
    """A fake InteractionService that records open_form() calls (ask unused here)."""

    def __init__(self, ok: bool = True) -> None:
        self.calls: list = []
        self._ok = ok

    async def ask(self, agent, runtime_session_id, prompt, options, *, style="buttons") -> bool:
        return self._ok

    async def open_form(self, agent, runtime_session_id, form) -> bool:
        self.calls.append((agent, runtime_session_id, form))
        return self._ok


def _ctx_forms(forms) -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=FakeAdmin(),
        runtime_session_id="assistant--c1", interaction_svc=forms,
    )


async def test_open_form_posts_via_forms() -> None:
    from crucible.builtin_tools import OpenForm

    forms = FakeForms()
    result = await OpenForm().execute(_ctx_forms(forms), {"title": "Bug", "fields": [
        {"name": "s", "label": "Summary", "type": "text"},
        {"name": "p", "label": "Priority", "type": "select", "options": ["low", "high"]},
    ]})
    assert result["status"] == "posted"
    _agent, _rsid, form = forms.calls[0]
    assert form.title == "Bug" and len(form.fields) == 2
    assert form.fields[1].type == "select" and form.fields[1].options == ("low", "high")


async def test_open_form_rejects_select_without_options() -> None:
    from crucible.builtin_tools import OpenForm

    try:
        await OpenForm().execute(_ctx_forms(FakeForms()),
                                 {"title": "X", "fields": [{"name": "p", "label": "P", "type": "select"}]})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_open_form_rejects_empty_fields() -> None:
    from crucible.builtin_tools import OpenForm

    try:
        await OpenForm().execute(_ctx_forms(FakeForms()), {"title": "X", "fields": []})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_open_form_accepts_the_whole_vocabulary() -> None:
    from crucible.builtin_tools import OpenForm
    from crucible.ports.chat.types import FIELD_TYPES

    forms = FakeForms()
    fields = [
        {"name": t, "label": t, "type": t,
         **({"options": ["a", "b"]} if t in ("select", "multiselect", "radio") else {})}
        for t in FIELD_TYPES
    ]
    # More types than the per-form limit, so validate them in two batches.
    for batch in (fields[:9], fields[9:]):
        await OpenForm().execute(_ctx_forms(forms), {"title": "All", "fields": batch})
    built = [f.type for _a, _r, form in forms.calls for f in form.fields]
    assert built == list(FIELD_TYPES)


async def test_open_form_rejects_options_where_they_make_no_sense() -> None:
    from crucible.builtin_tools import OpenForm

    for field in (
        {"name": "u", "label": "Who", "type": "user", "options": ["a", "b"]},  # workspace-fed
        {"name": "t", "label": "T", "type": "text", "options": ["a", "b"]},    # free text
        {"name": "m", "label": "M", "type": "multiselect"},                    # needs options
        {"name": "x", "label": "X", "type": "wat"},                            # unknown type
    ):
        try:
            await OpenForm().execute(_ctx_forms(FakeForms()), {"title": "X", "fields": [field]})
            raise AssertionError(f"expected ToolError for {field}")
        except ToolError:
            pass


async def test_open_form_passes_a_custom_button_label() -> None:
    from crucible.builtin_tools import OpenForm

    forms = FakeForms()
    await OpenForm().execute(_ctx_forms(forms), {
        "title": "Bug", "open_label": "  Report a bug  ",
        "fields": [{"name": "s", "label": "Summary"}],
    })
    assert forms.calls[0][2].open_label == "Report a bug"  # trimmed

    await OpenForm().execute(_ctx_forms(forms), {
        "title": "Bug", "fields": [{"name": "s", "label": "Summary"}],
    })
    assert forms.calls[1][2].open_label == ""  # unset -> the engine's default


async def test_open_form_rejects_an_oversized_button_label() -> None:
    from crucible.builtin_tools import OpenForm

    try:
        await OpenForm().execute(_ctx_forms(FakeForms()), {
            "title": "Bug", "open_label": "x" * 76,
            "fields": [{"name": "s", "label": "Summary"}],
        })
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_open_form_rejects_a_form_that_collects_nothing() -> None:
    from crucible.builtin_tools import OpenForm

    try:
        await OpenForm().execute(_ctx_forms(FakeForms()), {"title": "X", "fields": [
            {"name": "n", "label": "just some text", "type": "label"},
        ]})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


# --- create_agent -------------------------------------------------------------


def _create_agent_settings(tmp_path) -> CreateAgentSettings:
    return CreateAgentSettings(
        _env_file=None,  # hermetic: never read the developer's real .env  # pyright: ignore[reportCallIssue]
        admin_token="admin-pat",
        mattermost_url="http://mm:8065",
        agents_path=str(tmp_path / "profiles"),
        dotenv_path=str(tmp_path / ".env"),
        gateway="mattermost",
    )


def _support_ctx(settings) -> ToolContext:
    return ToolContext(
        agent_name="support", directory=FakeDirectory(AGENTS), settings=settings
    )


async def test_create_agent_is_refused_to_non_support_agents(tmp_path) -> None:
    ctx = ToolContext(
        agent_name="assistant",  # a user agent that somehow allowlisted the tool
        directory=FakeDirectory(AGENTS),
        settings=_create_agent_settings(tmp_path),
    )
    try:
        await CreateAgent().execute(ctx, {"name": "x", "role": "r"})
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "support" in str(exc)
    assert not (tmp_path / "profiles").exists()  # nothing was written


async def test_create_agent_provisions_and_writes_everything(tmp_path, monkeypatch) -> None:
    import impi.agent_tools as agent_tools

    async def fake_provision(url, admin_token, *, username, **kwargs):
        assert (url, admin_token, username) == ("http://mm:8065", "admin-pat", "tutor")
        return BotCredentials(user_id="uid", username=username, token="minted", team="main")

    monkeypatch.setattr(agent_tools, "provision_mm_bot", fake_provision)
    result = await CreateAgent().execute(
        _support_ctx(_create_agent_settings(tmp_path)),
        {"name": "tutor", "role": "tutor", "system_prompt": "Ты репетитор.\n"},
    )
    assert result["created"] is True and result["restart_required"] is True
    profile = tmp_path / "profiles" / "agents" / "tutor"
    assert (profile / "agent.yaml").exists()
    assert (profile / ".pi" / "SYSTEM.md").read_text() == "Ты репетитор.\n"
    assert "AGENTS_MM_TOKEN__TUTOR=minted" in (tmp_path / ".env").read_text()


async def test_create_agent_writes_gateway_override_on_slack_default(tmp_path, monkeypatch) -> None:
    import impi.agent_tools as agent_tools

    async def fake_provision(url, admin_token, *, username, **kwargs):
        return BotCredentials(user_id="uid", username=username, token="minted")

    monkeypatch.setattr(agent_tools, "provision_mm_bot", fake_provision)
    settings = _create_agent_settings(tmp_path)
    settings.gateway = "slack"  # engine default gateway is not mattermost
    await CreateAgent().execute(_support_ctx(settings), {"name": "mm-one", "role": "r"})
    assert "AGENTS_GATEWAY__MM_ONE=mattermost" in (tmp_path / ".env").read_text()


async def test_create_agent_rolls_back_profile_when_provisioning_fails(tmp_path, monkeypatch) -> None:
    import impi.agent_tools as agent_tools
    from impi.provisioning import ProvisioningError

    async def fail(*args, **kwargs):
        raise ProvisioningError("username taken")

    monkeypatch.setattr(agent_tools, "provision_mm_bot", fail)
    try:
        await CreateAgent().execute(
            _support_ctx(_create_agent_settings(tmp_path)), {"name": "dup", "role": "r"}
        )
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "taken" in str(exc)
    # No orphan profile is left behind for a bot that was never created.
    assert not (tmp_path / "profiles" / "agents" / "dup").exists()


async def test_create_agent_requires_admin_token(tmp_path) -> None:
    settings = _create_agent_settings(tmp_path)
    settings.admin_token = ""
    try:
        await CreateAgent().execute(_support_ctx(settings), {"name": "x", "role": "r"})
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "TOOL_CREATE_AGENT_ADMIN_TOKEN" in str(exc)


def test_create_agent_is_confirmation_gated() -> None:
    # The runtime's confirm gate reads the manifest flag — it must travel.
    reg = build_registry()
    entry = next(e for e in reg.manifest(("create_agent",)))
    assert entry["requires_confirmation"] is True


# --- send_ephemeral -----------------------------------------------------------


from crucible.builtin_tools import SendEphemeral  # noqa: E402
from crucible.tools.base import CAP_EPHEMERAL  # noqa: E402


def _ctx_ephemeral(admin, *, channel="C1", user="u-triggered") -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS), chat_admin=admin,
        channel_id=channel, user_id=user,
    )


async def test_send_ephemeral_targets_current_user_by_default() -> None:
    admin = FakeAdmin()
    result = await SendEphemeral().execute(_ctx_ephemeral(admin), {"message": "psst"})
    assert result["delivered"] is True and result["ephemeral"] is True
    assert admin.ephemeral == [("C1", "u-triggered", "psst")]


async def test_send_ephemeral_resolves_target_username() -> None:
    admin = FakeAdmin()
    await SendEphemeral().execute(
        _ctx_ephemeral(admin), {"message": "hi", "target": "@someone"}
    )
    # FakeAdmin.resolve_username -> "uid-someone"
    assert admin.ephemeral == [("C1", "uid-someone", "hi")]


async def test_send_ephemeral_needs_a_target() -> None:
    admin = FakeAdmin()
    ctx = _ctx_ephemeral(admin, user="")  # no triggering user, no target arg
    try:
        await SendEphemeral().execute(ctx, {"message": "hi"})
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "target" in str(exc)
    assert admin.ephemeral == []


async def test_send_ephemeral_needs_conversation_context() -> None:
    admin = FakeAdmin()
    ctx = _ctx_ephemeral(admin, channel="")
    try:
        await SendEphemeral().execute(ctx, {"message": "hi"})
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


async def test_send_ephemeral_errors_when_username_unknown() -> None:
    class NoResolve(FakeAdmin):
        async def resolve_username(self, username):
            return None

    admin = NoResolve()
    try:
        await SendEphemeral().execute(
            _ctx_ephemeral(admin), {"message": "hi", "target": "@ghost"}
        )
        raise AssertionError("expected ToolError")
    except ToolError as exc:
        assert "ghost" in str(exc)


def test_send_ephemeral_declares_ephemeral_capability() -> None:
    assert SendEphemeral.requires == frozenset({CAP_EPHEMERAL})


async def test_server_resolves_conversation_into_context() -> None:
    # A tool that echoes the resolved conversation context; the server fills
    # ctx.channel_id/user_id from the injected resolver keyed on the session header.
    from typing import ClassVar

    from crucible.tools.base import Tool
    from crucible.tools.registry import ToolRegistry

    class _Echo(Tool):
        name: ClassVar[str] = "echo_ctx"
        description: ClassVar[str] = "d"
        parameters: ClassVar[dict] = {}

        async def execute(self, ctx, args):
            return {"channel_id": ctx.channel_id, "user_id": ctx.user_id}

    reg = ToolRegistry((_Echo(),))  # type: ignore[arg-type]

    async def resolver(rsid: str):
        return ("C-resolved", "U-resolved") if rsid == "assistant--conv" else None

    server = ToolServer(
        reg,
        directory=FakeDirectory(AGENTS),
        admins={},
        tokens={"tok": "assistant"},
        allowlists={"assistant": frozenset({"echo_ctx"})},
        port=8470,
        session_resolver=resolver,
    )
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "http://127.0.0.1:8470/tool/echo_ctx", json={},
                headers={"X-Tool-Token": "tok", "X-Runtime-Session": "assistant--conv"},
            ) as resp:
                body = (await resp.json())["result"]
        assert body == {"channel_id": "C-resolved", "user_id": "U-resolved"}
    finally:
        await server.stop()


# --- send_file ----------------------------------------------------------------


from crucible.builtin_tools import SendFile  # noqa: E402
from crucible.ports.chat.files import FileError  # noqa: E402
from crucible.tools.base import CAP_FILES  # noqa: E402


class FakeFileService:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self._error = error

    async def send(self, agent, runtime_session_id, paths, *, text=""):
        self.calls.append((agent, runtime_session_id, paths, text))
        if self._error is not None:
            raise self._error
        return [p.rsplit("/", 1)[-1] for p in paths]


def _ctx_files(file_svc) -> ToolContext:
    return ToolContext(
        agent_name="assistant", directory=FakeDirectory(AGENTS),
        runtime_session_id="assistant--conv", file_svc=file_svc,
    )


async def test_send_file_hands_the_path_and_caption_to_the_service() -> None:
    svc = FakeFileService()

    result = await SendFile().execute(
        _ctx_files(svc), {"path": "/tmp/chart.png", "caption": "the trend"}
    )

    assert result == {"sent": ["chart.png"]}
    assert svc.calls == [("assistant", "assistant--conv", ["/tmp/chart.png"], "the trend")]


async def test_send_file_reports_the_refusal_so_the_agent_can_fix_it() -> None:
    svc = FakeFileService(error=FileError("/etc/passwd: outside the directories you may send from"))

    with pytest.raises(ToolError, match="outside the directories"):
        await SendFile().execute(_ctx_files(svc), {"path": "/etc/passwd"})


async def test_send_file_needs_the_files_capability() -> None:
    # Nothing wired (attachments off) — the tool says so instead of crashing.
    with pytest.raises(ToolError, match="turned off"):
        await SendFile().execute(_ctx_files(None), {"path": "/tmp/a.png"})
    assert SendFile.requires == frozenset({CAP_FILES})
