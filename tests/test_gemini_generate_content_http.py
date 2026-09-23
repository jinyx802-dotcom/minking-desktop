from __future__ import annotations

import json

import httpx
from conftest import auth_payload, import_pool

from app.http_client import set_http_transport


def sse(response_id: str, text: str = "OK") -> bytes:
    usage = {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "gpt-5.6-sol"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": text}),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "model": "gpt-5.6-sol",
                    "output_text": text,
                    "usage": usage,
                },
            },
        ),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def test_generate_content_json_and_goog_api_key(client):
    body = import_pool(client, auth_payload("acct-gemini"))
    key = body["generated_api_key"]["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["instructions"] == ""
        assert payload["reasoning"]["context"] == "all_turns"
        assert payload["input"][0]["role"] == "developer"
        assert payload["input"][1]["role"] == "user"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-gem-1", "hello"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1beta/models/gpt-5.6-sol:generateContent",
        headers={"x-goog-api-key": key},
        json={
            "model": "gpt-5.6-sol",
            "systemInstruction": {"parts": [{"text": "be brief"}]},
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["candidates"][0]["content"]["parts"][0]["text"] == "hello"
    assert payload["candidates"][0]["finishReason"] == "STOP"


def test_v1_prefixed_generate_content_unauthorized(client):
    response = client.post(
        "/v1/v1beta/models/gemini-3.8-flash:generateContent",
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    assert response.status_code in {401, 403}
    body = response.json()
    assert "error" in body
