from __future__ import annotations

import json

from app.providers.anthropic_protocol import (
    anthropic_to_responses,
    convert_sse_to_anthropic,
    finalize_anthropic_message,
)
from app.providers.codex_protocol import parse_sse_event


def test_anthropic_to_responses_keeps_tool_use_id_and_system():
    payload = anthropic_to_responses(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 128,
            "system": "You are a coding agent.",
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "calling"},
                        {"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
                },
            ],
            "tools": [
                {
                    "name": "bash",
                    "description": "run a command",
                    "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
        },
        default_model="claude-sonnet-4-6",
    )
    assert payload["model"] == "claude-sonnet-4-6"
    assert payload["instructions"] == "You are a coding agent."
    assert payload["max_output_tokens"] == 128
    assert payload["tools"][0]["name"] == "bash"
    assert payload["tools"][0]["parameters"]["properties"]["cmd"]["type"] == "string"
    kinds = [item["type"] for item in payload["input"]]
    assert kinds == ["message", "message", "function_call", "function_call_output"]
    call = payload["input"][2]
    assert call["call_id"] == "toolu_1"
    assert call["name"] == "bash"
    assert json.loads(call["arguments"]) == {"cmd": "ls"}
    assert payload["input"][3]["call_id"] == "toolu_1"
    assert payload["input"][3]["output"] == "ok"


def test_anthropic_to_responses_accepts_system_role_in_messages():
    payload = anthropic_to_responses(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 32,
            "system": "You are a coding agent.",
            "messages": [
                {"role": "system", "content": [{"type": "text", "text": "Use the workspace tools."}]},
                {"role": "user", "content": "hi"},
            ],
        },
        default_model="claude-sonnet-4-6",
    )
    assert payload["instructions"] == "You are a coding agent.\n\nUse the workspace tools."
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_anthropic_sse_emits_text_then_tool_use():
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": "resp_1", "model": "claude-sonnet-4-6"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"}),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "toolu_9", "name": "read"},
            },
        ),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "delta": "{\"p\""}),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "delta": ":1}"}),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 4, "output_tokens": 6},
                },
            },
        ),
    ]
    state: dict = {}
    events: list[dict] = []
    for name, payload in blocks:
        raw = f"event: {name}\ndata: {json.dumps(payload)}"
        event, data = parse_sse_event(raw)
        for encoded in convert_sse_to_anthropic(event, data, state):
            text = encoded.decode()
            body = text.split("data: ", 1)[1]
            events.append(json.loads(body))
    types = [item["type"] for item in events]
    assert types[0] == "message_start"
    assert "content_block_start" in types
    assert "content_block_delta" in types
    assert types[-2] == "message_delta"
    assert types[-1] == "message_stop"
    assert events[-2]["delta"]["stop_reason"] == "tool_use"
    result = finalize_anthropic_message(state)
    assert result["content"][0] == {"type": "text", "text": "hi"}
    assert result["content"][1]["type"] == "tool_use"
    assert result["content"][1]["id"] == "toolu_9"
    assert result["content"][1]["input"] == {"p": 1}
    assert result["stop_reason"] == "tool_use"
