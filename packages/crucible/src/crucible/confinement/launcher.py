"""Run a command under Landlock rules, then get out of the way.

    python3 -I -S launcher.py --readable DIR … --writable DIR … \
        --writable-file FILE … --mkdir-only DIR … -- COMMAND [ARGS…]

The process restricts itself, sets no-new-privileges and replaces itself with
COMMAND, which inherits the rules for good: Landlock cannot be lifted, and
every child the command starts is bound by the same rules. What is not named
is out of reach — no read, no write, no listing — and, from ABI 6, the
command can neither signal nor reach the abstract sockets of any process
outside its own domain. Reading another process's ``/proc/<pid>/environ`` or
``mem`` is refused by the kernel for a process outside the domain as well.

Self-contained on purpose: standard library only, no imports from the package
it ships in, runnable by path. An agent's container runs this same file
without the library installed beside it.

A path that does not exist is skipped, not an error: the caller names what a
process may touch, and an absent directory is simply one it will not find.
A kernel without Landlock runs the command unrestricted and says so on stderr;
whoever starts it decides beforehand whether that is acceptable.
"""

import ctypes
import os
import sys
from collections.abc import Sequence

_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446
_PR_SET_NO_NEW_PRIVS = 38
_RULE_PATH_BENEATH = 1
_CREATE_RULESET_VERSION = 1
# Scopes (ABI 6): no signals to, no abstract unix sockets of, processes outside.
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13  # ABI 2
_TRUNCATE = 1 << 14  # ABI 3
_IOCTL_DEV = 1 << 15  # ABI 5

READABLE = "--readable"
WRITABLE = "--writable"
WRITABLE_FILE = "--writable-file"
MKDIR_ONLY = "--mkdir-only"
OPTIONS = (READABLE, WRITABLE, WRITABLE_FILE, MKDIR_ONLY)
UNAVAILABLE_NOTICE = "confinement: Landlock is not available; running unconfined"


class _RulesetAttr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    ]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def landlock_abi() -> int:
    """The Landlock ABI version this kernel offers; 0 when it offers none."""
    try:
        version = _libc().syscall(
            _SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION)
        )
    except (OSError, AttributeError):
        return 0
    return version if version > 0 else 0


def handled_access(abi: int) -> int:
    """Every filesystem right this ABI can restrict — all of them are handled,
    so whatever a rule does not grant is denied."""
    rights = (
        _EXECUTE | _WRITE_FILE | _READ_FILE | _READ_DIR | _REMOVE_DIR | _REMOVE_FILE
        | _MAKE_CHAR | _MAKE_DIR | _MAKE_REG | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM
    )
    if abi >= 2:
        rights |= _REFER
    if abi >= 3:
        rights |= _TRUNCATE
    if abi >= 5:
        rights |= _IOCTL_DEV
    return rights


def granted_access(option: str, abi: int, *, is_dir: bool) -> int:
    """What a rule of this kind grants. A file can only carry file rights, so
    a directory right named for a file is dropped rather than refused."""
    read = _EXECUTE | _READ_FILE | _READ_DIR
    if option == READABLE:
        rights = read
    elif option == WRITABLE:
        rights = handled_access(abi)
    elif option == WRITABLE_FILE:
        rights = _READ_FILE | _WRITE_FILE | (_TRUNCATE if abi >= 3 else 0)
    elif option == MKDIR_ONLY:
        # Subdirectories may be made and removed — a lock directory — but no
        # file created: nothing a runtime would load can be planted here.
        rights = read | _MAKE_DIR | _REMOVE_DIR
    else:
        raise ValueError(f"unknown option {option!r}")
    if not is_dir:
        file_rights = _EXECUTE | _WRITE_FILE | _READ_FILE
        file_rights |= _TRUNCATE if abi >= 3 else 0
        file_rights |= _IOCTL_DEV if abi >= 5 else 0
        rights &= file_rights
    return rights


def parse_command_line(argv: Sequence[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """``[(option, path), …]`` and the command after ``--``."""
    if "--" not in argv:
        raise ValueError("no command: the rules end with `--`, the command follows it")
    split = list(argv).index("--")
    options, command = list(argv[:split]), list(argv[split + 1 :])
    if not command:
        raise ValueError("no command after `--`")
    if len(options) % 2:
        raise ValueError("every option takes a path")
    rules: list[tuple[str, str]] = []
    for index in range(0, len(options), 2):
        option, path = options[index], options[index + 1]
        if option not in OPTIONS:
            raise ValueError(f"unknown option {option!r}")
        rules.append((option, path))
    return rules, command


def confine_self(rules: Sequence[tuple[str, str]], abi: int) -> None:
    """Confine this process to ``rules``. Irreversible."""
    libc = _libc()
    size = 8 if abi < 4 else (16 if abi < 6 else 24)
    scoped = (_SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL) if abi >= 6 else 0
    attr = _RulesetAttr(handled_access(abi), 0, scoped)
    ruleset = libc.syscall(
        _SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.c_size_t(size), ctypes.c_uint32(0)
    )
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset failed")
    try:
        for option, path in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            try:
                allowed = granted_access(option, abi, is_dir=os.path.isdir(path))
                beneath = _PathBeneathAttr(allowed, fd)
                if libc.syscall(
                    _SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
                    ctypes.byref(beneath), ctypes.c_uint32(0),
                ) < 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule failed for {path}")
            finally:
                os.close(fd)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        if libc.syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
    finally:
        os.close(ruleset)


def main(argv: Sequence[str]) -> None:
    rules, command = parse_command_line(argv)
    abi = landlock_abi()
    if abi:
        confine_self(rules, abi)
    else:
        print(UNAVAILABLE_NOTICE, file=sys.stderr)
    os.execvp(command[0], command)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, OSError) as exc:
        print(f"confinement: {exc}", file=sys.stderr)
        sys.exit(126)
