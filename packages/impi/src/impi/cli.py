"""impi's command-line interface (console script `impi`): provision agents and
Mattermost credentials from a terminal, on the same core the create_agent
engine tool uses. Interactive by default; fully flag-driven with --yes so the
installer can run it inside the container (`compose run --rm impi impi ...`).

Deliberately stdlib-only for the UI (argparse + input + a little ANSI): the
branded installer TUI lives in bash, and the runtime image stays lean.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import shutil
import sys
from pathlib import Path

import httpx

from crucible import sessions_cli
from crucible.ports.tasks import TaskError
from crucible.profiles.errors import ProfileError
from crucible.profiles.loader import FsProfileStore
from crucible.scheduler.admin import TaskAdmin, local_time
from crucible.scheduler.health import ALIVE, liveness
from crucible.scheduler.triggers import from_iso, to_iso, utc_now
from crucible.secrets.approvals import humanize as humanize_window
from crucible.secrets.ports import SecretBackendError, UnlockMaterial
from crucible.secrets.vault import VaultBackend
from crucible.skills import (
    SkillError,
    SkillLibrary,
    assign_skill,
    assigned_skills,
    declared_tools,
    install,
    stage,
    unassign_skill,
)
from crucible.store.base import (
    APPROVAL_NEVER,
    APPROVALS,
    KIND_SECRET,
    SecretPolicyRecord,
)
from crucible.store.sessions import SqliteSessionStore
from impi import provisioning as prov
from impi.config import ImpiSettings, load_settings

# --- tiny ANSI helpers -------------------------------------------------------

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _sgr(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def _bold(text: str) -> str:
    return _sgr("1", text)


def _dim(text: str) -> str:
    return _sgr("2", text)


def _ok(text: str) -> None:
    print(_sgr("32", "✔ ") + text)


def _fail(text: str) -> None:
    print(_sgr("31", "✘ ") + text, file=sys.stderr)


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = _dim(f" [{default}]") if default else ""
    while True:
        if secret:
            value = getpass.getpass(f"{_bold(label)}: ")
        else:
            value = input(f"{_bold(label)}{suffix}: ").strip()
        if value:
            return value
        if default:
            return default
        print(_dim("  (a value is required)"))


def _confirm(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{_bold(question)} {_dim(hint)} ").strip().lower()
    except EOFError:
        # Nobody is there to answer (a pipe, a container with no TTY). Refusing
        # is the safe reading of silence; --yes is how a script says yes.
        print(_dim("  (nothing to read the answer from — pass --yes)"))
        return False
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


# --- shared plumbing ----------------------------------------------------------


def _settings() -> ImpiSettings:
    return load_settings()


def _prov_defaults(env_file: str) -> prov.CreateAgentSettings:
    # _env_file is a runtime-only pydantic-settings kwarg pyright cannot see.
    return prov.CreateAgentSettings(_env_file=env_file)  # pyright: ignore[reportCallIssue]


def _apply_env(env_file: str, updates: dict[str, str]) -> None:
    for key, value in updates.items():
        prov.set_env_key(env_file, key, value)
        _ok(f"{key} -> {env_file}")


def _reload_hint() -> None:
    """A skill assignment only changes an existing agent's config, so a reload is
    enough — no restart, and live conversations keep their memory."""
    print()
    print(
        _bold("Reload to apply: ")
        + "an agent picks up its new skills on the next turn.\n"
        + _dim("  deployment: `impi reload`; dev checkout: `make reload`")
    )


def _restart_hint() -> None:
    print()
    print(
        _bold("Restart required: ")
        + "new agents are enumerated at engine startup.\n"
        + _dim("  deployment: run `impi restart` on the host; dev checkout: "
               "`make stop && make run-bg`")
    )


# --- impi agent add -----------------------------------------------------------


def _cmd_agent_add(args: argparse.Namespace) -> int:
    settings = _settings()
    env_file = args.env_file or settings.dotenv_path
    defaults = _prov_defaults(env_file)
    interactive = not args.yes

    name: str = args.name or ""
    if not name and interactive:
        name = _prompt("Agent name (slug)")
    if not name:
        _fail("--name is required with --yes")
        return 2
    if not prov.AGENT_NAME_RE.match(name):
        _fail(f"invalid agent name {name!r}: lowercase letters, digits, hyphens")
        return 2
    role: str = args.role or ""
    if not role and interactive:
        role = _prompt("Role (one line)", "assistant")
    if not role:
        _fail("--role is required with --yes")
        return 2
    display_name: str = args.display_name or ""
    if not display_name and interactive:
        display_name = _prompt("Display name", name.replace("-", " ").title())
    display_name = display_name or name
    description: str = args.description or ""
    if not description and interactive:
        description = input(f"{_bold('Description')} {_dim('(optional)')}: ").strip()

    gateway = args.gateway or settings.gateway
    agents_dir = args.agents_dir or defaults.agents_path or settings.agents_path
    if not agents_dir:
        _fail("no agents directory (pass --agents-dir or set AGENTS_PATH)")
        return 2

    updates: dict[str, str] = {}
    mm_url = args.mm_url or defaults.mattermost_url or settings.mattermost_url
    admin_token = args.admin_token or defaults.admin_token
    bot_token: str = args.bot_token or ""
    if gateway == "mattermost":
        if bot_token:
            admin_token = ""  # an explicit token wins over auto-provisioning
        elif not admin_token:
            if interactive:
                print(_dim(
                    "No admin token configured — create a bot manually in the "
                    "Mattermost System Console and paste its token."
                ))
                bot_token = _prompt("Bot access token", secret=True)
            else:
                _fail("need --admin-token or --bot-token with --yes")
                return 2
    elif gateway == "slack":
        slack_bot: str = args.slack_bot_token or ""
        if not slack_bot and interactive:
            slack_bot = _prompt("Slack bot token (xoxb-...)", secret=True)
        slack_app: str = args.slack_app_token or ""
        if not slack_app and interactive:
            slack_app = _prompt("Slack app token (xapp-...)", secret=True)
        if not slack_bot or not slack_app:
            _fail("slack agents need --slack-bot-token and --slack-app-token")
            return 2
        updates[prov.agent_env_key(name, "SLACK_BOT_TOKEN")] = slack_bot
        updates[prov.agent_env_key(name, "SLACK_APP_TOKEN")] = slack_app
    elif gateway == "ws":
        # No per-agent credentials: client services authorize against the hub
        # with their own tokens (impi ws add-service).
        pass
    else:
        _fail(f"unknown gateway {gateway!r}")
        return 2

    if interactive and not _confirm(f"Create agent '{name}' ({gateway})?"):
        print("aborted")
        return 1

    try:
        profile_dir = prov.write_agent_profile(
            agents_dir,
            name=name,
            role=role,
            display_name=display_name,
            description=description,
        )
        _ok(f"profile: {profile_dir}")
        if gateway == "mattermost":
            if bot_token:
                token = bot_token
            else:
                creds = asyncio.run(
                    prov.provision_mm_bot(
                        mm_url,
                        admin_token,
                        username=name,
                        display_name=display_name,
                        description=description,
                        team=args.team or defaults.team,
                    )
                )
                token = creds.token
                _ok(f"bot @{creds.username} provisioned"
                    + (f" (team: {creds.team})" if creds.team else ""))
            updates[prov.agent_env_key(name)] = token
    except prov.ProvisioningError as exc:
        _fail(str(exc))
        return 2

    if gateway != settings.gateway:
        updates[prov.agent_env_key(name, "GATEWAY")] = gateway
    _apply_env(env_file, updates)
    if gateway == "ws" and not settings.ws_services():
        print(_dim(
            "No ws client services yet — register one with "
            "`impi ws add-service <name>` so something can talk to this agent."
        ))
    _restart_hint()
    return 0


# --- impi agent list ----------------------------------------------------------


def _cmd_agent_list(args: argparse.Namespace) -> int:
    settings = _settings()
    agents_dir = args.agents_dir or settings.agents_path
    if not agents_dir:
        _fail("no agents directory (set AGENTS_PATH)")
        return 2
    try:
        store = FsProfileStore(agents_dir)
    except ProfileError as exc:
        _fail(str(exc))
        return 2
    for spec in store.list():
        gateway = settings.gateway_for(spec.name)
        if gateway == "mattermost":
            has_token = bool(settings.mm_token_for(spec.name))
        else:
            has_token = all(settings.slack_tokens_for(spec.name))
        status = _sgr("32", "token ok") if has_token else _sgr("33", "no token")
        print(f"{_bold(spec.name):<32} {spec.role:<24} {gateway:<12} {status}")
    return 0


# --- impi skill ------------------------------------------------------------------


def _library(args: argparse.Namespace):
    settings = _settings()
    root = getattr(args, "skills_dir", None) or settings.resolved_skills_path
    return SkillLibrary(root)


def _manifest_of(args: argparse.Namespace, agent: str) -> Path:
    """The agent's profile, which is where an assignment is recorded."""
    settings = _settings()
    agents_dir = getattr(args, "agents_dir", None) or settings.agents_path
    if not agents_dir:
        raise FileNotFoundError("no agents directory (set AGENTS_PATH)")
    manifest = Path(agents_dir) / "agents" / agent / "agent.yaml"
    if not manifest.is_file():
        raise FileNotFoundError(f"no agent {agent!r} at {manifest}")
    return manifest


def _cmd_skill_list(args: argparse.Namespace) -> int:
    library = _library(args)
    skills = library.list()
    if not skills:
        print(f"no skills in {library.root}")
        return 0
    users = _skill_users(args)
    for skill in skills:
        where = ", ".join(users.get(skill.name, ())) or _dim("unassigned")
        origin = skill.source.describe() if skill.source else "local"
        print(f"{_bold(skill.name):<32} {skill.description[:48]:<50} {where}")
        print(f"{'':<32} {_dim(origin)}")
    return 0


def _skill_users(args: argparse.Namespace) -> dict[str, list[str]]:
    """skill name -> agents that reference it, read from the profiles themselves."""

    settings = _settings()
    agents_dir = getattr(args, "agents_dir", None) or settings.agents_path
    out: dict[str, list[str]] = {}
    if not agents_dir:
        return out
    for manifest in sorted(Path(agents_dir).glob("agents/*/agent.yaml")):
        for name in assigned_skills(manifest):
            out.setdefault(name, []).append(manifest.parent.name)
    return out


def _cmd_skill_show(args: argparse.Namespace) -> int:
    try:
        skill = _library(args).get(args.name)
    except SkillError as exc:
        _fail(str(exc))
        return 2
    print(_bold(skill.name), f"({skill.version})" if skill.version else "")
    print(skill.description)
    print(_dim(f"path:     {skill.path}"))
    if skill.source:
        print(_dim(f"source:   {skill.source.describe()}"))
    if skill.requires_tools:
        print(_dim(f"needs:    {', '.join(skill.requires_tools)}"))
    if skill.tags:
        print(_dim(f"tags:     {', '.join(skill.tags)}"))
    used_by = _skill_users(args).get(skill.name, [])
    print(_dim(f"assigned: {', '.join(used_by) if used_by else 'nobody'}"))
    return 0


def _cmd_skill_install(args: argparse.Namespace) -> int:
    library = _library(args)
    try:
        staged = stage(args.source)
    except SkillError as exc:
        _fail(str(exc))
        return 2
    with staged:
        print(f"{_bold(staged.skill.name)} — {staged.skill.description}")
        print(_dim(f"from {staged.source.describe()}"))
        # A skill's scripts run inside the engine with the agent's tools, so show
        # what is about to land before copying anything.
        for name, size, executable in staged.files():
            mark = _sgr("33", " exec") if executable else ""
            print(f"  {name:<44} {size:>8}B{mark}")
        if staged.skill.requires_tools:
            print(_dim(f"needs tools: {', '.join(staged.skill.requires_tools)}"))
        if not args.yes and not _confirm(f"Install into {library.root}?", True):
            print("cancelled")
            return 1
        try:
            skill = install(library, staged, name=args.name or "", force=args.force)
        except SkillError as exc:
            _fail(str(exc))
            return 2
    _ok(f"installed {skill.name} -> {skill.path}")
    return 0


def _cmd_skill_update(args: argparse.Namespace) -> int:
    library = _library(args)
    try:
        current = library.get(args.name)
    except SkillError as exc:
        _fail(str(exc))
        return 2
    if current.source is None or current.source.kind != "git":
        _fail(f"{args.name} was not installed from a repository — nothing to update")
        return 2
    spec = current.source.location
    if current.source.path:
        spec += f"#{current.source.path}"
    if current.source.ref:
        spec += f"@{current.source.ref}"
    with stage(spec) as staged:
        if staged.source.sha and staged.source.sha == current.source.sha:
            print(f"{args.name} is already at {current.source.sha[:7]}")
            return 0
        print(f"{current.source.sha[:7] or '?'} -> {staged.source.sha[:7]}")
        if not args.yes and not _confirm(f"Update {args.name}?", True):
            print("cancelled")
            return 1
        install(library, staged, name=args.name, force=True)
    _ok(f"updated {args.name}")
    return 0


def _cmd_skill_remove(args: argparse.Namespace) -> int:
    library = _library(args)
    if not library.has(args.name):
        _fail(f"no skill {args.name!r} in {library.root}")
        return 2
    users = _skill_users(args).get(args.name, [])
    if users and not args.force:
        _fail(f"{args.name} is still assigned to: {', '.join(users)} (unassign first, or --force)")
        return 2
    if not args.yes and not _confirm(f"Remove {args.name} from {library.root}?", False):
        print("cancelled")
        return 1
    shutil.rmtree(library.path_of(args.name))
    _ok(f"removed {args.name}")
    return 0


def _cmd_skill_assign(args: argparse.Namespace) -> int:
    library = _library(args)
    try:
        manifest = _manifest_of(args, args.agent)
    except FileNotFoundError as exc:
        _fail(str(exc))
        return 2
    if args.remove:
        changed = unassign_skill(manifest, args.name)
        _ok(f"{args.agent}: removed {args.name}" if changed else f"{args.agent} did not have {args.name}")
        _reload_hint()
        return 0
    try:
        skill = library.get(args.name)
    except SkillError as exc:
        _fail(str(exc))
        return 2
    missing = [t for t in skill.requires_tools if t not in declared_tools(manifest)]
    if missing:
        # Without these the skill installs and then quietly does nothing.
        _fail(f"{args.agent} lacks the tools {args.name} needs: {', '.join(missing)}")
        print(_dim(f"add them to runtime.tools in {manifest}"))
        if not args.yes and not _confirm("Assign anyway?", False):
            return 1
    changed = assign_skill(manifest, skill.name)
    _ok(f"{args.agent}: added {skill.name}" if changed else f"{args.agent} already had {skill.name}")
    _reload_hint()
    return 0


# --- impi mm bootstrap-token ----------------------------------------------------


def _cmd_mm_bootstrap_token(args: argparse.Namespace) -> int:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
    if not password:
        _fail("empty password")
        return 2
    try:
        token, user_id = asyncio.run(
            prov.mm_admin_pat(args.url, args.login_id, password)
        )
    except prov.ProvisioningError as exc:
        _fail(str(exc))
        return 2
    print(f"user_id: {user_id}", file=sys.stderr)
    print(token)  # stdout carries only the token: the installer captures it
    return 0


# --- impi provision support -----------------------------------------------------


def _cmd_provision(args: argparse.Namespace) -> int:
    settings = _settings()
    env_file = args.env_file or settings.dotenv_path
    defaults = _prov_defaults(env_file)
    mm_url = args.mm_url or defaults.mattermost_url or settings.mattermost_url
    admin_token = args.admin_token or defaults.admin_token
    if not admin_token:
        _fail("need an admin token (--admin-token or TOOL_CREATE_AGENT_ADMIN_TOKEN)")
        return 2
    try:
        creds = asyncio.run(
            prov.provision_mm_bot(
                mm_url,
                admin_token,
                username=args.username,
                display_name="Impi Support",
                description="impi's built-in agent-builder",
                team=args.team or defaults.team,
            )
        )
    except prov.ProvisioningError as exc:
        _fail(str(exc))
        return 2
    _ok(f"bot @{creds.username} provisioned"
        + (f" (team: {creds.team})" if creds.team else ""))
    updates = {prov.agent_env_key("support"): creds.token}
    if settings.gateway != "mattermost":
        updates[prov.agent_env_key("support", "GATEWAY")] = "mattermost"
    _apply_env(env_file, updates)
    _restart_hint()
    return 0


# --- impi ws add-service ----------------------------------------------------------


def _cmd_ws_add_service(args: argparse.Namespace) -> int:
    settings = _settings()
    env_file = args.env_file or settings.dotenv_path
    name = args.name.strip().lower()
    if not prov.AGENT_NAME_RE.match(name):
        _fail(f"invalid service name {name!r}: lowercase letters, digits, hyphens")
        return 2
    suffix = name.upper().replace("-", "_")
    token = secrets.token_hex(24)
    updates = {f"WS_SERVICE_TOKEN__{suffix}": token}
    if args.agents is not None:
        updates[f"WS_SERVICE_AGENTS__{suffix}"] = args.agents
    _apply_env(env_file, updates)
    allowed = args.agents if args.agents is not None else "all ws agents"
    print(f"service {_bold(name)} registered (agents: {allowed})")
    print(f"connect: ws://<engine-host>:{settings.ws_port}/ws")
    print(f"token  : {token}")
    print(_dim("shown only once — store it in the service's config now"))
    print(_dim("restart the engine so the hub picks the service up"))
    return 0


# --- impi health ----------------------------------------------------------------


def _cmd_health(args: argparse.Namespace) -> int:
    settings = _settings()
    failures = 0
    url = settings.mattermost_url.rstrip("/") + "/api/v4/system/ping"
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            _ok(f"Mattermost reachable: {settings.mattermost_url}")
        else:
            _fail(f"Mattermost ping HTTP {resp.status_code}: {settings.mattermost_url}")
            failures += 1
    except httpx.HTTPError as exc:
        _fail(f"Mattermost unreachable ({settings.mattermost_url}): {exc}")
        failures += 1
    agents_dir = Path(settings.agents_path) / "agents" if settings.agents_path else None
    if agents_dir and agents_dir.is_dir():
        count = len(list(agents_dir.glob("*/agent.yaml")))
        _ok(f"agents dir: {agents_dir} ({count} profile(s))")
    else:
        _fail(f"agents dir missing: {agents_dir or '(AGENTS_PATH unset)'}")
        failures += 1
    return 1 if failures else 0


# --- impi secret --------------------------------------------------------------
#
# Two kinds of command live here, and the split is deliberate.
#
# `unlock` and `status` talk to the RUNNING engine over its loopback tool server,
# because what they change or read is the credential it holds in memory. Run them
# with `impi secret …` from the host, which execs into the live container.
#
# Everything that touches a value — init, set, ls, rm — talks to the backend
# directly with the operator's own material, and everything that touches policy,
# grants or the ledger reads SQLite directly. Neither goes through the engine,
# because a route the engine exposes on loopback is a route the agents' shells
# can reach too.


def _engine(settings: ImpiSettings, path: str, body: dict | None = None) -> dict:
    """Call the running engine's loopback API."""
    url = settings.tools.server_url.rstrip("/") + path
    try:
        if body is None:
            response = httpx.get(url, timeout=30.0)
        else:
            response = httpx.post(url, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        raise TaskError(
            f"no running engine at {url} ({exc}) — this command needs the live "
            "engine, so run it as `impi secret …` on the host"
        ) from exc
    payload = response.json() if response.content else {}
    if response.status_code >= 400:
        raise TaskError(str(payload.get("error") or f"HTTP {response.status_code}"))
    return payload


def _unlock_material(settings: ImpiSettings, *, prompt: bool = True) -> UnlockMaterial:
    """The unseal key and the AppRole secret, from the configured files or from
    whoever is at the terminal."""
    config = settings.secrets
    unseal = _read_material_file(config.unseal_key_file)
    auth = _read_material_file(config.secret_id_file)
    if prompt and not unseal:
        unseal = _prompt("Vault unseal key", secret=True)
    if prompt and not auth:
        auth = _prompt("Engine AppRole secret id", secret=True)
    return UnlockMaterial(unseal_key=unseal, auth_secret=auth)


def _read_material_file(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _vault(settings: ImpiSettings) -> VaultBackend:
    return VaultBackend(
        settings.secrets.vault_addr,
        mount=settings.secrets.vault_mount,
        role_id=settings.secrets.role_id,
    )


async def _open_vault(settings: ImpiSettings) -> VaultBackend:
    """A backend this process can read and write with. Unlike the engine, the
    CLI holds no credential between invocations — it opens one each time from
    the operator's material and drops it on exit."""
    backend = _vault(settings)
    state = await backend.unlock(_unlock_material(settings))
    if not state.usable:
        await backend.close()
        raise TaskError(state.detail or "vault is not usable with that material")
    return backend


def _now_iso() -> str:
    """The store's timestamp format — the same one the engine writes."""
    return to_iso(utc_now()) or ""


def _zone() -> str:
    """Where the operator reading this output is. The engine writes UTC; the
    scheduler's timezone is the closest thing to "here" the config knows."""
    return _settings().scheduler.timezone


def _duration(text: str) -> int:
    """"15m" / "1h" / "0" -> seconds. The unit is required above zero, because a
    bare number is exactly the kind of thing that means minutes to one person
    and seconds to another."""
    raw = text.strip().lower()
    if raw in ("0", "none", "never"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if len(raw) > 1 and raw[-1] in units and raw[:-1].isdigit():
        return int(raw[:-1]) * units[raw[-1]]
    raise TaskError(f"not a duration: {text!r} (try 15m, 1h, or 0)")


def _cmd_secret_init(args: argparse.Namespace) -> int:
    settings = _settings()
    if not settings.secrets.enabled:
        _fail("secrets are off — set SECRETS_ENABLED=true and restart the engine")
        return 2
    backend = _vault(settings)
    try:
        material = asyncio.run(backend.bootstrap())
    except SecretBackendError as exc:
        _fail(str(exc))
        return 1
    finally:
        asyncio.run(backend.close())

    _apply_env(args.env_file or settings.dotenv_path, {"SECRETS_ROLE_ID": material.role_id})
    print()
    print(_bold("Store these somewhere safe — they are shown once and nowhere else."))
    print(f"  unseal key : {material.unseal_key}")
    print(f"  root token : {material.root_token}")
    print(f"  secret id  : {material.secret_id}")
    print()
    print(
        _dim(
            "The engine needs the unseal key and the secret id after every restart.\n"
            "  by hand    : `impi secret unlock`\n"
            "  unattended : write them to files and point SECRETS_UNSEAL_KEY_FILE /\n"
            "               SECRETS_SECRET_ID_FILE at them (see docs/secrets.md for\n"
            "               what that costs you)"
        )
    )
    _restart_hint()
    return 0


def _cmd_secret_unlock(args: argparse.Namespace) -> int:
    settings = _settings()
    material = _unlock_material(settings)
    if not material:
        _fail("nothing to unlock with")
        return 2
    state = _engine(
        settings,
        "/secrets/unlock",
        {"unseal_key": material.unseal_key, "auth_secret": material.auth_secret},
    )
    if not state.get("usable"):
        _fail(state.get("detail") or "the store is still not usable")
        return 1
    _ok("the secret store is open")
    return 0


def _cmd_secret_status(args: argparse.Namespace) -> int:
    settings = _settings()
    state = _engine(settings, "/secrets/status")
    if not state.get("enabled"):
        print("secrets: off")
        return 0
    if state.get("usable"):
        _ok(f"secrets: open ({settings.secrets.vault_addr})")
    elif not state.get("reachable"):
        _fail(f"secrets: {settings.secrets.vault_addr} is unreachable")
    elif state.get("sealed"):
        _fail("secrets: the store is sealed — run `impi secret unlock`")
    else:
        _fail("secrets: the engine holds no credential — run `impi secret unlock`")
    if state.get("detail"):
        print(_dim(f"  {state['detail']}"))

    store = _store()
    try:
        policies = store.list_policies_sync()
        grants = store.list_grants_sync(now=_now_iso(), kind=KIND_SECRET)
        recent = store.list_audit_sync(limit=1)
    finally:
        store.close_sync()
    print(f"  policies    : {len(policies)}")
    print(f"  open windows: {len(grants)}")
    if recent:
        print(
            f"  last request: {recent[0].at}  {recent[0].principal} "
            f"-> {recent[0].decision}"
        )
    if not settings.secrets.approvers:
        print(_dim("  no approvers configured — every asking secret is unreachable"))
    return 0


def _cmd_secret_set(args: argparse.Namespace) -> int:
    settings = _settings()
    fields = dict(_split_field(item) for item in (args.field or []))
    if not fields:
        fields = {"value": _prompt(f"Value for {args.name}", secret=True)}

    async def _write() -> None:
        backend = await _open_vault(settings)
        try:
            await backend.write(args.name, fields)
        finally:
            await backend.close()

    asyncio.run(_write())
    _ok(f"{args.name} stored ({', '.join(sorted(fields))})")
    if _settings_policy_missing(args.name):
        print(
            _dim(
                "  no policy yet, so no agent can reach it. Grant one with:\n"
                f"  impi secret policy set {args.name} --subjects <agent>"
            )
        )
    return 0


def _split_field(item: str) -> tuple[str, str]:
    name, sep, value = item.partition("=")
    if not sep or not name:
        raise TaskError(f"--field wants NAME=VALUE, got {item!r}")
    return name, value


def _settings_policy_missing(name: str) -> bool:
    store = _store()
    try:
        return store.get_policy_sync(name) is None
    finally:
        store.close_sync()


def _cmd_secret_ls(args: argparse.Namespace) -> int:
    settings = _settings()

    async def _names() -> list[str]:
        backend = await _open_vault(settings)
        try:
            return await backend.names()
        finally:
            await backend.close()

    names = asyncio.run(_names())
    store = _store()
    try:
        policies = {p.name: p for p in store.list_policies_sync()}
    finally:
        store.close_sync()
    if not names and not policies:
        print("no secrets yet — add one with `impi secret set <name>`")
        return 0
    for name in sorted(set(names) | set(policies)):
        policy = policies.get(name)
        if policy is None:
            note = _dim("no policy — unreachable by every agent")
        elif not policy.subjects:
            note = _dim("no subjects — unreachable by every agent")
        else:
            window = _humanize(policy.max_grant_s) if policy.max_grant_s else "ask every time"
            note = f"{policy.approval}, {window}, for: {policy.subjects}"
        missing = "" if name in names else _dim("  (policy only — no value stored)")
        print(f"  {_bold(name):<28} {note}{missing}")
    return 0


def _cmd_secret_rm(args: argparse.Namespace) -> int:
    settings = _settings()
    if not args.yes and not _confirm(f"Remove {args.name} and its policy?", default=False):
        return 1

    async def _delete() -> None:
        backend = await _open_vault(settings)
        try:
            await backend.delete(args.name)
        finally:
            await backend.close()

    asyncio.run(_delete())
    store = _store()
    try:
        store.delete_policy_sync(args.name)
        # The windows go with it, or an agent keeps reaching something whose
        # permission has just been deleted.
        store.revoke_scope_sync(KIND_SECRET, args.name, now=_now_iso())
    finally:
        store.close_sync()
    _ok(f"{args.name} removed, with its policy and any open windows")
    return 0


def _cmd_secret_policy_set(args: argparse.Namespace) -> int:
    if args.approval not in APPROVALS:
        raise TaskError(f"--approval is one of: {', '.join(APPROVALS)}")
    max_grant = _duration(args.max_grant)
    subjects = ",".join(
        part.strip() for part in (args.subjects or "").split(",") if part.strip()
    )
    now = _now_iso()
    store = _store()
    try:
        existing = store.get_policy_sync(args.name)
        store.put_policy_sync(
            SecretPolicyRecord(
                name=args.name,
                approval=args.approval,
                max_grant_s=max_grant,
                subjects=subjects,
                description=args.description,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
        )
    finally:
        store.close_sync()
    _ok(f"policy for {args.name}: {args.approval}, for: {subjects or '(nobody)'}")
    if args.approval == APPROVAL_NEVER:
        print(_dim("  approval: never — every listed agent may use it unattended"))
    return 0


def _cmd_secret_policy_show(args: argparse.Namespace) -> int:
    store = _store()
    try:
        policies = (
            [p for p in store.list_policies_sync() if p.name == args.name]
            if args.name
            else store.list_policies_sync()
        )
    finally:
        store.close_sync()
    if not policies:
        _fail("no such policy" if args.name else "no policies yet")
        return 1
    for policy in policies:
        print(_bold(policy.name))
        print(f"  approval : {policy.approval}")
        print(
            "  window   : "
            + (_humanize(policy.max_grant_s) if policy.max_grant_s else "none (ask every time)")
        )
        print(f"  subjects : {policy.subjects or '(nobody)'}")
        if policy.description:
            print(f"  about    : {policy.description}")
    return 0


def _cmd_secret_grants(args: argparse.Namespace) -> int:
    store = _store()
    try:
        grants = store.list_grants_sync(
            now=_now_iso(), kind=KIND_SECRET, include_dead=args.all
        )
    finally:
        store.close_sync()
    if not grants:
        print("no open windows")
        return 0
    for grant in grants:
        state = (
            "revoked" if grant.revoked_at
            else f"until {local_time(from_iso(grant.expires_at), _zone())}"
        )
        print(
            f"  {_bold(grant.id):<16} {grant.principal} -> {grant.scope}  "
            f"{state}  {_dim('by ' + grant.granted_by)}"
        )
    return 0


def _cmd_secret_revoke(args: argparse.Namespace) -> int:
    store = _store()
    try:
        closed = store.revoke_grant_sync(args.grant_id, now=_now_iso())
    finally:
        store.close_sync()
    if not closed:
        _fail("no open window with that id")
        return 1
    _ok("window closed — the next request asks again")
    return 0


def _cmd_secret_audit(args: argparse.Namespace) -> int:
    store = _store()
    try:
        rows = store.list_audit_sync(
            limit=args.limit, kind=KIND_SECRET,
            principal=args.agent or "", scope=args.secret or "",
        )
    finally:
        store.close_sync()
    if not rows:
        print("nothing requested yet")
        return 0
    for row in rows:
        line = (
            f"  {local_time(from_iso(row.at), _zone())}  {_bold(row.decision):<24} "
            f"{row.principal} -> {row.scope}"
        )
        print(line)
        if row.detail:
            print(_dim(f"      {row.detail}"))
        if row.reason:
            print(_dim(f"      reason: {row.reason}"))
    return 0


def _humanize(seconds: int) -> str:
    return humanize_window(seconds)


# --- parser ----------------------------------------------------------------------



# --- tasks -------------------------------------------------------------------


def _store():
    return SqliteSessionStore(_settings().resolved_db_path)


def _admin(store):
    settings = _settings()
    return TaskAdmin(
        store, store,
        default_timezone=settings.scheduler.timezone,
        max_per_agent=settings.scheduler.max_tasks_per_agent,
    )


def _with_store(work) -> int:
    """Open the engine's database, do one thing, close it. The CLI runs in its
    own container: it may read and edit rows, but it never fires a run — it has
    no gateways to answer through."""
    store = _store()
    try:
        return asyncio.run(work(store))
    finally:
        store.close_sync()


def _find_task(store, wanted: str):
    found = store.get_task_sync(wanted)
    if found is not None:
        return found
    matches = [t for t in store.list_tasks_sync() if t.name == wanted]
    if len(matches) > 1:
        raise TaskError(
            f"{wanted!r} is the name of {len(matches)} tasks — use the id: "
            + ", ".join(t.id for t in matches)
        )
    if not matches:
        raise TaskError(f"no task {wanted!r}")
    return matches[0]


def _task_rows(tasks) -> list[tuple[str, ...]]:
    return [
        (
            task.name, task.id, task.agent, task.trigger_spec,
            local_time(from_iso(task.next_run_at), task.timezone) or "—",
            task.state, task.last_status or "never run",
        )
        for task in tasks
    ]


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """Columns sized to their content: a zone name like Europe/Belgrade is
    longer than any fixed width worth guessing at."""
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]
    print(_dim("  ".join(cell.ljust(width) for cell, width in zip(header, widths, strict=True))))
    for row in rows:
        line = "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        print(line.replace(row[0], _bold(row[0]), 1))


def _cmd_task_list(args: argparse.Namespace) -> int:
    store = _store()
    try:
        tasks = store.list_tasks_sync(getattr(args, "agent", None))
        if not tasks:
            print("no scheduled tasks")
            return 0
        _print_table(
            ("NAME", "ID", "AGENT", "SCHEDULE", "NEXT RUN", "STATE", "LAST"),
            _task_rows(tasks),
        )
        return 0
    finally:
        store.close_sync()


def _cmd_task_show(args: argparse.Namespace) -> int:
    store = _store()
    try:
        task = _find_task(store, args.task)
        print(f"{_bold(task.name)}  {_dim(task.id)}")
        print(f"  agent        {task.agent}")
        print(f"  schedule     {task.trigger_spec}  ({task.timezone or 'UTC'})")
        print(f"  mode         {task.mode}")
        print(f"  state        {task.state}")
        print(f"  next run     {local_time(from_iso(task.next_run_at), task.timezone) or '—'}")
        last = local_time(from_iso(task.last_run_at), task.timezone) or "—"
        print(f"  last run     {last}{'  ' + task.last_status if task.last_status else ''}")
        print(f"  runs/missed  {task.run_count}/{task.miss_count}"
              f"  failures in a row: {task.consecutive_failures}")
        print(f"  on missed    {task.on_missed}    notify: {task.notify}")
        print(f"  conversation {task.conversation_id} ({task.kind})")
        print(f"  prompt       {task.prompt}")
        return 0
    finally:
        store.close_sync()


def _cmd_task_runs(args: argparse.Namespace) -> int:
    store = _store()
    try:
        task = _find_task(store, args.task)
        runs = store.list_runs_sync(task.id, limit=args.limit)
        if not runs:
            print(f"{task.name} has not run yet")
            return 0
        _print_table(
            ("SCHEDULED FOR", "STATUS", "TOOK", "WHY"),
            [
                (
                    # In the task's own zone, the way every other surface prints
                    # a moment — this is a person reading their own schedule.
                    local_time(from_iso(run.scheduled_at), task.timezone),
                    run.status,
                    f"{run.duration_ms / 1000:.1f}s" if run.duration_ms else "—",
                    run.detail or "",
                )
                for run in runs
            ],
        )
        return 0
    finally:
        store.close_sync()


def _cmd_task_add(args: argparse.Namespace) -> int:
    async def work(store) -> int:
        sessions = store.list_sync(args.agent)
        chosen = next(
            (s for s in sessions if s.conversation_id == args.conversation), None
        )
        if chosen is None:
            known = ", ".join(s.conversation_id for s in sessions) or "none yet"
            print(
                f"✘ {args.agent} has no conversation {args.conversation!r} "
                f"(known: {known})\n"
                "  A task belongs to a conversation — talk to the agent there first, "
                "or ask it to schedule the task itself.",
                file=sys.stderr,
            )
            return 1
        view = await _admin(store).schedule_in(
            args.agent, channel_id=chosen.channel_id,
            conversation_id=chosen.conversation_id, kind=chosen.kind,
            name=args.name, prompt=args.prompt, schedule=args.schedule,
            mode=args.mode, timezone=args.tz or "", notify=args.notify,
            on_missed=args.on_missed,
        )
        print(f"✔ {view.name} ({view.id}) — next: {view.next_run}")
        for moment in view.upcoming[1:]:
            print(f"    then {moment}")
        return 0

    return _with_store(work)


def _cmd_task_rm(args: argparse.Namespace) -> int:
    async def work(store) -> int:
        task = _find_task(store, args.task)
        if not args.yes and not _confirm(f"Delete task {task.name} ({task.id})?"):
            return 1
        await _admin(store).cancel(task.agent, task.id)
        print(f"✔ {task.name} deleted, with its run history")
        return 0

    return _with_store(work)


def _cmd_task_pause(args: argparse.Namespace) -> int:
    paused = args.task_command == "pause"

    async def work(store) -> int:
        task = _find_task(store, args.task)
        view = await _admin(store).set_paused(task.agent, task.id, paused)
        print(f"✔ {view.name} {'paused' if paused else 'resumed'}"
              + (f" — next: {view.next_run}" if not paused else ""))
        return 0

    return _with_store(work)


def _cmd_task_run_now(args: argparse.Namespace) -> int:
    store = _store()
    try:
        task = _find_task(store, args.task)
        # Only ever a request: the engine owns the gateways, so it does the
        # running. This just brings the next occurrence forward.
        if not store.request_run_now_sync(task.id, now=to_iso(utc_now()) or ""):
            print(f"✘ {task.name} is paused or already running", file=sys.stderr)
            return 1
        print(f"✔ {task.name} is due now — the engine picks it up within a tick")
        return 0
    finally:
        store.close_sync()


def _cmd_task_status(args: argparse.Namespace) -> int:
    settings = _settings()
    store = _store()
    try:
        verdict, detail = liveness(
            store.read_heartbeat_sync(), now=utc_now(),
            enabled=settings.scheduler.enabled,
        )
        mark = "✔" if verdict == ALIVE else "✘"
        print(f"{mark} scheduler {verdict}: {detail}")
        return 0 if verdict in (ALIVE, "absent") else 1
    finally:
        store.close_sync()


# --- impi sessions ------------------------------------------------------------


def _cmd_sessions(args: argparse.Namespace) -> int:
    """Conversation memory, on the database this engine actually writes.

    The library ships the same three commands as `python -m crucible.sessions_cli`,
    but that entry point resolves the inventory with crucible's own default
    filename — pointed at an impi deployment it opens a file nobody writes and
    reports an empty stand. Here the path comes from impi's settings, the same
    ones the engine boots with."""
    store = _store()
    try:
        args.run(_settings(), store, args)
        return 0
    finally:
        store.close_sync()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="impi", description="impi engine companion CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    agent = sub.add_parser("agent", help="manage agent profiles and their bots")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)

    add = agent_sub.add_parser("add", help="create an agent: bot + profile + .env")
    add.add_argument("--name", help="agent slug (lowercase, digits, hyphens)")
    add.add_argument("--role", help="one-line role")
    add.add_argument("--display-name")
    add.add_argument("--description")
    add.add_argument("--gateway", choices=["mattermost", "slack", "ws"])
    add.add_argument("--mm-url", help="Mattermost base URL (default: MATTERMOST_URL)")
    add.add_argument("--admin-token", help="MM system-admin PAT (auto bot creation)")
    add.add_argument("--bot-token", help="existing bot token (skip auto creation)")
    add.add_argument("--slack-bot-token")
    add.add_argument("--slack-app-token")
    add.add_argument("--team", help="MM team for the new bot (default: first team)")
    add.add_argument("--agents-dir", help="profiles directory (default: AGENTS_PATH)")
    add.add_argument("--env-file", help="target .env (default: DOTENV_PATH)")
    add.add_argument("--yes", action="store_true", help="non-interactive")
    add.set_defaults(func=_cmd_agent_add)

    lst = agent_sub.add_parser("list", help="list profiles and token status")
    lst.add_argument("--agents-dir")
    lst.set_defaults(func=_cmd_agent_list)

    skill = sub.add_parser("skill", help="the shared skill library")
    skill_sub = skill.add_subparsers(dest="skill_command", required=True)
    for name, help_text, handler in (
        ("list", "list installed skills and who uses them", _cmd_skill_list),
        ("show", "one skill in detail", _cmd_skill_show),
        ("install", "install a skill from a directory or a repository", _cmd_skill_install),
        ("update", "re-fetch a skill from its source", _cmd_skill_update),
        ("remove", "delete a skill from the library", _cmd_skill_remove),
        ("assign", "give a skill to an agent (or take it away)", _cmd_skill_assign),
    ):
        cmd = skill_sub.add_parser(name, help=help_text)
        cmd.add_argument("--skills-dir", help="skill library (default: SKILLS_PATH)")
        cmd.add_argument("--agents-dir", help="profiles directory (default: AGENTS_PATH)")
        if name == "install":
            cmd.add_argument(
                "source",
                help="a directory, owner/repo[/path][@ref], or a git URL[#path][@ref]",
            )
            cmd.add_argument("--name", help="install under this name (default: the skill's own)")
            cmd.add_argument("--force", action="store_true", help="overwrite an installed skill")
        elif name == "assign":
            cmd.add_argument("name", help="skill name")
            cmd.add_argument("agent", help="agent name")
            cmd.add_argument("--remove", action="store_true", help="unassign instead")
        elif name != "list":
            cmd.add_argument("name", help="skill name")
        if name in ("install", "update", "remove", "assign"):
            cmd.add_argument("--yes", action="store_true", help="don't ask")
        if name == "remove":
            cmd.add_argument("--force", action="store_true", help="remove even if assigned")
        cmd.set_defaults(func=handler)

    task = sub.add_parser("task", help="scheduled and recurring work")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    for name, help_text, handler in (
        ("list", "every scheduled task and when it next runs", _cmd_task_list),
        ("show", "one task in detail", _cmd_task_show),
        ("runs", "what happened on the last runs, and why", _cmd_task_runs),
        ("add", "schedule a task in a conversation the agent already has", _cmd_task_add),
        ("rm", "delete a task and its run history", _cmd_task_rm),
        ("pause", "stop a task from running", _cmd_task_pause),
        ("resume", "let a paused task run again, on its original rhythm", _cmd_task_pause),
        ("run-now", "ask the engine to run a task at once", _cmd_task_run_now),
        ("status", "is the scheduler alive, and what does it wake for next", _cmd_task_status),
    ):
        cmd = task_sub.add_parser(name, help=help_text)
        if name == "list":
            cmd.add_argument("--agent", help="only this agent's tasks")
        elif name == "add":
            cmd.add_argument("--agent", required=True)
            cmd.add_argument(
                "--conversation", required=True,
                help="conversation id the task belongs to (see `impi task list`)",
            )
            cmd.add_argument("--name", required=True, help="short name, unique per agent")
            cmd.add_argument("--prompt", required=True, help="what the agent should do")
            cmd.add_argument(
                "--schedule", required=True,
                help="'in 2h', '2026-08-09T09:00', 'every 15m' or '0 9 * * 1-5'",
            )
            cmd.add_argument("--mode", default="turn", choices=["turn", "prompt"])
            cmd.add_argument("--tz", help="IANA zone (default: SCHEDULER_TIMEZONE)")
            cmd.add_argument(
                "--notify", default="failures", choices=["failures", "always", "never"]
            )
            cmd.add_argument("--on-missed", default="run", choices=["run", "skip"])
        elif name != "status":
            cmd.add_argument("task", help="task name or id")
        if name == "runs":
            cmd.add_argument("--limit", type=int, default=20)
        if name == "rm":
            cmd.add_argument("--yes", action="store_true", help="don't ask")
        cmd.set_defaults(func=handler)


    sessions = sub.add_parser("sessions", help="conversation memory (pi sessions)")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    s_list = sessions_sub.add_parser("list", help="every conversation the engine remembers")
    s_list.add_argument("--agent", default=None)
    s_list.set_defaults(func=_cmd_sessions, run=sessions_cli.cmd_list)
    s_del = sessions_sub.add_parser(
        "delete", help="forget one conversation (inventory row + the runtime's memory)"
    )
    s_del.add_argument("agent")
    s_del.add_argument("conversation_id")
    s_del.set_defaults(func=_cmd_sessions, run=sessions_cli.cmd_delete)
    s_purge = sessions_sub.add_parser("purge-idle", help="forget conversations idle for N+ days")
    s_purge.add_argument("--days", type=int, required=True)
    s_purge.add_argument("--agent", default=None)
    s_purge.set_defaults(func=_cmd_sessions, run=sessions_cli.cmd_purge_idle)

    mm = sub.add_parser("mm", help="Mattermost helpers")
    mm_sub = mm.add_subparsers(dest="mm_command", required=True)
    boot = mm_sub.add_parser(
        "bootstrap-token",
        help="log in with admin credentials and print a fresh PAT to stdout",
    )
    boot.add_argument("--url", required=True)
    boot.add_argument("--login-id", required=True)
    boot.add_argument(
        "--password-stdin", action="store_true", help="read the password from stdin"
    )
    boot.set_defaults(func=_cmd_mm_bootstrap_token)

    provision = sub.add_parser("provision", help="provision engine-owned bots")
    prov_sub = provision.add_subparsers(dest="target", required=True)
    support = prov_sub.add_parser("support", help="bot for the bundled support agent")
    support.add_argument("--username", default="support")
    support.add_argument("--mm-url")
    support.add_argument("--admin-token")
    support.add_argument("--team")
    support.add_argument("--env-file")
    support.add_argument("--yes", action="store_true")
    support.set_defaults(func=_cmd_provision)

    ws = sub.add_parser("ws", help="ws gateway helpers")
    ws_sub = ws.add_subparsers(dest="ws_command", required=True)
    add_service = ws_sub.add_parser(
        "add-service",
        help="register a client service on the ws hub (generates its token)",
    )
    add_service.add_argument("name", help="service slug (lowercase, digits, hyphens)")
    add_service.add_argument(
        "--agents", help="CSV allowlist of agents it may address (default: all ws agents)"
    )
    add_service.add_argument("--env-file")
    add_service.set_defaults(func=_cmd_ws_add_service)

    secret = sub.add_parser("secret", help="the secret store and who may reach it")
    secret_sub = secret.add_subparsers(dest="secret_command", required=True)

    init = secret_sub.add_parser("init", help="initialise a fresh Vault for impi")
    init.add_argument("--env-file")
    init.set_defaults(func=_cmd_secret_init)

    unlock = secret_sub.add_parser(
        "unlock", help="open the store on the running engine (asks for the key)"
    )
    unlock.set_defaults(func=_cmd_secret_unlock)

    status = secret_sub.add_parser("status", help="open or locked, and what is configured")
    status.set_defaults(func=_cmd_secret_status)

    set_secret = secret_sub.add_parser("set", help="store a value (prompts, never echoes)")
    set_secret.add_argument("name")
    set_secret.add_argument(
        "--field", action="append",
        help="NAME=VALUE for a multi-field secret; repeatable. Omit to be prompted "
             "for the single 'value' field.",
    )
    set_secret.set_defaults(func=_cmd_secret_set)

    ls = secret_sub.add_parser("ls", help="what is stored and who may reach it")
    ls.set_defaults(func=_cmd_secret_ls)

    rm = secret_sub.add_parser("rm", help="remove a value, its policy and its windows")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true")
    rm.set_defaults(func=_cmd_secret_rm)

    policy = secret_sub.add_parser("policy", help="who may ask for a secret, and how")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_set = policy_sub.add_parser("set", help="create or replace a policy")
    policy_set.add_argument("name")
    policy_set.add_argument(
        "--subjects", default="",
        help="CSV of agents that may ask (empty = nobody, which is the default)",
    )
    policy_set.add_argument(
        "--approval", default="always", choices=list(APPROVALS),
        help="always = ask a human unless a window is open; never = unattended",
    )
    policy_set.add_argument(
        "--max-grant", default="1h",
        help="longest window a human may leave open (15m, 1h, or 0 to always ask)",
    )
    policy_set.add_argument("--description", default="")
    policy_set.set_defaults(func=_cmd_secret_policy_set)
    policy_show = policy_sub.add_parser("show", help="show one policy, or all of them")
    policy_show.add_argument("name", nargs="?")
    policy_show.set_defaults(func=_cmd_secret_policy_show)

    grants = secret_sub.add_parser("grants", help="windows currently left open")
    grants.add_argument("--all", action="store_true", help="include expired and revoked")
    grants.set_defaults(func=_cmd_secret_grants)

    revoke = secret_sub.add_parser("revoke", help="close a window now")
    revoke.add_argument("grant_id")
    revoke.set_defaults(func=_cmd_secret_revoke)

    audit = secret_sub.add_parser("audit", help="every request, granted or not")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--agent")
    audit.add_argument("--secret")
    audit.set_defaults(func=_cmd_secret_audit)

    health = sub.add_parser("health", help="check Mattermost + agents dir")
    health.set_defaults(func=_cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TaskError as exc:
        # An expected refusal — no such task, a schedule that doesn't parse — is
        # a message, not a stack trace.
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
