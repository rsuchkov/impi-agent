"""``ward`` — set the store up once, then run it.

Two verbs. `init` is the ceremony: initialise the store, create the certificate
authority, issue ward's own certificate and the operator's, and print the
material a human has to keep. `serve` is the process the container runs.

Everything an operator does afterwards — storing values, writing policies,
reading the ledger — goes through `impi ward`, which reaches this process over
mutual TLS with the operator certificate. There is no second CLI to learn, and
no path to the store that does not pass the door.
"""

import argparse
import asyncio
import logging
import sys
import time
from typing import TextIO

from ward.app import run
from ward.ca import OPERATOR_CN, CertificateAuthority, Issued
from ward.config import WardSettings, load_settings
from ward.ports import SecretBackendError
from ward.vault import VaultBackend, VaultBootstrap

# How long the ceremony waits for the store's listener, and how often it looks.
# Generous: the cost of waiting too long is a slow first install, and the cost of
# not waiting long enough is an install that fails on a machine slower than the
# one this was tried on.
_STORE_WAIT_S = 60.0
_STORE_POLL_S = 0.5


async def _initialise(backend: VaultBackend, *, out: TextIO) -> VaultBootstrap:
    """Wait for the store to answer, then run the ceremony.

    `ward init` runs in a container brought up beside the store, both at once, so
    whether the store's listener is ready when the ceremony starts is a race —
    and losing it ends a first install with "unreachable" and no sign that
    running the same command again would work.

    Compose is told to wait too (the store has a healthcheck and this service
    depends on it being healthy), but only for `up`: not every compose runtime
    applies a dependency condition to the one-off container `run` creates.
    Waiting here is what makes the ceremony safe on all of them.

    On the deadline this returns anyway rather than reporting its own failure —
    the ceremony below produces the message that says what is wrong, and two
    spellings of "unreachable" is one too many.
    """
    deadline = time.monotonic() + _STORE_WAIT_S
    announced = False
    while not (await backend.status()).reachable and time.monotonic() < deadline:
        if not announced:
            print("waiting for the store to come up...", file=out)
            announced = True
        await asyncio.sleep(_STORE_POLL_S)
    return await backend.bootstrap()


def _cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.ca_cert.exists() and not args.force:
        print(f"a certificate authority already exists at {settings.tls}", file=sys.stderr)
        return 2

    backend = VaultBackend(settings.vault_addr, mount=settings.vault_mount)
    try:
        material = asyncio.run(_initialise(backend, out=sys.stderr))
    except SecretBackendError as exc:
        print(f"could not initialise the store: {exc}", file=sys.stderr)
        if "already initialized" in str(exc):
            # --force replaces the certificate authority, and cannot touch a
            # store that already exists — saying so here is the difference
            # between an operator who knows their material is still live and one
            # who thinks they just replaced it.
            print(
                "\nThat store keeps its own keys, and no flag here rotates them. To\n"
                "replace the broker's credential use `ward rotate`; to start over,\n"
                "remove the store's volume — which deletes every value in it.",
                file=sys.stderr,
            )
        return 1
    except Exception as exc:
        print(f"could not initialise the store: {exc}", file=sys.stderr)
        return 1
    finally:
        asyncio.run(backend.close())

    ca, ca_material = CertificateAuthority.create()
    ca_material.write(settings.ca_cert, settings.ca_key)
    ca.issue_server(settings.names).write(settings.server_cert, settings.server_key)
    operator = ca.issue_client(OPERATOR_CN)
    operator.write(settings.tls / "operator.crt", settings.tls / "operator.key")
    # In machine mode stdout carries the material and NOTHING else, so every
    # human word — including this one — goes to stderr.
    _hand_out(settings, ca, operator, out=sys.stderr if args.machine else sys.stdout)

    if args.machine:
        # stdout carries the material and nothing else, so a caller can put it
        # straight into a file without it passing a terminal — which is where a
        # printed credential ends up in somebody's scrollback, or worse, in the
        # transcript of whatever ran the command.
        _emit(material)
        print(
            f"certificate authority and certificates written to {settings.tls}",
            file=sys.stderr,
        )
        return 0

    print(f"certificate authority and certificates written to {settings.tls}")
    print()
    print("Keep these somewhere safe — they are shown once and nowhere else:")
    print(f"  unseal key : {material.unseal_key}")
    print(f"  secret id  : {material.secret_id}")
    print(f"  role id    : {material.role_id}")
    print()
    print(
        "The root token was destroyed: nothing needs it again, and the unseal key\n"
        "regenerates one if that ever changes (`vault operator generate-root`).\n"
        "The role id goes in ward's configuration (WARD_ROLE_ID). The unseal key\n"
        "and the secret id are needed after every restart: by hand with\n"
        "`impi ward unlock`, or unattended by pointing WARD_UNSEAL_KEY_FILE and\n"
        "WARD_SECRET_ID_FILE at files holding them — see docs/secrets.md for what\n"
        "that trade costs."
    )
    return 0


def _emit(material: VaultBootstrap) -> None:
    """The machine contract: KEY=VALUE on stdout, one per line, nothing else."""
    print(f"WARD_UNSEAL_KEY={material.unseal_key}")
    print(f"WARD_SECRET_ID={material.secret_id}")
    print(f"WARD_ROLE_ID={material.role_id}")


def _hand_out(
    settings: WardSettings,
    ca: CertificateAuthority,
    operator: Issued,
    *,
    # Resolved at the call, not bound here: a default evaluated at import time
    # captures whatever sys.stdout was then, which is not what a caller that
    # redirects it later gets.
    out: TextIO | None = None,
) -> None:
    """Put the identities where the people and programs that need them can read
    them — and, as much, keep them apart.

    Two directories, because two audiences. The agents' one is mounted by the
    engine, whose container is where the agents run; the operator's is mounted
    only by the tool an operator runs. A single directory would mean an agent
    could read the certificate that administers the broker.

    Without this the ceremony would end with the only certificate that may drive
    the broker locked inside the broker's own volume, and every administrative
    command answering 404.

    The authority's key is in neither, and never is: both are mounted somewhere
    else, and a signing key there would let that side mint an agent.
    """
    out = out if out is not None else sys.stdout
    try:
        # The CA goes in both: everyone verifies the broker with it, and it
        # authenticates nobody on its own.
        settings.issued.mkdir(parents=True, exist_ok=True)
        (settings.issued / "ca.crt").write_text(ca.certificate, encoding="utf-8")
        settings.operator.mkdir(parents=True, exist_ok=True)
        operator.write(
            settings.operator / "operator.crt", settings.operator / "operator.key"
        )
        (settings.operator / "ca.crt").write_text(ca.certificate, encoding="utf-8")
    except OSError as exc:
        print(
            f"could not hand the identities out ({exc.strerror}).\n"
            "Copy operator.crt, operator.key and ca.crt out of "
            f"{settings.tls} by hand, or mount that directory — until then no "
            "administrative command can authenticate.",
            file=sys.stderr,
        )
        return
    print(f"the operator identity is in {settings.operator}", file=out)


def _cmd_serve(args: argparse.Namespace) -> int:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 130
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ward", description="the secret broker")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="initialise the store and create the certificates")
    init.add_argument("--force", action="store_true", help="replace an existing authority")
    init.add_argument(
        "--machine", action="store_true",
        help="print the material as KEY=VALUE on stdout (everything else on stderr), "
             "so a caller can capture it without it passing a terminal",
    )
    init.set_defaults(func=_cmd_init)

    serve = sub.add_parser("serve", help="run the broker")
    serve.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())


__all__ = ["WardSettings", "build_parser", "main"]
