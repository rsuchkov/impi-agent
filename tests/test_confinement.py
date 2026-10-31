"""crucible.confinement: the launcher's rules, the command it is given, and
— on a kernel that has it — Landlock actually holding."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from crucible.confinement import (
    LAUNCHER_PATH,
    ProcessScope,
    confined_command,
    landlock_abi,
    launcher,
)

ABI = landlock_abi()
needs_landlock = pytest.mark.skipif(ABI == 0, reason="this kernel has no Landlock")


def test_the_command_is_prefixed_by_the_launcher_and_its_rules(tmp_path: Path) -> None:
    scope = ProcessScope(
        readable=(Path("/usr"),), writable=(tmp_path,),
        writable_files=(tmp_path / "auth.json",), mkdir_only=(tmp_path / "agent",),
    )
    command = confined_command(scope, ["pi", "--mode", "rpc"], python="/py")
    assert command == [
        "/py", "-I", "-S", str(LAUNCHER_PATH),
        "--readable", "/usr", "--writable", str(tmp_path),
        "--writable-file", str(tmp_path / "auth.json"), "--mkdir-only", str(tmp_path / "agent"),
        "--", "pi", "--mode", "rpc",
    ]


def test_the_launcher_reads_its_rules_and_the_command() -> None:
    rules, command = launcher.parse_command_line(["--readable", "/usr", "--writable", "/tmp/x", "--", "sh", "-c", "true"])
    assert rules == [("--readable", "/usr"), ("--writable", "/tmp/x")]
    assert command == ["sh", "-c", "true"]


def test_a_urls_port_is_the_one_it_names_else_its_schemes() -> None:
    from crucible.confinement import url_port

    assert url_port("http://127.0.0.1:8422") == 8422
    assert url_port("https://ward:8425/x") == 8425
    assert url_port("https://api.example.com/v1") == 443 and url_port("http://tools.internal") == 80
    assert url_port("") is None and url_port("tools.internal:8422") is None and url_port("ftp://x") is None
    assert url_port("http://x:notaport") is None and url_port("/srv/http://x") is None and url_port("/usr/bin") is None


def test_ports_are_rules_of_their_own_beside_the_paths() -> None:
    scope = ProcessScope(readable=(Path("/usr"),), tcp_ports=(443, 8422))
    command = confined_command(scope, ["pi"], python="/py")
    assert command[-6:] == ["--connect-tcp", "443", "--connect-tcp", "8422", "--", "pi"]
    rules, _ = launcher.parse_command_line(command[4:])
    assert launcher.tcp_ports(rules) == [443, 8422]
    assert launcher.tcp_ports([("--readable", "/usr")]) == []  # no port named: the network is not touched


@pytest.mark.parametrize("argv, message", [
    (["--readable", "/usr"], "no command"),
    (["--readable", "/usr", "--"], "no command after"),
    (["--readable", "--", "sh"], "takes a value"),
    (["--everything", "/", "--", "sh"], "unknown option"),
    (["--connect-tcp", "https", "--", "sh"], "takes a port"),
    (["--connect-tcp", "0", "--", "sh"], "takes a port"),
    (["--connect-tcp", "65536", "--", "sh"], "takes a port"),
])
def test_the_launcher_refuses_what_it_cannot_read(argv: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        launcher.parse_command_line(argv)


def test_a_file_rule_carries_file_rights_only() -> None:
    """A writable directory may create files; a writable FILE may only be
    rewritten — and the mkdir-only rule never lets a file be created."""
    make_reg = 1 << 8
    make_dir = 1 << 7
    assert launcher.granted_access("--writable", 7, is_dir=True) & make_reg
    assert not launcher.granted_access("--writable", 7, is_dir=False) & make_reg
    assert not launcher.granted_access("--writable-file", 7, is_dir=False) & make_reg
    mkdir_only = launcher.granted_access("--mkdir-only", 7, is_dir=True)
    assert mkdir_only & make_dir and not mkdir_only & make_reg


def test_every_right_of_the_abi_is_handled_so_what_is_not_granted_is_denied() -> None:
    assert launcher.handled_access(1) == (1 << 13) - 1
    assert launcher.handled_access(7) == (1 << 16) - 1


def _run(scope: ProcessScope, *command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(confined_command(scope, list(command)), capture_output=True, text=True, timeout=30)


def _base(*extra: Path) -> tuple[Path, ...]:
    return (Path("/usr"), Path("/lib"), Path("/lib64"), Path("/bin"), Path("/etc"), Path("/proc"),
            Path(sys.prefix), Path(sys.base_prefix), *extra)


@needs_landlock
def test_what_is_not_named_is_out_of_reach(tmp_path: Path) -> None:
    own, other = tmp_path / "own", tmp_path / "other"
    own.mkdir()
    other.mkdir()
    (other / "secret").write_text("theirs")
    scope = ProcessScope(readable=_base(), writable=(own, Path("/dev")))
    denied = _run(scope, "cat", str(other / "secret"))
    assert denied.returncode != 0 and "Permission denied" in denied.stderr
    allowed = _run(scope, "sh", "-c", f"echo mine > {own}/f && cat {own}/f")
    assert allowed.returncode == 0 and allowed.stdout.strip() == "mine"


@needs_landlock
def test_a_child_of_the_command_is_bound_too(tmp_path: Path) -> None:
    (tmp_path / "secret").write_text("x")
    scope = ProcessScope(readable=_base(), writable=(Path("/dev"),))
    result = _run(scope, "sh", "-c", f"sh -c 'cat {tmp_path}/secret'")
    assert result.returncode != 0 and "Permission denied" in result.stderr


@needs_landlock
def test_another_processs_environment_is_out_of_reach(tmp_path: Path) -> None:
    """The gap a secret in the environment left open: a neighbour reading
    /proc/<pid>/environ. Outside the domain, the kernel refuses."""
    neighbour = subprocess.Popen(["sleep", "30"], env={**os.environ, "NEIGHBOUR_SECRET": "s3cret"})
    try:
        scope = ProcessScope(readable=_base(), writable=(Path("/dev"),))
        result = _run(scope, "cat", f"/proc/{neighbour.pid}/environ")
        assert result.returncode != 0 and "s3cret" not in result.stdout
        own = _run(scope, "sh", "-c", "cat /proc/self/environ > /dev/null")
        assert own.returncode == 0
    finally:
        neighbour.kill()


@needs_landlock
def test_a_mkdir_only_directory_takes_a_lock_and_no_file(tmp_path: Path) -> None:
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "auth.json").write_text("{}")
    scope = ProcessScope(
        readable=_base(), writable=(Path("/dev"),),
        writable_files=(agent / "auth.json",), mkdir_only=(agent,),
    )
    assert _run(scope, "sh", "-c", f"mkdir {agent}/auth.json.lock && rmdir {agent}/auth.json.lock").returncode == 0
    assert _run(scope, "sh", "-c", f"echo '{{\"t\":1}}' > {agent}/auth.json").returncode == 0
    planted = _run(scope, "sh", "-c", f"echo x > {agent}/settings.json")
    assert planted.returncode != 0 and not (agent / "settings.json").exists()


@needs_landlock
def test_a_path_that_does_not_exist_is_skipped_not_fatal(tmp_path: Path) -> None:
    scope = ProcessScope(readable=_base(tmp_path / "missing"), writable=(Path("/dev"),))
    assert _run(scope, "true").returncode == 0


@pytest.mark.skipif(ABI < launcher.NET_ABI, reason="network rules arrived in Landlock ABI 4")
def test_the_command_connects_to_the_ports_named_and_to_no_other(tmp_path: Path) -> None:
    """Two servers on this host; the confined command reaches the one whose
    port the scope names and is refused the other — and refused a port of
    its own to listen on. Without a port named, both are reached as before."""
    allowed, other = socket.socket(), socket.socket()
    allowed.bind(("127.0.0.1", 0))
    other.bind(("127.0.0.1", 0))
    allowed.listen()
    other.listen()
    try:
        allowed_port, other_port = allowed.getsockname()[1], other.getsockname()[1]
        connect = "import socket; socket.create_connection(('127.0.0.1', {}), timeout=5).close(); print('connected')"
        scope = ProcessScope(readable=_base(), writable=(Path("/dev"),), tcp_ports=(allowed_port,))
        reached = _run(scope, sys.executable, "-I", "-S", "-c", connect.format(allowed_port))
        assert reached.returncode == 0 and reached.stdout.strip() == "connected"
        refused = _run(scope, sys.executable, "-I", "-S", "-c", connect.format(other_port))
        assert refused.returncode != 0 and "PermissionError" in refused.stderr
        listen = "import socket; s = socket.socket(); s.bind(('127.0.0.1', 0)); print('bound')"
        bound = _run(scope, sys.executable, "-I", "-S", "-c", listen)
        assert bound.returncode != 0 and "PermissionError" in bound.stderr
        # No port named: the network is not restricted at all.
        open_scope = ProcessScope(readable=_base(), writable=(Path("/dev"),))
        assert _run(open_scope, sys.executable, "-I", "-S", "-c", connect.format(other_port)).returncode == 0
        assert _run(open_scope, sys.executable, "-I", "-S", "-c", listen).returncode == 0
    finally:
        allowed.close()
        other.close()


def test_before_network_rules_the_ports_are_ignored_and_said_so(monkeypatch, capsys) -> None:
    confined: list[list[tuple[str, str]]] = []
    monkeypatch.setattr(launcher, "landlock_abi", lambda: 3)
    monkeypatch.setattr(launcher, "confine_self", lambda rules, abi: confined.append(list(rules)))
    monkeypatch.setattr(launcher.os, "execvp", lambda file, args: None)
    launcher.main(["--readable", "/usr", "--connect-tcp", "443", "--", "true"])
    assert confined == [[("--readable", "/usr"), ("--connect-tcp", "443")]]
    assert launcher.NET_UNAVAILABLE_NOTICE in capsys.readouterr().err
    launcher.main(["--readable", "/usr", "--", "true"])
    assert launcher.NET_UNAVAILABLE_NOTICE not in capsys.readouterr().err  # nothing asked, nothing to say


def test_without_landlock_the_command_runs_and_says_so(monkeypatch, capsys) -> None:
    executed: list[list[str]] = []
    monkeypatch.setattr(launcher, "landlock_abi", lambda: 0)
    monkeypatch.setattr(launcher.os, "execvp", lambda file, args: executed.append(args))
    launcher.main(["--readable", "/usr", "--", "true"])
    assert executed == [["true"]]
    assert launcher.UNAVAILABLE_NOTICE in capsys.readouterr().err


@pytest.mark.skipif(ABI < 6, reason="signal scoping arrived in Landlock ABI 6")
def test_a_process_outside_the_domain_cannot_be_signalled() -> None:
    """From ABI 6: the confined command cannot kill the engine, or a process
    of another conversation."""
    outside = subprocess.Popen(["sleep", "30"])
    try:
        scope = ProcessScope(readable=_base(), writable=(Path("/dev"),))
        result = _run(scope, "kill", "-0", str(outside.pid))
        assert result.returncode != 0
        assert outside.poll() is None
    finally:
        outside.kill()


def test_the_launcher_runs_on_the_standard_library_alone(tmp_path: Path) -> None:
    """Started the way the engine and an agent's image start it — isolated,
    without site packages — it confines (where the kernel can) and runs the
    command, on any kernel."""
    out = tmp_path / "ran"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(LAUNCHER_PATH), "--readable", "/", "--writable", str(tmp_path),
         "--", sys.executable, "-I", "-S", "-c", f"open({str(out)!r}, 'w').write('yes')"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert out.read_text() == "yes"
