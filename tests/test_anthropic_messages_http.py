from __future__ import annotations

import json

import httpx
from conftest import auth_payload, import_pool

from app.http_client import set_http_transport
from app.providers.codex_protocol import parse_sse_event


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


def test_messages_json_and_x_api_key(client):
    body = import_pool(client, auth_payload("acct-messages"))
    key = body["generated_api_key"]["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["instructions"] == ""
        assert payload["reasoning"]["context"] == "all_turns"
        assert payload["parallel_tool_calls"] is False
        assert "max_output_tokens" not in payload
        assert payload["input"][0]["role"] == "developer"
        assert payload["input"][1]["role"] == "user"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-msg-1", "hello"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/messages",
        headers={"x-api-key": key},
        json={
            "model": "gpt-5.6-sol",
            "max_tokens": 32,
            "stream": False,
            "system": "be brief",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"] == [{"type": "text", "text": "hello"}]
    assert payload["stop_reason"] == "end_turn"
    assert payload["usage"]["output_tokens"] == 1


def test_messages_stream_and_unauthorized_shape(client):
    body = import_pool(client, auth_payload("acct-messages-stream"))
    key = body["generated_api_key"]["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-msg-2", "ok"),
            )
        )
    )
    streamed = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "gpt-5.6-sol",
            "max_tokens": 16,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert streamed.status_code == 200, streamed.text
    types: list[str] = []
    for raw in streamed.text.split("\n\n"):
        if not raw.strip():
            continue
        _event, data = parse_sse_event(raw)
        types.append(json.loads(data)["type"])
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    denied = client.post(
        "/v1/messages",
        json={"model": "gpt-5.6-sol", "max_tokens": 8, "messages": [{"role": "user", "content": "x"}]},
    )
    assert denied.status_code == 401
    error = denied.json()
    assert error["type"] == "error"
    assert error["error"]["type"] == "authentication_error"


def test_messages_accepts_system_role(client):
    body = import_pool(client, auth_payload("acct-messages-system"))
    key = body["generated_api_key"]["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["instructions"] == ""
        assert payload["input"][0]["role"] == "developer"
        assert payload["input"][1]["role"] == "user"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-msg-system", "hello"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/messages",
        headers={"x-api-key": key},
        json={
            "model": "gpt-5.6-sol",
            "max_tokens": 32,
            "stream": False,
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "hi"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["content"] == [{"type": "text", "text": "hello"}]
