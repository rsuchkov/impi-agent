"""Manual session cleanup: the ONLY way conversation memory gets deleted.

Deletes both halves in one motion — the inventory row and the runtime's on-disk
session files (`{pi_session_dir}/{agent}/{runtime_session_id}/`, as
``runtimes.pi.spawn.conversation_memory_dir`` spells it). Deleting only one side silently
resets or orphans memory, so don't.

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
from crucible.runtimes.pi.spawn import forget_conversation_memory, session_files
from crucible.store import open_store
from crucible.store.base import SessionRecord, SessionStore, Store


def _session_files(settings: Settings, record: SessionRecord) -> list[Path]:
    return session_files(settings.resolved_pi_session_dir, record.agent, record.runtime_session_id)


def _forget_memory(settings: Settings, record: SessionRecord) -> int:
    return forget_conversation_memory(
        settings.resolved_pi_session_dir, record.agent, record.runtime_session_id
    )


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
    # delete() already removed the row; the memory goes with it.
    removed = _forget_memory(settings, record)
    print(f"deleted {record.agent}/{record.conversation_id} (pi files removed: {removed})")


async def purge_idle(
    settings: Settings, store: SessionStore, *, days: int, agent: str | None = None
) -> list[SessionRecord]:
    """Forget every conversation idle for ``days`` or more: the record and the
    runtime's files for it. Returns what was forgotten. The one place this is
    done — the command below and an engine that purges at startup both call it."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    victims = [r for r in await store.list(agent) if _idle_before(r, cutoff)]
    for record in victims:
        await store.delete(record.agent, record.conversation_id)
        _forget_memory(settings, record)
    return victims


def _idle_before(record: SessionRecord, cutoff: datetime) -> bool:
    """Whether ``record`` was last active before ``cutoff``. A value that does
    not parse is kept, not forgotten — and never stops the sweep; a naive one
    is read as UTC, which is what the engine writes."""
    value: object = record.last_active  # a string; a Mongo row may carry a datetime
    try:
        moment: datetime = (
            value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        )
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = datetime.combine(moment.date(), moment.time(), tzinfo=timezone.utc)
    return moment < cutoff


async def cmd_purge_idle(settings: Settings, store: Store, args: argparse.Namespace) -> None:
    victims = await purge_idle(settings, store, days=args.days, agent=args.agent)
    if not victims:
        print(f"nothing idle for {args.days}+ days")
        return
    for record in victims:
        print(f"deleted {record.agent}/{record.conversation_id}")
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
        prefix=settings.db_collection_prefix,
    )
    try:
        await args.func(settings, store, args)
    finally:
        await store.close()


if __name__ == "__main__":
    main()
