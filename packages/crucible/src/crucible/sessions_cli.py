"""Manual session cleanup: the ONLY way conversation memory gets deleted.

Deletes both halves in one motion — the inventory row and the runtime's on-disk
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
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crucible.config import Settings, load_settings
from crucible.store import open_store
from crucible.store.base import SessionRecord, Store


def _session_files(settings: Settings, record: SessionRecord) -> list[Path]:
    agent_dir = settings.resolved_pi_session_dir / record.agent
    return sorted(agent_dir.glob(f"*_{record.runtime_session_id}.*"))


async def _delete_record(settings: Settings, store: Store, record: SessionRecord) -> None:
    await store.delete(record.agent, record.conversation_id)
    removed = 0
    for path in _session_files(settings, record):
        path.unlink(missing_ok=True)
        removed += 1
    print(f"deleted {record.agent}/{record.conversation_id} (pi files removed: {removed})")


async def cmd_list(settings: Settings, store: Store, args: argparse.Namespace) -> None:
    records = await store.list(args.agent)
    if not records:
        print("no sessions")
        return
    for r in records:
        files = len(_session_files(settings, r))
        print(
            f"{r.agent:<16} {r.kind:<8} {r.conversation_id:<28} "
            f"last_active={r.last_active} pi_files={files}"
        )


async def cmd_delete(settings: Settings, store: Store, args: argparse.Namespace) -> None:
    record = await store.delete(args.agent, args.conversation_id)
    if record is None:
        print(f"not found: {args.agent}/{args.conversation_id}")
        return
    # delete() already removed the row; reuse the file cleanup.
    removed = 0
    for path in _session_files(settings, record):
        path.unlink(missing_ok=True)
        removed += 1
    print(f"deleted {record.agent}/{record.conversation_id} (pi files removed: {removed})")


async def cmd_purge_idle(settings: Settings, store: Store, args: argparse.Namespace) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    victims = [
        r
        for r in await store.list(args.agent)
        if datetime.fromisoformat(r.last_active) < cutoff
    ]
    if not victims:
        print(f"nothing idle for {args.days}+ days")
        return
    for record in victims:
        await _delete_record(settings, store, record)
    print(f"purged {len(victims)} session(s)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="crucible.sessions_cli", description=__doc__)
    parser.add_argument(
        "--db", default="",
        help="which inventory to open — a file path on sqlite, a database name "
             "on mongo (default: DB_NAME, or this library's own)",
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
    asyncio.run(_run(args))


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    # --db overrides the name, whatever the backend reads it as: a file path on
    # SQLite, a database name on MongoDB. Where the server is stays a setting —
    # a URL carries credentials and does not belong on a command line.
    store: Store = open_store(
        settings.store_backend,
        name=args.db or settings.resolved_db_name,
        url=settings.db_url,
    )
    try:
        await args.func(settings, store, args)
    finally:
        await store.close()


if __name__ == "__main__":
    main()
