from __future__ import annotations

from app.providers.gemini_protocol import (
    convert_sse_to_gemini,
    finalize_gemini_response,
    gemini_to_responses,
)


def test_gemini_to_responses_maps_text_and_tools():
    payload = gemini_to_responses(
        {
            "model": "gemini-3.8-flash",
            "systemInstruction": {"parts": [{"text": "Be brief."}]},
            "contents": [
                {"role": "user", "parts": [{"text": "Hello"}]},
                {
                    "role": "model",
                    "parts": [{"functionCall": {"name": "read_file", "args": {"path": "a.py"}}}],
                },
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": "read_file", "response": {"ok": True}}}],
                },
            ],
            "tools": [{"functionDeclarations": [{"name": "read_file", "parameters": {"type": "object"}}]}],
        },
        default_model="gemini-3.8-flash",
    )
    assert payload["model"] == "gemini-3.8-flash"
    assert payload["instructions"] == "Be brief."
    assert payload["input"][0]["role"] == "user"
    assert payload["input"][1]["type"] == "function_call"
    assert payload["input"][1]["name"] == "read_file"
    assert payload["input"][2]["type"] == "function_call_output"
    assert payload["tools"][0]["name"] == "read_file"


def test_gemini_sse_text_then_complete():
    state = {}
    convert_sse_to_gemini("response.created", '{"type":"response.created","response":{"model":"gemini-3.8-flash"}}', state)
    chunks = convert_sse_to_gemini("response.output_text.delta", '{"type":"response.output_text.delta","delta":"Hi"}', state)
    assert b'"text": "Hi"' in chunks[0]
    convert_sse_to_gemini(
        "response.completed",
        '{"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1}}}',
        state,
    )
    final = finalize_gemini_response(state)
    assert final["candidates"][0]["content"]["parts"][0]["text"] == "Hi"
    assert final["usageMetadata"]["promptTokenCount"] == 3
