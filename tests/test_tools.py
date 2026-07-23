import aiohttp

import crucible.builtin_tools  # noqa: F401  # registers the generic ask/form tools
from crucible.ports.chat.admin import ChannelMember
from crucible.ports.chat.directory import AgentInfo
from crucible.ports.chat.types import PostSnippet
from crucible.tools.base import ToolContext, ToolError
from crucible.tools.registry import build_registry
from crucible.tools.server import ToolServer
from impi.chat_tools import (
    CreateChannel,
    CreateChannelSettings,
    InviteToChannel,
    ListAgents,
    ReadChannel,
    SendMessage,
)


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
        "send_message", "read_channel",
        "ask_user_buttons", "ask_user_select", "open_form",
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
