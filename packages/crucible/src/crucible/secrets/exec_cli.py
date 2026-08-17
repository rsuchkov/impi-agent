"""``secret-exec``: run a command with a secret in its environment.

    secret-exec --env GITHUB_TOKEN=vault://github-token \\
                --reason "push the release" -- gh release create v1.2.0

This is the whole of an agent's access to credentials. There is no verb that
prints a value and no verb that lists what exists: the agent either knows the
reference it needs — from its own instructions — or it does not, and it cannot
learn one by asking. What it gets back is an exit code and whatever the command
it asked for wrote.

The value never passes through the agent's context. This process asks the
engine, receives the value over loopback, and replaces itself with the requested
command via ``execvpe``, so the secret exists only in the environment of a
process the model never reads. It is still the human's job to look at the
command on the approval card: a caller may legally ask to run ``echo $TOKEN``.
"""

import json
import os
import sys
import urllib.error
import urllib.request

from crucible.secrets.ports import WIRE_UNAVAILABLE, parse_ref

# One message for every authorization outcome. No secret, not permitted, refused
# by a human, nobody answered — all the same sentence, because a caller that
# could tell them apart could map the store by trying names.
_REFUSED = "secret-exec: access was not granted"
_UNAVAILABLE = "secret-exec: the secret store is not available right now"

# sysexits.h, so a shell (and the model reading the transcript) can tell the
# three failures apart without a message to parse.
EXIT_USAGE = 64  # the command line was wrong
EXIT_UNAVAILABLE = 75  # temporary: the store is sealed, locked or unreachable
EXIT_REFUSED = 77  # the request was not granted
EXIT_CONFIG = 78  # this process was not given the engine's address

# Generously above any sane approval timeout: the engine decides when to give
# up waiting for a human, and this must not give up first.
_HTTP_TIMEOUT_S = 600.0

_USAGE = (
    "usage: secret-exec --env NAME=vault://secret [--env ...] "
    "[--reason TEXT] -- command [args...]"
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    try:
        bindings, reason, command = _parse(args)
    except ValueError as exc:
        print(f"secret-exec: {exc}\n{_USAGE}", file=sys.stderr)
        return EXIT_USAGE

    url = os.environ.get("TOOL_URL", "").rstrip("/")
    token = os.environ.get("TOOL_TOKEN", "")
    if not url or not token:
        print(
            "secret-exec: no engine to ask (TOOL_URL/TOOL_TOKEN are unset)", file=sys.stderr
        )
        return EXIT_CONFIG

    payload = {
        "bindings": [{"env": name, "ref": ref} for name, ref in bindings],
        "reason": reason,
        "command": command,
    }
    request = urllib.request.Request(
        f"{url}/secrets/lease",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Tool-Token": token,
            "X-Runtime-Session": os.environ.get("RUNTIME_SESSION_ID", ""),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            answer = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        # A 4xx here is this process's own fault (a malformed request, an unknown
        # token) — worth saying, and it reveals nothing about what is stored.
        detail = _detail(exc)
        print(f"secret-exec: the engine refused the request ({detail})", file=sys.stderr)
        return EXIT_USAGE if exc.code == 400 else EXIT_CONFIG
    except (urllib.error.URLError, TimeoutError, OSError):
        print(_UNAVAILABLE, file=sys.stderr)
        return EXIT_UNAVAILABLE

    if not isinstance(answer, dict) or not answer.get("granted"):
        status = answer.get("status") if isinstance(answer, dict) else ""
        if status == WIRE_UNAVAILABLE:
            print(_UNAVAILABLE, file=sys.stderr)
            return EXIT_UNAVAILABLE
        print(_REFUSED, file=sys.stderr)
        return EXIT_REFUSED

    values = answer.get("values") or {}
    if not isinstance(values, dict):
        print(_REFUSED, file=sys.stderr)
        return EXIT_REFUSED

    env = {**os.environ, **{str(k): str(v) for k, v in values.items()}}
    # Deliberately not inherited by anything else: exec replaces this process, so
    # the value lives in the command's environment and nowhere upstream of it.
    try:
        os.execvpe(command[0], list(command), env)
    except OSError as exc:
        print(f"secret-exec: cannot run {command[0]}: {exc.strerror}", file=sys.stderr)
        return EXIT_USAGE
    return 0  # unreachable: execvpe either replaces this process or raises


def _parse(args: list[str]) -> tuple[list[tuple[str, str]], str, list[str]]:
    """Split ``--env``/``--reason`` from the command after ``--``.

    Hand-rolled rather than argparse: everything after the separator belongs to
    the command verbatim, including its own flags, and argparse's ways of
    expressing that all leak one option or another into the wrong side.
    """
    if "--" not in args:
        raise ValueError("the command must follow a `--` separator")
    split = args.index("--")
    options, command = args[:split], args[split + 1 :]
    if not command:
        raise ValueError("no command to run after `--`")

    bindings: list[tuple[str, str]] = []
    seen: set[str] = set()
    reason = ""
    index = 0
    while index < len(options):
        option = options[index].split("=", 1)[0]
        if option not in ("--env", "-e", "--reason"):
            raise ValueError(f"unknown option {options[index]!r}")
        value, index = _value_for(options[index], options, index)
        if option in ("--env", "-e"):
            name, sep, ref = value.partition("=")
            if not sep:
                raise ValueError(f"--env wants NAME=reference, got {value!r}")
            if not name.isidentifier():
                raise ValueError(f"not an environment variable name: {name!r}")
            if name in seen:
                raise ValueError(f"--env {name} given twice")
            seen.add(name)
            parse_ref(ref)  # fail here rather than after a round trip
            bindings.append((name, ref))
        else:
            reason = value
    if not bindings:
        raise ValueError("nothing to fetch — pass at least one --env")
    return bindings, reason, command


def _value_for(option: str, options: list[str], index: int) -> tuple[str, int]:
    """``--env=X`` and ``--env X`` both work; returns the value and where to
    resume."""
    if "=" in option and option.split("=", 1)[0] in ("--env", "-e", "--reason"):
        return option.split("=", 1)[1], index + 1
    if index + 1 >= len(options):
        raise ValueError(f"{option} needs a value")
    return options[index + 1], index + 2


def _detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = json.loads(exc.read() or b"{}")
        return str(body.get("error") or f"http {exc.code}")
    except Exception:
        return f"http {exc.code}"


if __name__ == "__main__":  # pragma: no cover - console-script entry point
    raise SystemExit(main())
