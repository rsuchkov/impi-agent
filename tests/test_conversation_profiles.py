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
        async def entitlement(self, agent: str, msg: IncomingMessage) -> Entitlement:
            return Entitlement(tools=("read", "bash"), instructions="The person is an admin.")

    joined = await GatewayEntitlements(lambda _: spec, inner=Inner()).entitlement("assistant", msg("slack"))
    assert joined == Entitlement(tools=("read",), instructions="The person is an admin.")  # both allow read; the inner's words
    on_mm = await GatewayEntitlements(lambda _: spec, inner=Inner()).entitlement("assistant", msg("mattermost"))
    assert on_mm.tools == ("read", "bash")  # the gateway imposes nothing there; the inner's answer stands
    # And the profile a Slack conversation runs with is the narrowed one.
    scoped = EntitledProfiles(wiring, narrow_pi_profile).build(
        "assistant", _agent_profile(wiring, spec), await source.entitlement("assistant", msg("slack")), "assistant--slack1",
    )
    assert isinstance(scoped, PiProfile) and scoped.tools == ("read", "ask_user_buttons")
    assert wiring.session_allowlists["assistant--slack1"] == frozenset({"ask_user_buttons"})
