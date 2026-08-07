import base64
import json

import pytest

from crucible.ports.agent.runtime import PromptImage
from crucible.runtimes.pi import protocol
from crucible.runtimes.pi.errors import PiProtocolError


def test_encode_prompt_round_trip() -> None:
    line = protocol.encode_prompt("hello", command_id="req-1")
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj == {"id": "req-1", "type": "prompt", "message": "hello"}


def test_encode_follow_up_and_abort() -> None:
    assert json.loads(protocol.encode_follow_up("again", command_id="a"))["type"] == "follow_up"
    abort = json.loads(protocol.encode_abort(command_id="c"))
    assert abort == {"id": "c", "type": "abort"}


def test_extension_ui_response_variants() -> None:
    assert json.loads(protocol.encode_extension_ui_response("u1", value="Allow")) == {
        "type": "extension_ui_response",
        "id": "u1",
        "value": "Allow",
    }
    assert json.loads(protocol.encode_extension_ui_response("u2", confirmed=True))["confirmed"] is True
    assert json.loads(protocol.encode_extension_ui_response("u3", cancelled=True))["cancelled"] is True


def test_new_command_id_is_unique() -> None:
    assert protocol.new_command_id() != protocol.new_command_id()


def test_parse_response_success() -> None:
    event = protocol.parse_line('{"id": "req-1", "type": "response", "command": "prompt", "success": true}')
    assert event.is_response
    assert event.id == "req-1"
    assert event.success is True
    assert event.error is None


def test_parse_response_failure_carries_error() -> None:
    event = protocol.parse_line('{"type": "response", "command": "set_model", "success": false, "error": "boom"}')
    assert event.is_response
    assert event.success is False
    assert event.error == "boom"


def test_parse_strips_trailing_crlf() -> None:
    event = protocol.parse_line('{"type": "agent_start"}\r\n')
    assert event.type == "agent_start"


def test_completed_text_from_text_end() -> None:
    # Current pi: text_start / text_delta / text_end (full text in text_end).
    end = protocol.parse_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_end","content":"Hi there"}}'
    )
    assert protocol.completed_text(end) == "Hi there"

    delta = protocol.parse_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"Hi"}}'
    )
    # Streaming deltas are ignored; only completed text is aggregated.
    assert protocol.completed_text(delta) is None

    start = protocol.parse_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_start"}}'
    )
    assert protocol.completed_text(start) is None


def test_completed_text_accepts_legacy_text_done() -> None:
    done = protocol.parse_line(
        '{"type":"message_update","assistantMessageEvent":{"type":"text_done","content":"Legacy"}}'
    )
    assert protocol.completed_text(done) == "Legacy"


def test_tool_name_extraction() -> None:
    for line in (
        '{"type":"tool_execution_start","toolName":"list_agents"}',
        '{"type":"tool_execution_end","name":"list_agents"}',
        '{"type":"tool_execution_end","tool":{"name":"list_agents"}}',
    ):
        assert protocol.tool_name(protocol.parse_line(line)) == "list_agents"


def test_parse_malformed_json_raises() -> None:
    with pytest.raises(PiProtocolError):
        protocol.parse_line("{not json}")


def test_parse_non_object_raises() -> None:
    with pytest.raises(PiProtocolError):
        protocol.parse_line("[1, 2, 3]")


def test_parse_missing_type_raises() -> None:
    with pytest.raises(PiProtocolError):
        protocol.parse_line('{"foo": "bar"}')


def test_parse_empty_line_raises() -> None:
    with pytest.raises(PiProtocolError):
        protocol.parse_line("   \n")


def test_parse_rejects_unicode_line_separator() -> None:
    with pytest.raises(PiProtocolError):
        protocol.parse_line('{"type": "agent_start"}' + "\u2028")


def test_prompt_carries_images_as_base64_content_blocks() -> None:
    line = protocol.encode_prompt(
        "what is this?",
        command_id="req-1",
        images=[PromptImage(data=b"PNGDATA", mime="image/png")],
    )

    assert json.loads(line) == {
        "id": "req-1",
        "type": "prompt",
        "message": "what is this?",
        "images": [
            {
                "type": "image",
                "data": base64.b64encode(b"PNGDATA").decode(),
                "mimeType": "image/png",
            }
        ],
    }


def test_a_turn_without_images_carries_no_images_field() -> None:
    assert "images" not in json.loads(protocol.encode_prompt("hi", command_id="r"))
    assert "images" not in json.loads(protocol.encode_follow_up("hi", command_id="r"))
