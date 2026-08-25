"""``ward-admin`` — the operator's side of the secret broker.

Every verb here is a client call. The broker runs in its own container beside
the store it opens, holds the credential to it, and answers these routes only to
the operator certificate; an agent's certificate reaches exactly one route, and
it is not any of these. So there is nothing to read locally and nothing to
configure beyond where to ask and what to prove — see ``identity.py``.

It is a separate program from the engine on purpose. Secrets are a tool a
deployment may or may not have, so the tool brings its own operator surface
rather than growing one inside an application that would otherwise never need to
know the protocol exists.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

from wardline.console import (
    CommandError,
    bold,
    confirm,
    dim,
    fail,
    humanize,
    local_time,
    ok,
    parse_duration,
    prompt,
)
from wardline.identity import IdentityError, certs_dir, operator_identity
from wardline.wire import APPROVAL_NEVER, APPROVALS

# How the hints below spell this tool. A deployment that wraps it in its own CLI
# says so here, so "run X" tells the operator what they would actually type.
_PROG = os.environ.get("WARD_ADMIN_PROG", "").strip() or "ward-admin"


# --- talking to the broker ------------------------------------------------------


def _client() -> httpx.Client:
    """A client that proves it is the operator, and checks it reached the broker
    and not something answering in its place."""
    identity = operator_identity()
    # An explicit context rather than httpx's path shorthand: the client
    # certificate and the CA have to end up on the same context, and this is the
    # only spelling where that is visible.
    return httpx.Client(base_url=identity.url, verify=identity.context(), timeout=30.0)


def _ask(method: str, path: str, **kwargs) -> dict:
    try:
        with _client() as client:
            response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise CommandError(f"cannot reach the secret broker: {exc}") from exc
    payload = response.json() if response.content else {}
    if response.status_code == 404:
        # The operator routes answer 404 to anyone who is not the operator, so
        # this is as likely to be the wrong certificate as the wrong path.
        raise CommandError("the broker did not recognize this operator certificate")
    if response.status_code >= 400:
        raise CommandError(str(payload.get("error") or f"HTTP {response.status_code}"))
    return payload


# --- the store's state ----------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    state = _ask("GET", "/status")
    if state.get("usable"):
        ok(f"secrets: open ({operator_identity().url})")
    elif not state.get("reachable"):
        fail("secrets: the store is unreachable from the broker")
    elif state.get("sealed"):
        fail(f"secrets: the store is sealed — run `{_PROG} unlock`")
    else:
        fail(f"secrets: the broker holds no credential — run `{_PROG} unlock`")
    if state.get("detail"):
        print(dim(f"  {state['detail']}"))
    policies = _ask("GET", "/policies").get("policies", [])
    grants = _ask("GET", "/grants").get("grants", [])
    recent = _ask("GET", "/audit", params={"limit": 1}).get("audit", [])
    print(f"  policies    : {len(policies)}")
    print(f"  open windows: {len(grants)}")
    if recent:
        print(f"  last request: {recent[0]['at']}  {recent[0]['agent']} "
              f"-> {recent[0]['decision']}")
    return 0


def _cmd_unlock(args: argparse.Namespace) -> int:
    """Open the store on the running broker.

    Three ways in, and the typed one is the worst of them: a key typed at a
    prompt is a key in somebody's scrollback, and in the transcript of whatever
    ran the command. So a file or a pipe is offered first, and the material the
    ceremony produced is written in exactly that shape.
    """
    material = _material(args)
    key = material.get(_ENV_UNSEAL) or prompt("Store unseal key", secret=True)
    auth = material.get(_ENV_SECRET_ID) or prompt("Broker credential (secret id)", secret=True)
    state = _ask("POST", "/unlock", json={"unseal_key": key, "auth_secret": auth})
    if not state.get("usable"):
        fail(state.get("detail") or "the store is still not usable")
        return 1
    ok("the secret store is open")
    return 0


# What `ward init --machine` writes and this reads back. Two names, one shape,
# so the file the ceremony produced is the file the unlock consumes.
_ENV_UNSEAL = "WARD_UNSEAL_KEY"
_ENV_SECRET_ID = "WARD_SECRET_ID"


def _material(args: argparse.Namespace) -> dict[str, str]:
    """The unlock material from a file or a pipe, if either was offered."""
    if getattr(args, "source", ""):
        try:
            text = Path(args.source).read_text(encoding="utf-8")
        except OSError as exc:
            raise CommandError(f"cannot read {args.source}: {exc.strerror}") from exc
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        return {}
    found = _parse_material(text)
    missing = [name for name in (_ENV_UNSEAL, _ENV_SECRET_ID) if name not in found]
    if missing:
        # Half the material is not a shortcut, it is a surprise prompt in the
        # middle of a script. Say which line is absent.
        raise CommandError(
            f"no {' or '.join(missing)} in that material — expected the KEY=VALUE "
            "lines `ward init --machine` writes"
        )
    return found


def _parse_material(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        found[name.strip()] = value.strip()
    return found


# --- values ----------------------------------------------------------------------


def _cmd_set(args: argparse.Namespace) -> int:
    fields = dict(_split_field(item) for item in (args.field or []))
    if not fields:
        fields = {"value": prompt(f"Value for {args.name}", secret=True)}
    result = _ask("PUT", f"/secrets/{args.name}", json={"fields": fields})
    ok(f"{args.name} stored ({', '.join(result.get('fields', []))})")
    known = {p["name"] for p in _ask("GET", "/policies").get("policies", [])}
    if args.name not in known:
        print(
            dim(
                "  no policy yet, so no agent can reach it. Grant one with:\n"
                f"  {_PROG} policy set {args.name} --subjects <agent>"
            )
        )
    return 0


def _split_field(item: str) -> tuple[str, str]:
    name, sep, value = item.partition("=")
    if not sep or not name:
        raise CommandError(f"--field wants NAME=VALUE, got {item!r}")
    return name, value


def _cmd_ls(args: argparse.Namespace) -> int:
    entries = _ask("GET", "/secrets").get("secrets", [])
    if not entries:
        print(f"no secrets yet — add one with `{_PROG} set <name>`")
        return 0
    for entry in entries:
        policy = entry.get("policy")
        if policy is None:
            note = dim("no policy — unreachable by every agent")
        elif not policy["subjects"]:
            note = dim("no subjects — unreachable by every agent")
        else:
            window = (
                humanize(policy["max_grant_s"])
                if policy["max_grant_s"] else "ask every time"
            )
            note = f"{policy['approval']}, {window}, for: {policy['subjects']}"
        missing = "" if entry.get("stored") else dim("  (policy only — no value stored)")
        print(f"  {bold(entry['name']):<28} {note}{missing}")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    if not args.yes and not confirm(f"Remove {args.name} and its policy?", default=False):
        return 1
    result = _ask("DELETE", f"/secrets/{args.name}")
    ok(
        f"{args.name} removed, with its policy and "
        f"{result.get('windows_closed', 0)} open window(s)"
    )
    return 0


# --- policies ---------------------------------------------------------------------


def _cmd_policy_set(args: argparse.Namespace) -> int:
    if args.approval not in APPROVALS:
        raise CommandError(f"--approval is one of: {', '.join(APPROVALS)}")
    subjects = ",".join(
        part.strip() for part in (args.subjects or "").split(",") if part.strip()
    )
    _ask(
        "PUT",
        f"/policies/{args.name}",
        json={
            "approval": args.approval,
            "max_grant_s": parse_duration(args.max_grant),
            "subjects": subjects,
            "description": args.description,
        },
    )
    ok(f"policy for {args.name}: {args.approval}, for: {subjects or '(nobody)'}")
    if args.approval == APPROVAL_NEVER:
        print(dim("  approval: never — every listed agent may use it unattended"))
    return 0


def _cmd_policy_show(args: argparse.Namespace) -> int:
    policies = _ask("GET", "/policies").get("policies", [])
    if args.name:
        policies = [p for p in policies if p["name"] == args.name]
    if not policies:
        fail("no such policy" if args.name else "no policies yet")
        return 1
    for policy in policies:
        print(bold(policy["name"]))
        print(f"  approval : {policy['approval']}")
        window = policy["max_grant_s"]
        print("  window   : " + (humanize(window) if window else "none (ask every time)"))
        print(f"  subjects : {policy['subjects'] or '(nobody)'}")
        if policy["description"]:
            print(f"  about    : {policy['description']}")
    return 0


# --- windows and the ledger --------------------------------------------------------


def _cmd_grants(args: argparse.Namespace) -> int:
    params = {"all": "1"} if args.all else None
    grants = _ask("GET", "/grants", params=params).get("grants", [])
    if not grants:
        print("no open windows")
        return 0
    for grant in grants:
        state = (
            "revoked" if grant["revoked_at"]
            else f"until {local_time(grant['expires_at'])}"
        )
        print(
            f"  {bold(grant['id']):<16} {grant['agent']} -> {grant['secret']}  "
            f"{state}  {dim('by ' + grant['granted_by'])}"
        )
    return 0


def _cmd_revoke(args: argparse.Namespace) -> int:
    if not _ask("DELETE", f"/grants/{args.grant_id}").get("closed"):
        fail("no open window with that id")
        return 1
    ok("window closed — the next request asks again")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    params: dict[str, str] = {"limit": str(args.limit)}
    if args.agent:
        params["agent"] = args.agent
    if args.secret:
        params["secret"] = args.secret
    if args.kind:
        params["kind"] = args.kind
    rows = _ask("GET", "/audit", params=params).get("audit", [])
    if not rows:
        print("nothing requested yet")
        return 0
    for row in rows:
        # An operator row is a person doing something, not an agent being
        # answered; saying which keeps the two from reading alike.
        mark = "" if row.get("kind", "secret") == "secret" else dim("  [operator]")
        print(
            f"  {local_time(row['at'])}  {bold(row['decision']):<24} "
            f"{row['agent']} -> {row['secret']}{mark}"
        )
        if row["detail"]:
            print(dim(f"      {row['detail']}"))
        if row["reason"]:
            print(dim(f"      reason: {row['reason']}"))
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    """Replace the credential the broker logs in with.

    The new one is a credential, so it follows the same rule as the ceremony's:
    with --machine it goes to stdout as KEY=VALUE and nothing else, for a caller
    that writes it to a file. Printed to a terminal it would live in scrollback,
    and in the transcript of whatever ran the command.
    """
    secret_id = str(_ask("POST", "/rotate", json={}).get("secret_id") or "")
    if not secret_id:
        raise CommandError("the broker returned no credential")
    if args.machine:
        print(f"WARD_SECRET_ID={secret_id}")
        print("the broker's credential was replaced", file=sys.stderr)
        return 0
    ok("the broker's credential was replaced")
    print(f"  new secret id: {secret_id}")
    print(dim("  keep it with the unseal key — the next unlock needs this one"))
    return 0


# --- identities ---------------------------------------------------------------------


def _cmd_cert(args: argparse.Namespace) -> int:
    """Mint an agent's identity.

    The certificate authority lives with the broker and its key goes nowhere
    else, so this asks rather than issues. That is what stops anything on this
    side from inventing an agent: a new name needs the operator.
    """
    issued = _ask("POST", f"/certs/{args.agent}", json={})
    root = Path(args.dir) if args.dir else certs_dir()
    if root is None:
        raise CommandError("nowhere to write it (SECRET_BROKER_CERTS_DIR or --dir)")
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{args.agent}.crt").write_text(issued["certificate"], encoding="utf-8")
    key = root / f"{args.agent}.key"
    key.write_text(issued["key"], encoding="utf-8")
    key.chmod(0o600)
    (root / "ca.crt").write_text(issued["ca"], encoding="utf-8")
    ok(f"identity for {args.agent} written to {root}")
    print(dim("  used from that agent's next request — no restart"))
    return 0


# --- parser ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="the secret broker: the store, who may reach it, and the ledger",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cert = sub.add_parser(
        "cert", help="mint an agent's identity for the broker (asks the broker)"
    )
    cert.add_argument("agent")
    cert.add_argument("--dir", help="where to write it (default: SECRET_BROKER_CERTS_DIR)")
    cert.set_defaults(func=_cmd_cert)

    unlock = sub.add_parser(
        "unlock",
        help="open the store on the running broker (from a file, a pipe, or a prompt)",
    )
    unlock.add_argument(
        "--from", dest="source", metavar="FILE",
        help="read the material from a file of KEY=VALUE lines, as written by "
             "`ward init --machine`; also read from stdin when it is not a terminal",
    )
    unlock.set_defaults(func=_cmd_unlock)

    status = sub.add_parser("status", help="open or locked, and what is configured")
    status.set_defaults(func=_cmd_status)

    set_secret = sub.add_parser("set", help="store a value (prompts, never echoes)")
    set_secret.add_argument("name")
    set_secret.add_argument(
        "--field", action="append",
        help="NAME=VALUE for a multi-field secret; repeatable. Omit to be prompted "
             "for the single 'value' field.",
    )
    set_secret.set_defaults(func=_cmd_set)

    ls = sub.add_parser("ls", help="what is stored and who may reach it")
    ls.set_defaults(func=_cmd_ls)

    rm = sub.add_parser("rm", help="remove a value, its policy and its windows")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true")
    rm.set_defaults(func=_cmd_rm)

    policy = sub.add_parser("policy", help="who may ask for a secret, and how")
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
    policy_set.set_defaults(func=_cmd_policy_set)
    policy_show = policy_sub.add_parser("show", help="show one policy, or all of them")
    policy_show.add_argument("name", nargs="?")
    policy_show.set_defaults(func=_cmd_policy_show)

    grants = sub.add_parser("grants", help="windows currently left open")
    grants.add_argument("--all", action="store_true", help="include expired and revoked")
    grants.set_defaults(func=_cmd_grants)

    revoke = sub.add_parser("revoke", help="close a window now")
    revoke.add_argument("grant_id")
    revoke.set_defaults(func=_cmd_revoke)

    rotate = sub.add_parser(
        "rotate", help="replace the broker's own credential and destroy the old one"
    )
    rotate.add_argument(
        "--machine", action="store_true",
        help="print the new credential as KEY=VALUE on stdout, nothing else",
    )
    rotate.set_defaults(func=_cmd_rotate)

    audit = sub.add_parser("audit", help="every request, granted or not")
    audit.add_argument("--limit", type=int, default=20)
    audit.add_argument("--agent")
    audit.add_argument("--secret")
    audit.add_argument(
        "--kind", default="",
        help="only requests for a credential (`secret`) or only what an operator "
             "did from chat (`operator`); both by default",
    )
    audit.set_defaults(func=_cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (CommandError, IdentityError) as exc:
        # Both are the operator's to fix — a wrong flag, a missing certificate,
        # a broker that is not there. A traceback would say nothing they need.
        fail(str(exc))
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print()
        return 130


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    sys.exit(main())
