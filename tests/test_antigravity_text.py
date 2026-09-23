from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
from conftest import ADMIN_HEADERS, auth_payload, import_pool

from app.http_client import set_http_transport
from app.providers.antigravity import (
    DEFAULT_HOST,
    MAX_SYSTEM_INSTRUCTION_CHARS,
    USER_AGENT,
    clean_claude_json_schema,
    clean_json_schema,
    clear_thought_signature_cache,
    cloudcode_payloads_to_codex_sse,
    function_declarations,
    lookup_thought_signature,
    parse_quota_snapshot,
    responses_to_cloudcode,
    wire_model,
)
from app.providers.codex_protocol import (
    chat_messages_to_input,
    convert_sse_to_chat_chunks,
    finalize_chat_completion,
    parse_sse_event,
)
from app.providers.registry import get_adapter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
ANTIGRAVITY_OAUTH = FIXTURES / "oauth_account.example.json"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def key_headers(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


def grok_cli_payload(
    user_id: str = "user-grok-1",
    email: str = "grok@example.com",
    token: str = "eyJhbGciOiJub25lIn0.e30.",
) -> dict:
    return {
        "https://auth.x.ai::test-client": {
            "auth_mode": "oidc",
            "key": token,
            "refresh_token": "refresh-test",
            "expires_at": "2099-01-01T00:00:00.000000Z",
            "user_id": user_id,
            "email": email,
            "principal_id": user_id,
            "oidc_client_id": "test-client",
            "oidc_issuer": "https://auth.x.ai",
        }
    }


def text_sse(response_id: str, text: str = "ok") -> bytes:
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "gpt-6-astra"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": text}),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "model": "gpt-6-astra",
                    "output_text": text,
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                },
            },
        ),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def _import_antigravity_key(client) -> str:
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("oauth.json", ANTIGRAVITY_OAUTH.read_bytes(), "application/json")},
        data={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/admin/api/keys",
        json={"name": "antigravity-text", "preferred_account_id": "antigravity:user@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def _enable_antigravity(monkeypatch) -> None:
    monkeypatch.setattr(get_adapter("antigravity"), "ready", True)


def test_translator_maps_responses_text_request_to_cloudcode_envelope():
    responses = _load("responses_text_request.json")
    envelope = responses_to_cloudcode(responses, project="example-cloud-project")
    request = envelope["request"]
    assert envelope["project"] == "example-cloud-project"
    assert envelope["requestType"] == "agent"
    assert envelope["model"] == "gemini-3.8-flash-tiered"
    system_text = request["systemInstruction"]["parts"][0]["text"]
    assert responses["instructions"] in system_text
    assert request["systemInstruction"]["role"] == "user"
    roles = [item["role"] for item in request["contents"]]
    assert set(roles) <= {"user", "model"}
    assert "system" not in roles
    assert request["contents"][0]["parts"][0]["text"] == "Reply with ok only."
    assert "safetySettings" not in request
    assert "safety_settings" not in request
    assert "safetySettings" not in envelope
    dumped = json.dumps(envelope)
    assert "thinkingBudget" not in dumped
    assert "thinking_budget" not in dumped
    assert request["generationConfig"]["thinkingConfig"]["thinkingLevel"] in {"low", "medium", "high"}
    assert "functionDeclarations" not in dumped
    assert "tools" not in request


def test_translator_maps_effort_and_aliases():
    low = responses_to_cloudcode(
        {"model": "gemini-3.7-flash", "reasoning_effort": "low", "input": "ok"},
        project="example-cloud-project",
    )
    assert low["model"] == "gemini-3.7-flash-tiered"
    assert low["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
    high = responses_to_cloudcode(
        {"model": "gemini-3.1-pro", "reasoning": {"effort": "xhigh"}, "input": "ok"},
        project="example-cloud-project",
    )
    assert high["model"] == "gemini-pro-agent"
    assert high["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"
    alias = responses_to_cloudcode(
        {"model": "gemini-3-flash", "input": "ok"},
        project="example-cloud-project",
    )
    assert alias["model"] == "gemini-3.8-flash-tiered"
    merged = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "instructions": "base",
            "input": [
                {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "dev"}]},
                {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "sys"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        },
        project="example-cloud-project",
    )
    system_text = merged["request"]["systemInstruction"]["parts"][0]["text"]
    assert "base" in system_text
    assert "dev" in system_text
    assert "sys" in system_text
    assert [item["role"] for item in merged["request"]["contents"]] == ["user"]


def test_translator_maps_claude_to_thinking_budget_not_level():
    sonnet = responses_to_cloudcode(
        {"model": "claude-sonnet-4-6", "reasoning_effort": "low", "input": "ok"},
        project="example-cloud-project",
    )
    assert sonnet["model"] == "claude-sonnet-4-6"
    think = sonnet["request"]["generationConfig"]["thinkingConfig"]
    assert think == {"thinkingBudget": 1024}
    assert "thinkingLevel" not in think
    opus = responses_to_cloudcode(
        {"model": "claude-opus-4-6", "reasoning": {"effort": "high"}, "input": "ok"},
        project="example-cloud-project",
    )
    assert opus["model"] == "claude-opus-4-6-thinking"
    assert opus["request"]["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}
    assert opus["request"]["generationConfig"]["maxOutputTokens"] == 64000
    assert opus["request"]["generationConfig"]["maxOutputTokens"] > 1024
    gemini = responses_to_cloudcode(
        {"model": "gemini-3.8-flash", "reasoning_effort": "high", "input": "ok"},
        project="example-cloud-project",
    )
    assert "maxOutputTokens" not in gemini["request"]["generationConfig"]
    assert wire_model("claude-sonnet-4-6-thinking") == "claude-sonnet-4-6"


def test_translator_strips_thought_signature_and_inlines_images():
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "prior", "thoughtSignature": "sig-other-model"},
                        {"type": "output_text", "text": "hidden", "thought": True},
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "look"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,QUJD",
                        },
                    ],
                },
            ],
        },
        project="example-cloud-project",
    )
    dumped = json.dumps(envelope)
    assert "thoughtSignature" not in dumped
    assert "sig-other-model" not in dumped
    parts = envelope["request"]["contents"]
    assert parts[0]["role"] == "model"
    assert parts[0]["parts"] == [{"text": "prior"}]
    assert parts[1]["role"] == "user"
    assert parts[1]["parts"][0] == {"text": "look"}
    assert parts[1]["parts"][1] == {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}


def test_function_call_thought_signature_round_trips_and_caches():
    clear_thought_signature_cache()
    upstream = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "shell",
                                    "args": {"command": "pwd"},
                                    "id": "call_sig_1",
                                },
                                "thoughtSignature": "sig-from-gemini",
                            }
                        ],
                    }
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 1,
                "totalTokenCount": 4,
            },
        }
    }
    sse = cloudcode_payloads_to_codex_sse(
        [upstream], model="gemini-3.8-flash", response_id="resp-sig"
    ).decode("utf-8")
    assert "thought_signature" in sse
    assert "sig-from-gemini" in sse
    assert lookup_thought_signature("call_sig_1") == "sig-from-gemini"

    # Client may strip unknown fields; cache still restores signature by call_id.
    clear_thought_signature_cache()
    # Re-seed as if first turn already remembered the signature.
    from app.providers.antigravity import remember_thought_signature

    remember_thought_signature("call_sig_1", "sig-from-gemini")
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_sig_1",
                    "name": "shell",
                    "arguments": "{\"command\":\"pwd\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_sig_1",
                    "output": "ok",
                },
            ],
            "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    model_parts = envelope["request"]["contents"][0]["parts"]
    assert model_parts[0]["functionCall"]["name"] == "shell"
    assert model_parts[0]["thoughtSignature"] == "sig-from-gemini"

    # Explicit field on the item also works without cache.
    clear_thought_signature_cache()
    envelope2 = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_sig_2",
                    "name": "shell",
                    "arguments": "{}",
                    "thought_signature": "sig-on-item",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_sig_2",
                    "output": "ok",
                },
            ],
            "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    assert envelope2["request"]["contents"][0]["parts"][0]["thoughtSignature"] == "sig-on-item"


def test_thought_part_signature_is_copied_onto_following_function_call():
    clear_thought_signature_cache()
    sse = cloudcode_payloads_to_codex_sse(
        [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "text": "plan",
                                        "thought": True,
                                        "thoughtSignature": "sig-from-thought",
                                    },
                                    {
                                        "functionCall": {
                                            "name": "exec_command",
                                            "args": {"cmd": "pwd"},
                                            "id": "call_1120165",
                                        }
                                    },
                                ],
                            }
                        }
                    ]
                }
            }
        ],
        model="gemini-3.8-flash",
        response_id="resp-thought-sig",
    ).decode("utf-8")
    assert "sig-from-thought" in sse
    assert lookup_thought_signature("call_1120165") == "sig-from-thought"
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1120165",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                },
                {"type": "function_call_output", "call_id": "call_1120165", "output": "ok"},
            ],
            "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    assert envelope["request"]["contents"][0]["parts"][0]["thoughtSignature"] == "sig-from-thought"


def test_incremental_thought_then_function_call_keeps_signature():
    clear_thought_signature_cache()
    thought = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": "plan", "thought": True, "thoughtSignature": "sig-inc"}],
                    }
                }
            ]
        }
    }
    call = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "exec_command",
                                    "args": {"cmd": "dir"},
                                    "id": "call_9",
                                }
                            }
                        ],
                    }
                }
            ]
        }
    }
    sse = cloudcode_payloads_to_codex_sse(
        [thought, call], model="gemini-3.8-flash", response_id="resp-inc"
    ).decode("utf-8")
    assert "sig-inc" in sse
    assert lookup_thought_signature("call_9") == "sig-inc"


def test_stream_snapshots_prefer_signed_function_call():
    clear_thought_signature_cache()
    unsigned = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "exec_command",
                                    "args": {"cmd": "pwd"},
                                    "id": "call_1",
                                }
                            }
                        ],
                    }
                }
            ]
        }
    }
    signed = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "exec_command",
                                    "args": {"cmd": "pwd"},
                                    "id": "call_1",
                                },
                                "thoughtSignature": "sig-final",
                            }
                        ],
                    }
                }
            ]
        }
    }
    sse = cloudcode_payloads_to_codex_sse(
        [unsigned, signed], model="gemini-3.8-flash", response_id="resp-merge"
    ).decode("utf-8")
    assert sse.count('"type": "function_call"') == 3
    assert sse.count("sig-final") >= 2
    assert "sig-final" in sse
    assert lookup_thought_signature("call_1") == "sig-final"


def test_function_call_signature_is_cached_under_item_id():
    clear_thought_signature_cache()
    sse = cloudcode_payloads_to_codex_sse(
        [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "exec_command",
                                            "args": {"cmd": "pwd"},
                                            "id": "call_numeric",
                                        },
                                        "thoughtSignature": "sig-item",
                                    }
                                ],
                            }
                        }
                    ]
                }
            }
        ],
        model="gemini-3.8-flash",
        response_id="resp-item-id",
    ).decode("utf-8")
    item_id = None
    for line in sse.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[5:].lstrip())
        item = payload.get("item") if isinstance(payload, dict) else None
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("id"):
            item_id = item["id"]
            break
    assert isinstance(item_id, str) and item_id.startswith("fc_")
    assert lookup_thought_signature("call_numeric") == "sig-item"
    assert lookup_thought_signature(item_id) == "sig-item"
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "function_call",
                    "id": item_id,
                    "call_id": "call_numeric",
                    "name": "exec_command",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_numeric", "output": "ok"},
            ],
            "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    assert envelope["request"]["contents"][0]["parts"][0]["thoughtSignature"] == "sig-item"


def test_stream_unwrap_emits_codex_output_text_delta():
    chunk = _load("cloudcode_stream_text_chunk.json")
    sse = cloudcode_payloads_to_codex_sse([chunk], model="gemini-3.8-flash", response_id="resp-ag-ok")
    text = sse.decode("utf-8")
    assert "event: response.output_text.delta" in text
    assert '"delta": "ok"' in text
    assert "event: response.completed" in text
    thought = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "hidden", "thought": True},
                            {"text": "visible", "thoughtSignature": "sig-from-other-model"},
                        ],
                    }
                }
            ]
        }
    }
    mixed = cloudcode_payloads_to_codex_sse([thought], model="gemini-3.8-flash").decode("utf-8")
    assert '"delta": "visible"' in mixed
    assert "hidden" not in mixed.split("response.output_text.delta", 1)[-1].split("\n\n", 1)[0]
    assert "event: response.reasoning_summary_text.delta" in mixed
    blocked = cloudcode_payloads_to_codex_sse(
        [{"response": {"promptFeedback": {"blockReason": "SAFETY", "blockReasonMessage": "policy text"}}}],
        model="gemini-3.8-flash",
    ).decode("utf-8")
    assert "event: response.refusal.delta" in blocked
    assert "policy text" not in blocked
    assert "SAFETY" not in blocked


def test_gateway_responses_stream_hits_daily_host(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "gemini-3.8-flash", "input": "Reply with ok only.", "stream": True},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "response.output_text.delta" in response.text
    assert "ok" in response.text
    assert captured
    outbound = captured[0]
    assert outbound.url.host == "daily-cloudcode-pa.googleapis.com"
    assert str(outbound.url).startswith(DEFAULT_HOST)
    assert "streamGenerateContent" in outbound.url.path
    assert outbound.headers["User-Agent"] == USER_AGENT
    assert outbound.headers["Authorization"] == "Bearer ya29.example-access-token"
    assert "x-goog-api-client" not in outbound.headers
    assert "client-metadata" not in outbound.headers
    body = json.loads(outbound.content)
    assert body["model"] == "gemini-3.8-flash-tiered"
    assert body["request"]["generationConfig"]["thinkingConfig"]["thinkingLevel"] in {
        "low",
        "medium",
        "high",
    }


def test_chat_completions_uses_responses_translator(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    json_response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [
                {"role": "system", "content": "You are a coding agent."},
                {"role": "user", "content": "Reply with ok only."},
            ],
        },
        headers=key_headers(key),
    )
    assert json_response.status_code == 200, json_response.text
    assert "ok" in json_response.json()["choices"][0]["message"]["content"]
    assert captured
    system_text = captured[0]["request"]["systemInstruction"]["parts"][0]["text"]
    assert "You are a coding agent." in system_text
    assert captured[0]["model"] == "gemini-3.8-flash-tiered"
    streamed = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "Reply with ok only."}],
        },
        headers=key_headers(key),
    )
    assert streamed.status_code == 200, streamed.text
    assert "ok" in streamed.text


def _cloudcode_function_call_chunk(
    *, name: str = "lookup", call_id: str = "call_sig_chat", signature: str = "sig-from-gemini"
) -> dict:
    return {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": name,
                                    "args": {"q": "next"},
                                    "id": call_id,
                                },
                                "thoughtSignature": signature,
                            }
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 8,
                "candidatesTokenCount": 2,
                "totalTokenCount": 10,
            },
        }
    }


def test_chat_messages_to_input_keeps_thought_signature_images_and_function_role():
    _instructions, items = chat_messages_to_input(
        [
            {"role": "user", "content": "look"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_sig_chat",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{\"q\":\"old\"}"},
                        "extra_content": {"google": {"thought_signature": "sig-from-gemini"}},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_sig_chat",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                ],
            },
            {
                "role": "function",
                "name": "lookup",
                "content": "legacy result",
            },
        ]
    )
    kinds = [item["type"] for item in items]
    assert kinds == ["message", "function_call", "function_call_output", "function_call_output"]
    assert items[1]["thought_signature"] == "sig-from-gemini"
    assert items[2]["output"] == [
        {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}
    ]
    assert items[3]["call_id"] == "lookup"
    assert items[3]["output"] == "legacy result"


def test_chat_sse_emits_google_thought_signature_on_tool_calls():
    sse = cloudcode_payloads_to_codex_sse(
        [_cloudcode_function_call_chunk()],
        model="gemini-3.8-flash",
        response_id="resp-chat-sig",
    ).decode("utf-8")
    state: dict = {}
    chunks: list[dict] = []
    for block in sse.split("\n\n"):
        if not block.strip():
            continue
        event, data = parse_sse_event(block)
        for encoded in convert_sse_to_chat_chunks(event, data, state):
            text = encoded.decode("utf-8")
            if not text.startswith("data:") or "[DONE]" in text:
                continue
            payload = json.loads(text[5:].strip())
            if isinstance(payload, dict):
                chunks.append(payload)
    completed = finalize_chat_completion(state)
    tool_calls = completed["choices"][0]["message"]["tool_calls"]
    assert tool_calls[0]["extra_content"]["google"]["thought_signature"] == "sig-from-gemini"
    streamed_calls = [
        delta["tool_calls"][0]
        for chunk in chunks
        for delta in [chunk.get("choices", [{}])[0].get("delta") or {}]
        if isinstance(delta.get("tool_calls"), list) and delta["tool_calls"]
    ]
    assert streamed_calls
    assert streamed_calls[0]["extra_content"]["google"]["thought_signature"] == "sig-from-gemini"


def test_chat_completions_round_trips_gemini_thought_signature(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[dict] = []
    turn = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" not in request.url.path:
            return httpx.Response(404, json={"error": {"message": "missing"}})
        captured.append(json.loads(request.content))
        turn["n"] += 1
        if turn["n"] == 1:
            return httpx.Response(200, json=_cloudcode_function_call_chunk())
        return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))

    set_http_transport(httpx.MockTransport(handler))
    first = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "look it up"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        },
        headers=key_headers(key),
    )
    assert first.status_code == 200, first.text
    tool_call = first.json()["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["extra_content"]["google"]["thought_signature"] == "sig-from-gemini"
    clear_thought_signature_cache()
    second = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [
                {"role": "user", "content": "look it up"},
                {"role": "assistant", "content": None, "tool_calls": [tool_call]},
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": "found it",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        },
        headers=key_headers(key),
    )
    assert second.status_code == 200, second.text
    assert "ok" in second.json()["choices"][0]["message"]["content"]
    assert len(captured) == 2
    contents = captured[1]["request"]["contents"]
    model_parts = next(item["parts"] for item in contents if item.get("role") == "model")
    assert model_parts[0]["functionCall"]["name"] == "lookup"
    assert model_parts[0]["thoughtSignature"] == "sig-from-gemini"


def test_chat_completions_stream_emits_thought_signature(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, json=_cloudcode_function_call_chunk())
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    streamed = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "stream": True,
            "messages": [{"role": "user", "content": "look it up"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                }
            ],
        },
        headers=key_headers(key),
    )
    assert streamed.status_code == 200, streamed.text
    assert "sig-from-gemini" in streamed.text
    assert '"extra_content"' in streamed.text


def test_chat_hosted_image_generation_is_stripped_like_responses(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini-3.8-flash",
            "messages": [{"role": "user", "content": "Reply with ok only."}],
            "tools": [
                {"type": "image_generation"},
                {
                    "type": "function",
                    "function": {
                        "name": "apply_patch",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured
    dumped = json.dumps(captured[0])
    assert "image_generation" not in dumped
    names = {item["name"] for item in captured[0]["request"]["tools"][0]["functionDeclarations"]}
    assert names == {"apply_patch"}


def test_claude_capacity_503_is_rate_limit_and_does_not_disable_account(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    generate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_calls
        if "streamGenerateContent" not in request.url.path:
            return httpx.Response(404, json={"error": {"message": "missing"}})
        generate_calls += 1
        body = json.loads(request.content)
        if body.get("model") == "claude-opus-4-6-thinking":
            return httpx.Response(
                503,
                json=[
                    {
                        "error": {
                            "code": 503,
                            "message": "No capacity available for model claude-opus-4-6-thinking on the server",
                        }
                    }
                ],
            )
        return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))

    set_http_transport(httpx.MockTransport(handler))
    blocked = client.post(
        "/v1/responses",
        json={"model": "claude-opus-4-6-thinking", "input": "Reply with ok only."},
        headers=key_headers(key),
    )
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["code"] == "rate_limit_exceeded"
    recovered = client.post(
        "/v1/responses",
        json={"model": "gemini-3.8-flash", "input": "Reply with ok only."},
        headers=key_headers(key),
    )
    assert recovered.status_code == 200, recovered.text
    assert "ok" in recovered.text
    assert generate_calls == 4


def test_unpatched_ready_runs_translation(client):
    assert get_adapter("antigravity").ready is True
    key = _import_antigravity_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "gemini-3.8-flash", "input": "hi"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "ok" in response.text


def test_gpt_and_grok_do_not_hit_daily_cloudcode(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    _import_antigravity_key(client)
    grok_imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert grok_imported.status_code == 200, grok_imported.text
    grok_key = client.post(
        "/admin/api/keys",
        json={"name": "grok-text", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    ).json()["key"]
    codex_body = import_pool(client, auth_payload("acct-codex-ag"))
    codex_key = codex_body["generated_api_key"]["key"]
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host == "daily-cloudcode-pa.googleapis.com":
            return httpx.Response(500, json={"error": {"message": "antigravity-should-not-run"}})
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=text_sse("resp-other"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    gpt = client.post(
        "/v1/responses",
        json={"model": "gpt-6-astra", "input": "hi"},
        headers=key_headers(codex_key),
    )
    grok = client.post(
        "/v1/responses",
        json={"model": "grok-4.6", "input": "hi"},
        headers=key_headers(grok_key),
    )
    assert gpt.status_code == 200, gpt.text
    assert grok.status_code == 200, grok.text
    assert "daily-cloudcode-pa.googleapis.com" not in hosts


def test_huge_instructions_are_invalid_request_not_quota(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    generate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_calls
        if "streamGenerateContent" in request.url.path:
            generate_calls += 1
            return httpx.Response(
                429,
                json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota"}},
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemini-3.8-flash",
            "instructions": "x" * (MAX_SYSTEM_INSTRUCTION_CHARS + 1),
            "input": "ok",
        },
        headers=key_headers(key),
    )
    assert response.status_code == 400, response.text
    assert response.status_code != 429
    assert response.json()["error"]["code"] == "invalid_request"
    assert generate_calls == 0


def test_hosted_tools_are_not_forwarded_or_promised(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "gemini-3.8-flash",
            "instructions": "You are a coding agent.",
            "input": "Reply with ok only.",
            "tools": [
                {"type": "image_generation"},
                {"type": "web_search"},
                {"type": "computer_use"},
                {"type": "computer_use_preview"},
                {
                    "type": "function",
                    "name": "apply_patch",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured
    request = captured[0]["request"]
    declarations = request["tools"][0]["functionDeclarations"]
    names = {item["name"] for item in declarations}
    assert names == {"apply_patch"}
    dumped = json.dumps(captured[0])
    assert "image_generation" not in dumped
    assert "web_search" not in dumped
    assert "computer_use" not in dumped
    assert "computer_use_preview" not in dumped
    system_text = request["systemInstruction"]["parts"][0]["text"]
    assert "image_generation" not in system_text
    assert "web_search" not in system_text
    assert "computer_use" not in system_text
    assert "apply_patch" not in system_text


def test_translator_maps_tools_request_to_function_envelope():
    responses = _load("responses_tools_request.json")
    expected = _load("cloudcode_function_envelope.json")
    envelope = responses_to_cloudcode(responses, project="example-cloud-project")
    request = envelope["request"]
    assert envelope["project"] == expected["project"]
    assert envelope["requestType"] == "agent"
    declarations = request["tools"][0]["functionDeclarations"]
    assert [item["name"] for item in declarations] == ["apply_patch"]
    assert request["contents"][0]["role"] == "user"
    assert request["contents"][1]["parts"][0]["functionCall"]["name"] == "apply_patch"
    assert request["contents"][2]["parts"][0]["functionResponse"]["name"] == "apply_patch"
    assert request["contents"][2]["parts"][0]["functionResponse"]["response"] == {"content": "ok"}
    assert "safetySettings" not in request


def test_translator_maps_custom_apply_patch_and_cli_history():
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "custom", "name": "apply_patch", "description": "edit files"},
                        {
                            "type": "function",
                            "name": "exec_command",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        },
                        {"type": "web_search"},
                        {"type": "computer_use_preview"},
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Patch README."}],
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "call_apply_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_apply_1",
                    "output": "ok",
                },
            ],
        },
        project="example-cloud-project",
    )
    names = [item["name"] for item in envelope["request"]["tools"][0]["functionDeclarations"]]
    assert names == ["apply_patch", "exec_command"]
    apply_patch = envelope["request"]["tools"][0]["functionDeclarations"][0]
    assert apply_patch["parameters"]["type"] == "object"
    assert "input" in apply_patch["parameters"]["properties"]
    contents = envelope["request"]["contents"]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "apply_patch"
    assert contents[1]["parts"][0]["functionCall"]["args"] == {
        "input": "*** Begin Patch\n*** End Patch\n"
    }
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "apply_patch"
    assert contents[2]["parts"][0]["functionResponse"]["response"] == {"content": "ok"}


def test_view_image_output_inlines_image_and_ends_on_user_turn():
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {"type": "message", "role": "user", "content": "look"},
                {
                    "type": "function_call",
                    "call_id": "call_1033847",
                    "name": "view_image",
                    "arguments": '{"path":"screenshot.png"}',
                    "thought_signature": "sig-view",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1033847",
                    "output": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,QUJD",
                            "detail": "high",
                        }
                    ],
                },
            ],
            "tools": [{"type": "function", "name": "view_image", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    contents = envelope["request"]["contents"]
    assert contents[-1]["role"] == "user"
    parts = contents[-1]["parts"]
    assert parts[0]["functionResponse"]["name"] == "view_image"
    assert parts[0]["functionResponse"]["response"] == {"content": "ok"}
    assert parts[1]["inlineData"] == {"mimeType": "image/png", "data": "QUJD"}
    dumped = json.dumps(envelope)
    assert "data:image/png;base64" not in dumped


def test_empty_tool_output_still_closes_model_turn():
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {
                    "type": "function_call",
                    "call_id": "call_empty",
                    "name": "view_image",
                    "arguments": "{}",
                    "thought_signature": "sig",
                },
                {"type": "function_call_output", "call_id": "call_empty", "output": []},
            ],
            "tools": [{"type": "function", "name": "view_image", "parameters": {"type": "object"}}],
        },
        project="example-cloud-project",
    )
    contents = envelope["request"]["contents"]
    assert contents[-1]["role"] == "user"
    assert contents[-1]["parts"][0]["functionResponse"]["name"] == "view_image"
    assert contents[-1]["parts"][0]["functionResponse"]["response"] == {"content": "ok"}


def test_claude_replay_sends_function_call_id_for_tool_use():
    envelope = responses_to_cloudcode(
        {
            "model": "claude-sonnet-4-6",
            "input": [
                {"type": "message", "role": "user", "content": "make a folder"},
                {
                    "type": "function_call",
                    "call_id": "call_1153088",
                    "name": "exec_command",
                    "arguments": '{"cmd":"echo hi"}',
                    "thought_signature": "sig-claude",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1153088",
                    "output": "hi",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
                }
            ],
        },
        project="example-cloud-project",
    )
    contents = envelope["request"]["contents"]
    call = contents[1]["parts"][0]["functionCall"]
    result = contents[2]["parts"][0]["functionResponse"]
    assert call["name"] == "exec_command"
    assert call["id"] == "call_1153088"
    assert result["name"] == "exec_command"
    assert result["id"] == "call_1153088"
    assert result["response"] == {"content": "hi"}
    assert contents[-1]["role"] == "user"


def test_translator_drops_orphan_outputs_and_never_uses_call_id_as_name():
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "input": [
                {"type": "message", "role": "user", "content": "hi"},
                {
                    "type": "function_call_output",
                    "call_id": "call_missing",
                    "output": "ok",
                },
            ],
        },
        project="example-cloud-project",
    )
    dumped = json.dumps(envelope)
    assert "functionResponse" not in dumped
    assert "call_missing" not in dumped
    assert envelope["request"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_translator_coerces_schema_type_to_string():
    payload = _load("schema_type_must_be_string.json")
    envelope = responses_to_cloudcode(
        {
            "model": "gemini-3.8-flash",
            "tools": [
                {
                    "type": "function",
                    "name": "read_file",
                    "parameters": payload["invalid_parameters"],
                }
            ],
            "input": "hi",
        },
        project="example-cloud-project",
    )
    params = envelope["request"]["tools"][0]["functionDeclarations"][0]["parameters"]
    assert params == payload["cleaned_parameters"]

    def _assert_types(node: object) -> None:
        if isinstance(node, dict):
            if "type" in node:
                assert isinstance(node["type"], str)
            for value in node.values():
                _assert_types(value)
        elif isinstance(node, list):
            for value in node:
                _assert_types(value)

    _assert_types(params)
    assert clean_json_schema(payload["invalid_parameters"]) == payload["cleaned_parameters"]


def test_claude_tool_schema_matches_draft_2020_12():
    envelope = responses_to_cloudcode(
        {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "type": "function",
                    "name": "count_lines",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "limit": {"type": "integer"},
                            "pair": {
                                "type": "array",
                                "prefixItems": [{"type": "string"}, {"type": "string"}],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                {"type": "custom", "name": "apply_patch"},
            ],
            "input": "hi",
        },
        project="example-cloud-project",
    )
    decls = envelope["request"]["tools"][0]["functionDeclarations"]
    by_name = {item["name"]: item["parameters"] for item in decls}
    count = by_name["count_lines"]
    assert count["type"] == "object"
    assert count["required"] == []
    assert count["properties"]["limit"]["type"] == "number"
    assert count["properties"]["pair"]["type"] == "array"
    assert "prefixItems" not in count["properties"]["pair"]
    assert count["properties"]["pair"]["items"]["type"] == "string"
    patch = by_name["apply_patch"]
    assert patch["type"] == "object"
    assert "required" in patch


def test_claude_flattens_optional_null_unions_from_mcp_image_tools():
    envelope = responses_to_cloudcode(
        {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__transfer_station_image",
                    "tools": [
                        {
                            "type": "function",
                            "name": "generate_image",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "prompt": {"type": "string"},
                                    "n": {"type": "integer"},
                                    "size": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "output_compression": {
                                        "anyOf": [{"type": "integer"}, {"type": "null"}]
                                    },
                                    "file_ids": {
                                        "anyOf": [
                                            {"type": "array", "items": {"type": "string"}},
                                            {"type": "null"},
                                        ]
                                    },
                                },
                                "required": ["prompt"],
                            },
                        }
                    ],
                }
            ],
            "input": "hi",
        },
        project="example-cloud-project",
    )
    params = envelope["request"]["tools"][0]["functionDeclarations"][0]["parameters"]
    dumped = json.dumps(params)
    assert "anyOf" not in dumped
    assert '"type": "null"' not in dumped
    assert params["properties"]["n"]["type"] == "number"
    assert params["properties"]["size"]["type"] == "string"
    assert params["properties"]["output_compression"]["type"] == "number"
    assert params["properties"]["file_ids"]["type"] == "array"
    assert params["properties"]["file_ids"]["items"]["type"] == "string"
    assert params["required"] == ["prompt"]
    assert clean_claude_json_schema({"anyOf": [{"type": "string"}, {"type": "null"}]}) == {
        "type": "string"
    }


def test_claude_collapses_object_unions_to_single_object():
    envelope = responses_to_cloudcode(
        {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "type": "function",
                    "name": "fork_thread",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "environment": {
                                "anyOf": [
                                    {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string", "enum": ["same-directory"]}
                                        },
                                        "required": ["type"],
                                        "additionalProperties": False,
                                    },
                                    {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string", "enum": ["worktree"]}
                                        },
                                        "required": ["type"],
                                        "additionalProperties": False,
                                    },
                                ]
                            }
                        },
                    },
                },
                {
                    "type": "function",
                    "name": "transfer_voice_call",
                    "parameters": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"threadId": {"type": "string"}},
                                "required": ["threadId"],
                            },
                            {
                                "type": "object",
                                "properties": {"return": {"type": "boolean"}},
                                "required": ["return"],
                            },
                        ]
                    },
                },
            ],
            "input": "hi",
        },
        project="example-cloud-project",
    )
    decls = {item["name"]: item["parameters"] for item in envelope["request"]["tools"][0]["functionDeclarations"]}
    dumped = json.dumps(decls)
    assert "anyOf" not in dumped
    env = decls["fork_thread"]["properties"]["environment"]
    assert env["type"] == "object"
    assert env["properties"]["type"]["enum"] == ["same-directory", "worktree"]
    voice = decls["transfer_voice_call"]
    assert voice["type"] == "object"
    assert "threadId" in voice["properties"]
    assert "return" in voice["properties"]
    assert voice["required"] == []


def test_claude_sanitizes_dotted_tool_names():
    envelope = responses_to_cloudcode(
        {
            "model": "claude-sonnet-4-6",
            "tools": [
                {
                    "type": "function",
                    "name": "safety_settings.get_family_info",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "type": "function",
                    "name": "sites.add_custom_domain",
                    "parameters": {
                        "type": "object",
                        "properties": {"hostname": {"type": "string"}},
                        "required": ["hostname"],
                    },
                },
            ],
            "input": "hi",
        },
        project="example-cloud-project",
    )
    decls = envelope["request"]["tools"][0]["functionDeclarations"]
    names = [item["name"] for item in decls]
    assert names == ["safety_settings_get_family_info", "sites_add_custom_domain"]
    assert "." not in "".join(names)
    assert envelope["_ts_claude_names"]["safety_settings_get_family_info"] == "safety_settings.get_family_info"
    sse = cloudcode_payloads_to_codex_sse(
        [
            {
                "response": {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "functionCall": {
                                            "name": "sites_add_custom_domain",
                                            "args": {"hostname": "example.com"},
                                        }
                                    }
                                ],
                            }
                        }
                    ]
                }
            }
        ],
        model="claude-sonnet-4-6",
        response_id="resp-name",
        name_map=envelope["_ts_claude_names"],
    ).decode("utf-8")
    assert '"name": "sites.add_custom_domain"' in sse
    assert '"name": "sites_add_custom_domain"' not in sse


def test_clean_json_schema_survives_cyclic_refs():
    cyclic = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "child": {"$ref": "#/$defs/Node"},
                    "items": {
                        "type": "array",
                        "items": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]},
                    },
                },
            }
        },
        "$ref": "#/$defs/Node",
    }
    cleaned = clean_json_schema(cyclic)
    assert isinstance(cleaned, dict)
    assert cleaned.get("type") == "object"
    shaped = clean_claude_json_schema(cyclic)
    assert shaped["type"] == "object"
    assert "properties" in shaped


def test_stream_unwrap_emits_function_call():
    chunk = {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "apply_patch",
                                    "args": {"path": "README.md", "patch": "x"},
                                }
                            }
                        ],
                    }
                }
            ]
        }
    }
    sse = cloudcode_payloads_to_codex_sse(
        [chunk], model="gemini-3.8-flash", response_id="resp-ag-fn"
    ).decode("utf-8")
    assert "event: response.output_item.added" in sse
    assert "event: response.function_call_arguments.done" in sse
    assert "event: response.output_item.done" in sse
    assert '"type": "function_call"' in sse
    assert '"name": "apply_patch"' in sse
    assert "README.md" in sse
    assert "event: response.output_text.delta" not in sse


def test_gateway_function_call_then_function_response(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "response": {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [
                                        {
                                            "functionCall": {
                                                "name": "apply_patch",
                                                "args": {
                                                    "path": "README.md",
                                                    "patch": "*** Begin Patch\n*** End Patch\n",
                                                },
                                            }
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                },
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    first = client.post(
        "/v1/responses",
        json={
            "model": "gemini-3.8-flash",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "description": "Apply a unified patch to a file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "patch": {"type": "string"},
                        },
                        "required": ["path", "patch"],
                    },
                }
            ],
            "input": "Patch README.",
        },
        headers=key_headers(key),
    )
    assert first.status_code == 200, first.text
    assert "function_call" in first.text
    assert "apply_patch" in first.text
    assert "response.output_item.done" in first.text
    second = client.post(
        "/v1/responses",
        json=_load("responses_tools_request.json"),
        headers=key_headers(key),
    )
    assert second.status_code == 200, second.text
    assert len(captured) == 2
    inbound = captured[1]["request"]["contents"]
    assert inbound[1]["parts"][0]["functionCall"]["name"] == "apply_patch"
    response_part = inbound[2]["parts"][0]["functionResponse"]
    assert response_part["name"] == "apply_patch"
    assert response_part["name"] != "call_apply_1"
    assert isinstance(response_part["response"], dict) and response_part["response"]
    names = {item["name"] for item in captured[1]["request"]["tools"][0]["functionDeclarations"]}
    assert names == {"apply_patch"}
    dumped = json.dumps(captured[1]["request"]["tools"])
    assert "image_generation" not in dumped
    assert "web_search" not in dumped
    assert "computer_use" not in dumped


def test_function_declarations_keep_named_functions_from_fixture():
    ignored = _load("hosted_tools_ignored.json")
    payload = {
        "tools": [
            {"type": "custom", "name": "apply_patch"},
            {
                "type": "function",
                "name": "exec_command",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
            {
                "type": "function",
                "name": "shell",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
            {"type": "image_generation"},
            {"type": "web_search"},
            {"type": "computer_use"},
            {"type": "computer_use_preview"},
        ],
        "input": "hi",
    }
    names = [item["name"] for item in function_declarations(payload)]
    assert names == ignored["keep_function_names"]
    assert not set(ignored["drop_from_tools"]) & set(names)


def test_parse_fetch_available_models_quota_windows():
    snapshot = parse_quota_snapshot(
        {
            "models": {
                "gemini-3.8-flash-tiered": {
                    "quotaInfo": {
                        "remainingFraction": 0.8,
                        "resetTime": "2026-09-17T12:00:00Z",
                    }
                },
                "gemini-3.8-flash-high": {
                    "quotaInfo": {
                        "remainingFraction": 0.8,
                        "resetTime": "2026-09-17T12:00:00Z",
                    }
                },
                "claude-sonnet-4-6": {
                    "quotaInfo": {
                        "remainingFraction": 1,
                        "resetTime": "2026-09-17T18:00:00Z",
                    }
                },
                "claude-opus-4-6-thinking": {
                    "quotaInfo": {
                        "remainingFraction": 1,
                        "resetTime": "2026-09-17T18:00:00Z",
                    }
                },
                "tab_flash_lite_preview": {
                    "quotaInfo": {"remainingFraction": 0.1, "resetTime": "2026-09-17T12:00:00Z"}
                },
                "gemini-3.1-flash-image": {
                    "quotaInfo": {"remainingFraction": 0.2, "resetTime": "2026-09-17T12:00:00Z"}
                },
                "gpt-oss-120b": {
                    "quotaInfo": {
                        "remainingFraction": 0.4,
                        "resetTime": "2026-09-17T12:00:00Z",
                    }
                },
            }
        }
    )
    assert snapshot is not None
    assert snapshot["quota_kind"] == "credits"
    assert snapshot["message"] is None
    families = [item["limit_name"] for item in snapshot["limits"]]
    assert families == ["Gemini", "Claude", "GPT-OSS"]
    gemini = snapshot["limits"][0]["primary"]
    assert gemini["remaining_percent"] == 80.0
    assert gemini["used_percent"] == 20.0
    assert gemini["window_label"] == "Gemini"
    assert gemini["resets_at"] == int(dt.datetime(2026, 9, 17, 12, tzinfo=dt.UTC).timestamp())
    claude = snapshot["limits"][1]["primary"]
    assert claude["remaining_percent"] == 100.0
    assert claude["window_label"] == "Claude"
    assert parse_quota_snapshot({"models": {}}) is None
    assert parse_quota_snapshot({"models": {"tab_flash_lite_preview": {"quotaInfo": {"remainingFraction": 1}}}}) is None


def test_claude_capacity_503_is_retried_then_succeeds(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    key = _import_antigravity_key(client)
    generate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_calls
        if "streamGenerateContent" not in request.url.path:
            return httpx.Response(404, json={"error": {"message": "missing"}})
        generate_calls += 1
        body = json.loads(request.content)
        think = ((body.get("request") or {}).get("generationConfig") or {}).get("thinkingConfig") or {}
        if body.get("model") == "claude-sonnet-4-6":
            assert think.get("thinkingBudget") == 1024
            assert ((body.get("request") or {}).get("generationConfig") or {}).get("maxOutputTokens") == 64000
            if generate_calls == 1:
                return httpx.Response(
                    503,
                    json=[
                        {
                            "error": {
                                "code": 503,
                                "message": "No capacity available for model claude-sonnet-4-6 on the server",
                                "status": "UNAVAILABLE",
                            }
                        }
                    ],
                )
        return httpx.Response(200, json=_load("cloudcode_stream_text_chunk.json"))

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "claude-sonnet-4-6", "reasoning_effort": "high", "input": "Reply with ok only."},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "ok" in response.text
    assert generate_calls == 2


def test_antigravity_quotas_use_fetch_available_models(client, monkeypatch):
    _enable_antigravity(monkeypatch)
    _import_antigravity_key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(":fetchAvailableModels"):
            return httpx.Response(
                200,
                json={
                    "models": {
                        "gemini-3.8-flash-tiered": {
                            "quotaInfo": {
                                "remainingFraction": 0.55,
                                "resetTime": "2026-09-17T12:00:00Z",
                            }
                        }
                    }
                },
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    row = response.json()["data"][0]
    assert row["provider"] == "antigravity"
    assert row["quota_kind"] == "credits"
    assert row["limits"][0]["primary"]["remaining_percent"] == 55.0
    assert row["limits"][0]["primary"]["window_label"] == "Gemini"
    assert row["reset_credits"] == {"available_count": 0, "credits": []}
