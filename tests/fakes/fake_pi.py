"""A minimal stand-in for `pi --mode rpc`, used by the subprocess-level test.

Reads JSONL commands from stdin and, for each prompt/follow_up, emits the event
sequence current pi produces: agent_start -> text_start/text_delta/text_end
(echoing the message) -> agent_end. Exits on EOF.
"""

import json
import sys


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctype = command.get("type")
        if ctype in ("prompt", "follow_up"):
            text = f"echo: {command.get('message', '')}"
            emit({"type": "agent_start"})
            emit({"type": "message_update", "assistantMessageEvent": {"type": "text_start"}})
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": text},
                }
            )
            emit(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_end", "content": text},
                }
            )
            emit({"type": "agent_end", "messages": []})
        elif ctype == "abort":
            break


if __name__ == "__main__":
    main()
