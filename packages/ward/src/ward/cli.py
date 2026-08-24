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

from ward.app import run
from ward.ca import OPERATOR_CN, CertificateAuthority
from ward.config import WardSettings, load_settings
from ward.vault import VaultBackend


def _cmd_init(args: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.ca_cert.exists() and not args.force:
        print(f"a certificate authority already exists at {settings.tls}", file=sys.stderr)
        return 2

    backend = VaultBackend(settings.vault_addr, mount=settings.vault_mount)
    try:
        material = asyncio.run(backend.bootstrap())
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

    print(f"certificate authority and certificates written to {settings.tls}")
    print()
    print("Keep these somewhere safe — they are shown once and nowhere else:")
    print(f"  unseal key : {material.unseal_key}")
    print(f"  root token : {material.root_token}")
    print(f"  secret id  : {material.secret_id}")
    print(f"  role id    : {material.role_id}")
    print()
    print(
        "The role id goes in ward's configuration (WARD_ROLE_ID). The unseal key\n"
        "and the secret id are needed after every restart: by hand with\n"
        "`impi ward unlock`, or unattended by pointing WARD_UNSEAL_KEY_FILE and\n"
        "WARD_SECRET_ID_FILE at files holding them — see docs/secrets.md for what\n"
        "that trade costs."
    )
    return 0


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
