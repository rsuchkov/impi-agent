"""An in-memory ``McpTurns`` for the gateway's tests."""

from dataclasses import replace

from crucible.ports.mcp_turns import TURN_ENDED, TURN_FAILED, McpTurnRecord


class MemoryMcpTurns:
    def __init__(self) -> None:
        self.records: dict[str, McpTurnRecord] = {}

    async def save_mcp_turn(self, record: McpTurnRecord) -> None:
        known = self.records.get(record.turn_id)
        # A save leaves the cancel mark as it is, like the backends do.
        self.records[record.turn_id] = record if known is None else replace(record, cancel_requested=known.cancel_requested)

    async def get_mcp_turn(self, turn_id: str) -> McpTurnRecord | None:
        return self.records.get(turn_id)

    async def latest_mcp_turn(self, conversation: str) -> McpTurnRecord | None:
        mine = [r for r in self.records.values() if r.conversation == conversation]
        return max(mine, key=lambda r: r.started_at) if mine else None

    async def find_mcp_turn(self, subject: str, client: str, request_digest: str) -> McpTurnRecord | None:
        mine = [
            r for r in self.records.values()
            if r.subject == subject and r.client == client and r.request_digest == request_digest and r.state not in TURN_ENDED
        ]
        return max(mine, key=lambda r: r.started_at) if mine and request_digest else None

    async def request_mcp_cancel(self, turn_id: str) -> bool:
        record = self.records.get(turn_id)
        if record is None:
            return False
        self.records[turn_id] = replace(record, cancel_requested=True)
        return True

    async def fail_mcp_turns(self, replica: str, reason: str, now: str, expires_at: str) -> int:
        count = 0
        for turn_id, record in list(self.records.items()):
            if record.replica == replica and record.state not in TURN_ENDED:
                self.records[turn_id] = replace(
                    record, state=TURN_FAILED, step="", answer=reason, is_error=True, updated_at=now, expires_at=expires_at,
                )
                count += 1
        return count

    async def purge_mcp_turns(self, now: str) -> int:
        stale = [turn_id for turn_id, record in self.records.items() if record.expires_at <= now]
        for turn_id in stale:
            del self.records[turn_id]
        return len(stale)
