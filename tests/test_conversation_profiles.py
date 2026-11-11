"""A conversation's profile: the agent's, narrowed and extended by what the
application says the conversation may do."""

import dataclasses
import json
from pathlib import Path

from crucible.config import ToolSettings
from crucible.conversation_profiles import EntitledProfiles
from crucible.ports.agent import AgentSpec
from crucible.ports.entitlement import Entitlement
from crucible.runtimes.pi.profiles import (
    PiProfile,
    build_pi_profile,
    narrow_pi_profile,
    with_tool_env,
)
from crucible.tool_manifests import MANIFEST_ENV
from crucible.tools import ToolWiring
from impi.engine_tools import engine_tool_registry


def _wiring(tmp_path: Path, *, enabled: bool = True) -> tuple[ToolWiring, AgentSpec]:
    wiring = ToolWiring(
        ToolSettings(enabled=enabled, server_host="127.0.0.1", server_port=8422, max_grant_s=900),
        registry=engine_tool_registry(), data_dir=str(tmp_path), interactivity_on=True,
    )
    spec = AgentSpec(
        name="assistant", display_name="A", role="r", description="d", profile_dir=tmp_path,
        tools=("read", "bash", "ask_user_buttons", "open_form"),
    )
    wiring.enroll(spec, None)
    return wiring, spec


def _agent_profile(wiring: ToolWiring, spec: AgentSpec) -> PiProfile:
    base = dataclasses.replace(build_pi_profile(spec), append_system_prompt="Answer in Markdown.")
    return with_tool_env(base, wiring.profile_env(spec))


def test_a_narrower_entitlement_narrows_the_allowlist_the_manifest_and_the_servers_door(
    tmp_path: Path,
) -> None:
    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)

    scoped = EntitledProfiles(wiring, narrow_pi_profile).build(
        "assistant", base, Entitlement(tools=("read", "ask_user_buttons", "not_the_agents"), instructions=""),
        "assistant--dm1",
    )

    assert isinstance(scoped, PiProfile)
    # --tools: built-ins included, in the agent's order, the agent's list the ceiling.
    assert scoped.tools == ("read", "ask_user_buttons")
    # The manifest is the conversation's own, and lists only its typed tools.
    manifest = Path(scoped.env_files[MANIFEST_ENV])
    assert manifest == (tmp_path / "tool-manifests" / "assistant" / "assistant--dm1.json").resolve()
    assert [entry["name"] for entry in json.loads(manifest.read_text())] == ["ask_user_buttons"]
    assert manifest.stat().st_mode & 0o777 == 0o600
    # ...and the server's second door says the same.
    assert wiring.session_allowlists["assistant--dm1"] == frozenset({"ask_user_buttons"})
    # The rest of the env — the token — is the agent's as before.
    assert scoped.env == base.env


def test_instructions_follow_the_agents_appended_prompt_as_trusted_text(tmp_path: Path) -> None:
    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)

    scoped = EntitledProfiles(wiring, narrow_pi_profile).build(
        "assistant", base, Entitlement(instructions="  The person is a project admin.\n"), "assistant--dm1"
    )

    assert isinstance(scoped, PiProfile)
    assert scoped.append_system_prompt == "Answer in Markdown.\n\nThe person is a project admin."
    # Tools untouched when the entitlement names none: the agent's list, the
    # agent's manifest, no door of its own at the server.
    assert scoped.tools == base.tools
    assert scoped.env_files == base.env_files
    assert "assistant--dm1" not in wiring.session_allowlists


def test_the_default_entitlement_gives_the_agents_profile_back_and_clears_the_door(
    tmp_path: Path,
) -> None:
    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)
    profiles = EntitledProfiles(wiring, narrow_pi_profile)
    profiles.build("assistant", base, Entitlement(tools=("read",)), "assistant--dm1")
    assert "assistant--dm1" in wiring.session_allowlists

    back = profiles.build("assistant", base, Entitlement(), "assistant--dm1")

    assert back == base  # the same profile, so the runtime keeps the process it has
    assert "assistant--dm1" not in wiring.session_allowlists


def test_no_tool_at_all_is_an_empty_set_not_the_agents(tmp_path: Path) -> None:
    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)

    scoped = EntitledProfiles(wiring, narrow_pi_profile).build("assistant", base, Entitlement(tools=()), "assistant--dm1")

    assert isinstance(scoped, PiProfile)
    assert scoped.tools == ()
    assert wiring.session_allowlists["assistant--dm1"] == frozenset()
    assert json.loads(Path(scoped.env_files[MANIFEST_ENV]).read_text()) == []


def test_with_tools_off_an_entitlement_still_narrows_the_runtimes_own_tools(tmp_path: Path) -> None:
    wiring, spec = _wiring(tmp_path, enabled=False)
    base = with_tool_env(build_pi_profile(spec), wiring.profile_env(spec))

    scoped = EntitledProfiles(wiring, narrow_pi_profile).build("assistant", base, Entitlement(tools=("bash",)), "assistant--dm1")

    assert isinstance(scoped, PiProfile)
    assert scoped.tools == ("bash",)
    assert scoped.env_files == {}  # no manifest when there is no registry
    assert scoped.env == base.env  # AGENT_NAME survives, as add_env promises
    assert wiring.session_allowlists == {}


def test_a_scoped_profile_is_the_same_object_turn_after_turn(tmp_path: Path) -> None:
    # The runtime compares profiles to decide whether to restart a process:
    # the same entitlement must give an equal profile, or every turn restarts.
    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)
    profiles = EntitledProfiles(wiring, narrow_pi_profile)
    entitlement = Entitlement(tools=("read", "open_form"), instructions="Be brief.")

    first = profiles.build("assistant", base, entitlement, "assistant--dm1")
    second = profiles.build("assistant", base, entitlement, "assistant--dm1")

    assert first == second


async def test_the_gateway_source_entitles_a_conversation_to_what_its_gateway_may_use(tmp_path: Path) -> None:
    """``GatewayEntitlements`` reads the agent's own ``tools_by_gateway``: the
    gateway's list for a message through it, the agent's own elsewhere; with an
    inner source (the application's), the tools both allow and the inner's
    instructions. Through ``EntitledProfiles`` the Slack conversation's profile
    is the narrowed one."""
    from crucible.conversation_profiles import GatewayEntitlements
    from crucible.ports.chat.types import ConversationRef, IncomingMessage

    wiring, spec = _wiring(tmp_path)
    spec = dataclasses.replace(spec, tools_by_gateway=(("slack", ("read", "ask_user_buttons")),))
    source = GatewayEntitlements(lambda name: spec if name == "assistant" else None)

    def msg(gateway: str) -> IncomingMessage:
        return IncomingMessage(
            ref=ConversationRef(channel_id="c", conversation_id="c", message_id="m", thread_root_id=""),
            text="hi", user_id="u1", username="u", gateway=gateway,
        )

    assert (await source.entitlement("assistant", msg("slack"))).tools == ("read", "ask_user_buttons")
    assert (await source.entitlement("assistant", msg("mattermost"))).tools is None  # the agent's own
    assert (await source.entitlement("other", msg("slack"))).tools is None  # not an agent of ours: imposes nothing

    class Inner:
        def __init__(self) -> None:
            self.ceilings: list = []

        async def entitlement(self, agent: str, msg: IncomingMessage, *, tools=None) -> Entitlement:
            self.ceilings.append(tools)
            # An application writes its instructions for the tools that will be there.
            allowed = [t for t in ("read", "bash") if tools is None or t in tools]
            return Entitlement(tools=tuple(allowed), instructions=f"You may use: {', '.join(allowed)}.")

    inner = Inner()
    joined = await GatewayEntitlements(lambda _: spec, inner=inner).entitlement("assistant", msg("slack"))
    assert inner.ceilings == [("read", "ask_user_buttons")]  # the inner was told what the gateway leaves
    assert joined == Entitlement(tools=("read",), instructions="You may use: read.")  # and wrote for it
    on_mm = await GatewayEntitlements(lambda _: spec, inner=inner).entitlement("assistant", msg("mattermost"))
    assert inner.ceilings[-1] is None and on_mm.tools == ("read", "bash")  # no ceiling there; the inner's answer stands
    with_ceiling = await GatewayEntitlements(lambda _: spec).entitlement("assistant", msg("mattermost"), tools=("read",))
    assert with_ceiling.tools == ("read",)  # a ceiling handed in narrows the gateway's answer too
    # Where other people read — a channel, a thread — ``tools_in_channels`` narrows again, on any gateway;
    # a direct message, and a thread answering one, keeps the gateway's list.
    from crucible.ports.chat.types import KIND_DM, KIND_THREAD

    shared_spec = dataclasses.replace(spec, tools_in_channels=("read", "bash"))
    in_rooms = GatewayEntitlements(lambda _: shared_spec)
    channel = dataclasses.replace(msg("slack"), kind="channel")
    thread = dataclasses.replace(msg("mattermost"), kind=KIND_THREAD)
    direct = dataclasses.replace(msg("slack"), kind=KIND_DM, is_dm=True)
    dm_thread = dataclasses.replace(msg("slack"), kind=KIND_DM, is_dm=True)
    assert (await in_rooms.entitlement("assistant", channel)).tools == ("read",)  # slack's list and the shared one
    assert (await in_rooms.entitlement("assistant", thread)).tools == ("read", "bash")  # no gateway list: the shared one
    assert (await in_rooms.entitlement("assistant", direct)).tools == ("read", "ask_user_buttons")  # the gateway's alone
    assert (await in_rooms.entitlement("assistant", dm_thread)).tools == ("read", "ask_user_buttons")
    # And the profile a Slack conversation runs with is the narrowed one.
    scoped = EntitledProfiles(wiring, narrow_pi_profile).build(
        "assistant", _agent_profile(wiring, spec), await source.entitlement("assistant", msg("slack")), "assistant--slack1",
    )
    assert isinstance(scoped, PiProfile) and scoped.tools == ("read", "ask_user_buttons")
    assert wiring.session_allowlists["assistant--slack1"] == frozenset({"ask_user_buttons"})


async def test_a_message_that_names_no_gateway_gets_the_narrowest_list_of_an_agent_that_narrows() -> None:
    """A door nobody named opens no wider than the narrowest one that is: a
    message with no gateway on an agent with a map is entitled to what every
    named gateway allows — never to the agent's whole list."""
    from pathlib import Path

    from crucible.conversation_profiles import GatewayEntitlements
    from crucible.ports.chat.types import ConversationRef, IncomingMessage

    def msg(gateway: str) -> IncomingMessage:
        return IncomingMessage(
            ref=ConversationRef(channel_id="c", conversation_id="c", message_id="m", thread_root_id=""),
            text="hi", user_id="u1", username="u", gateway=gateway,
        )

    spec = AgentSpec(
        name="assistant", display_name="A", role="r", description="d", profile_dir=Path("."),
        tools=("read", "bash", "send_file"),
        tools_by_gateway=(("slack", ("read", "send_file")), ("http", ("read", "bash"))),
    )
    source = GatewayEntitlements(lambda _: spec)
    assert (await source.entitlement("assistant", msg(""))).tools == ("read",)  # what slack and http both allow
    assert (await source.entitlement("assistant", msg("mattermost"))).tools is None  # a named gateway unnamed in the map: all
    plain = GatewayEntitlements(lambda _: dataclasses.replace(spec, tools_by_gateway=()))
    assert (await plain.entitlement("assistant", msg(""))).tools is None  # no map: nothing to narrow


def test_a_deferred_tool_stays_allowed_is_named_to_the_bridge_and_the_search_tool_and_a_note_are_added(
    tmp_path: Path,
) -> None:
    """A tool the model is not told about until it asks: still in --tools (a
    name left out is not registered, and nothing could load it), named in the
    env the bridge reads, the runtime's search tool added so the model can
    load it, and the prompt naming what can be loaded."""
    from crucible.runtimes.pi.profiles import DEFERRED_NOTE, TOOL_SEARCH
    from crucible.tool_manifests import DEFERRED_ENV

    wiring, spec = _wiring(tmp_path)
    base = _agent_profile(wiring, spec)
    profiles = EntitledProfiles(wiring, narrow_pi_profile)

    scoped = profiles.build(
        "assistant", base,
        Entitlement(tools=("read", "ask_user_buttons", "open_form"), instructions="Be brief.", deferred=("open_form", "not_the_agents")),
        "assistant--dm1",
    )
    assert isinstance(scoped, PiProfile)
    assert scoped.tools == ("read", "ask_user_buttons", "open_form", TOOL_SEARCH)
    assert scoped.env[DEFERRED_ENV] == "open_form"  # the conversation's tools alone, in the agent's order
    assert scoped.append_system_prompt == "Answer in Markdown.\n\nBe brief.\n\n" + DEFERRED_NOTE % "open_form"
    # The server's door is the conversation's whole set: a deferred tool is still the conversation's.
    assert wiring.session_allowlists["assistant--dm1"] == frozenset({"ask_user_buttons", "open_form"})
    # Deferring two names the conversation has: both named, once each, search added once.
    two = profiles.build(
        "assistant", base, Entitlement(deferred=("open_form", "ask_user_buttons", "read")), "assistant--dm1",
    )
    assert isinstance(two, PiProfile)
    assert two.env[DEFERRED_ENV] == "ask_user_buttons,open_form" and two.tools.count(TOOL_SEARCH) == 1  # read is a built-in: declared as it is
    # A built-in alone: nothing to hold back, so nothing is said or added.
    builtin_only = profiles.build("assistant", base, Entitlement(deferred=("read",)), "assistant--dm1")
    assert isinstance(builtin_only, PiProfile)
    assert DEFERRED_ENV not in builtin_only.env and TOOL_SEARCH not in builtin_only.tools and "not declared" not in builtin_only.append_system_prompt
    # An agent that names the search tool itself does not get it twice.
    names_it = dataclasses.replace(base, tools=(*base.tools, TOOL_SEARCH))
    again = profiles.build("assistant", names_it, Entitlement(deferred=("open_form",)), "assistant--dm1")
    assert isinstance(again, PiProfile) and again.tools.count(TOOL_SEARCH) == 1
    # Nothing deferred: no env, no search tool, no note — the profile as it always was.
    plain = profiles.build("assistant", base, Entitlement(deferred=()), "assistant--dm1")
    assert isinstance(plain, PiProfile)
    assert DEFERRED_ENV not in plain.env and TOOL_SEARCH not in plain.tools and "not declared" not in plain.append_system_prompt
    # The two differ in what the process is started with, so a change restarts it.
    from crucible.runtimes.pi.runtime import spawn_fingerprint

    assert spawn_fingerprint(scoped) != spawn_fingerprint(plain) != spawn_fingerprint(two)


async def test_the_gateway_source_passes_the_inners_deferred_names_on() -> None:
    from crucible.conversation_profiles import GatewayEntitlements
    from crucible.ports.chat.types import ConversationRef, IncomingMessage

    spec = AgentSpec(name="assistant", display_name="A", role="r", description="d", profile_dir=Path("/x"), tools=("read", "bash"))

    class Inner:
        async def entitlement(self, agent, msg, *, tools=None):
            return Entitlement(deferred=("bash",))

    msg = IncomingMessage(ref=ConversationRef("c", "c", "m"), text="hi", user_id="u1", username="u", kind="dm", gateway="slack")
    joined = await GatewayEntitlements(lambda _: spec, inner=Inner()).entitlement("assistant", msg)
    assert joined.deferred == ("bash",)
