"""Manual session cleanup: the ONLY way conversation memory gets deleted.

Deletes both halves in one motion — the SQLite inventory row and pi's on-disk
session files (`{pi_session_dir}/{agent}/*_{runtime_session_id}.*`). Deleting only
one side silently resets or orphans memory, so don't.

Usage:
    python -m crucible.sessions_cli list [--agent X]
    python -m crucible.sessions_cli delete <agent> <conversation_id>
    python -m crucible.sessions_cli purge-idle --days N [--agent X]

The inventory path comes from this library's settings. An app that names its
database differently must say so with --db (or expose its own entry point, the
way the application's own CLI does) — otherwise this opens a file the engine
never writes and reports an empty stand.
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crucible.config import Settings, load_settings
from crucible.store.base import SessionRecord
from crucible.store.sessions import SqliteSessionStore


def _session_files(settings: Settings, record: SessionRecord) -> list[Path]:
    agent_dir = settings.resolved_pi_session_dir / record.agent
    return sorted(agent_dir.glob(f"*_{record.runtime_session_id}.*"))


def _delete_record(settings: Settings, store: SqliteSessionStore, record: SessionRecord) -> None:
    store.delete_sync(record.agent, record.conversation_id)
    removed = 0
    for path in _session_files(settings, record):
        path.unlink(missing_ok=True)
        removed += 1
    print(f"deleted {record.agent}/{record.conversation_id} (pi files removed: {removed})")


def cmd_list(settings: Settings, store: SqliteSessionStore, args: argparse.Namespace) -> None:
    records = store.list_sync(args.agent)
    if not records:
        print("no sessions")
        return
    for r in records:
        files = len(_session_files(settings, r))
        print(
            f"{r.agent:<16} {r.kind:<8} {r.conversation_id:<28} "
            f"last_active={r.last_active} pi_files={files}"
        )


def cmd_delete(settings: Settings, store: SqliteSessionStore, args: argparse.Namespace) -> None:
    record = store.delete_sync(args.agent, args.conversation_id)
    if record is None:
        print(f"not found: {args.agent}/{args.conversation_id}")
        return
    # delete_sync already removed the row; reuse the file cleanup.
    removed = 0
    for path in _session_files(settings, record):
        path.unlink(missing_ok=True)
        removed += 1
    print(f"deleted {record.agent}/{record.conversation_id} (pi files removed: {removed})")


def cmd_purge_idle(settings: Settings, store: SqliteSessionStore, args: argparse.Namespace) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    victims = [
        r
        for r in store.list_sync(args.agent)
        if datetime.fromisoformat(r.last_active) < cutoff
    ]
    if not victims:
        print(f"nothing idle for {args.days}+ days")
        return
    for record in victims:
        _delete_record(settings, store, record)
    print(f"purged {len(victims)} session(s)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="crucible.sessions_cli", description=__doc__)
    parser.add_argument(
        "--db", default="", help="inventory path (default: this library's DB_PATH)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list known sessions")
    p_list.add_argument("--agent", default=None)
    p_list.set_defaults(func=cmd_list)

    p_del = sub.add_parser("delete", help="delete one session (DB row + pi files)")
    p_del.add_argument("agent")
    p_del.add_argument("conversation_id")
    p_del.set_defaults(func=cmd_delete)

    p_purge = sub.add_parser("purge-idle", help="delete sessions idle for N+ days")
    p_purge.add_argument("--days", type=int, required=True)
    p_purge.add_argument("--agent", default=None)
    p_purge.set_defaults(func=cmd_purge_idle)

    args = parser.parse_args()
    settings = load_settings()
    store = SqliteSessionStore(Path(args.db) if args.db else settings.resolved_db_path)
    try:
        args.func(settings, store, args)
    finally:
        store.close_sync()


if __name__ == "__main__":
    main()
