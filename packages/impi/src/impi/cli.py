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
import sys
from pathlib import Path

import httpx

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
    answer = input(f"{_bold(question)} {_dim(hint)} ").strip().lower()
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
    from crucible.profiles.errors import ProfileError
    from crucible.profiles.loader import FsProfileStore

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


# --- parser ----------------------------------------------------------------------


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

    health = sub.add_parser("health", help="check Mattermost + agents dir")
    health.set_defaults(func=_cmd_health)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
