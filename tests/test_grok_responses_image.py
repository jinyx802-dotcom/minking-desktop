from __future__ import annotations

import base64
import json

import httpx
from conftest import ADMIN_HEADERS

from app.http_client import set_http_transport
from app.providers.grok import sanitize_grok_responses_payload, shape_grok_image_body


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


def key_headers(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


def grok_image_sse(encoded: str, response_id: str = "resp-grok-img") -> bytes:
    item = {
        "id": "img-1",
        "type": "image_generation_call",
        "status": "completed",
        "result": encoded,
    }
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "grok-4.6"}}),
        (
            "response.output_item.done",
            {"type": "response.output_item.done", "item": item},
        ),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "model": "grok-4.6",
                    "output": [item],
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                },
            },
        ),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def grok_text_sse(response_id: str, text: str = "ok") -> bytes:
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "grok-4.6"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": text}),
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "model": "grok-4.6",
                    "output_text": text,
                    "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                },
            },
        ),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def _import_grok_key(client) -> str:
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-image", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def _image_paths(seen: list[str]) -> list[str]:
    return [path for path in seen if "/images/" in path]


def test_shape_grok_image_body_defaults_b64_and_object_image():
    generated = shape_grok_image_body(
        "generation",
        {"model": "grok-imagine-image-2.0", "prompt": "draw"},
    )
    assert generated["response_format"] == "b64_json"
    edited = shape_grok_image_body(
        "edit",
        {
            "model": "grok-imagine-image-2.0",
            "prompt": "darker",
            "image": "data:image/png;base64,aaaa",
        },
    )
    assert edited["image"] == {"url": "data:image/png;base64,aaaa", "type": "image_url"}
    assert isinstance(edited["image"], dict)
    assert edited["image"].get("type") == "image_url"
    multi = shape_grok_image_body(
        "edits",
        {
            "prompt": "mix",
            "image": ["data:image/png;base64,aaaa", "data:image/png;base64,bbbb"],
            "mask": "data:image/png;base64,mask",
        },
    )
    assert "image" not in multi
    assert multi["images"] == [
        {"url": "data:image/png;base64,aaaa", "type": "image_url"},
        {"url": "data:image/png;base64,bbbb", "type": "image_url"},
    ]
    assert multi["mask"] == {"url": "data:image/png;base64,mask", "type": "image_url"}


def test_shape_grok_image_body_maps_openai_size_and_drops_extra_fields():
    shaped = shape_grok_image_body(
        "generation",
        {
            "model": "grok-imagine-image-2.0",
            "prompt": "draw",
            "size": "1024x1024",
            "quality": "hd",
            "background": "transparent",
            "output_format": "png",
            "moderation": "low",
            "style": "vivid",
        },
    )
    assert shaped["aspect_ratio"] == "1:1"
    assert shaped["quality"] == "medium"
    assert "size" not in shaped
    assert "background" not in shaped
    assert "output_format" not in shaped
    assert "moderation" not in shaped
    assert "style" not in shaped
    assert shaped["response_format"] == "b64_json"
    portrait = shape_grok_image_body(
        "generation",
        {"prompt": "draw", "size": "1024x1792", "aspect_ratio": "4:3"},
    )
    assert portrait["aspect_ratio"] == "4:3"
    assert shape_grok_image_body("generation", {"prompt": "draw", "size": "auto"})[
        "aspect_ratio"
    ] == "auto"


def test_sanitize_grok_tool_choice_objects_become_required():
    image = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": "draw",
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
        }
    )
    assert image["tool_choice"] == "required"
    named = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": "lookup",
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "name": "lookup"},
        }
    )
    assert named["tool_choice"] == "required"
    nested = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": "lookup",
            "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        }
    )
    assert nested["tool_choice"] == "required"
    auto = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": "hi",
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
        }
    )
    assert auto["tool_choice"] == "auto"


def test_sanitize_grok_keeps_hosted_image_generation():
    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": "draw a cat",
            "tools": [
                {
                    "type": "image_generation",
                    "model": "gpt-image-2",
                    "size": "1024x1024",
                    "action": "generate",
                    "quality": "hd",
                }
            ],
            "tool_choice": {"type": "image_generation", "model": "gpt-image-2"},
        }
    )
    assert payload["tools"] == [{"type": "image_generation"}]
    assert payload["tool_choice"] == "required"


def test_grok_responses_image_tool_forwards_to_responses_and_urlizes_codex_ua(client):
    key = _import_grok_key(client)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"aaaa").decode()
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_image_sse(encoded),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    streamed = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": "draw a cat",
            "stream": True,
            "tools": [{"type": "image_generation", "model": "gpt-image-2", "size": "1024x1024"}],
        },
        headers=key_headers(key, **{"User-Agent": "codex_exec/0.155.0-alpha.2.6"}),
    )
    assert streamed.status_code == 200, streamed.text
    assert any(path.endswith("/responses") for path in seen)
    assert _image_paths(seen) == []
    assert captured[0]["tools"] == [{"type": "image_generation"}]
    assert "image_generation_call" in streamed.text
    assert encoded not in streamed.text
    assert "/v1/files/file_" in streamed.text

    collected = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": "draw a cat",
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
        },
        headers=key_headers(key, **{"User-Agent": "openai-python/1.40.0"}),
    )
    assert collected.status_code == 200, collected.text
    output = collected.json()["output"]
    assert output[0]["type"] == "image_generation_call"
    assert output[0]["result"] == encoded
    assert "/v1/files/file_" not in collected.text
    assert captured[1]["tool_choice"] == "required"


def test_grok_responses_without_image_tool_does_not_call_images(client):
    key = _import_grok_key(client)
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_text_sse("resp-grok-ok"),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "grok-4.6", "input": "only reply ok"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert _image_paths(seen) == []
    assert captured
    tools = captured[0].get("tools") or []
    assert all(
        (tool.get("type") if isinstance(tool, dict) else None) != "image_generation"
        for tool in tools
    )
    assert "image_generation" not in json.dumps(captured[0].get("tools") or [])


def test_grok_coding_request_strips_image_generation_and_skips_images(client):
    key = _import_grok_key(client)
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_text_sse("resp-grok-code"),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": "only reply ok",
            "tool_choice": "auto",
            "tools": [
                {"type": "image_generation"},
                {"type": "apply_patch"},
                {
                    "type": "function",
                    "name": "shell",
                    "parameters": {"type": "object", "properties": {}},
                },
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert _image_paths(seen) == []
    assert any(path.endswith("/responses") for path in seen)
    assert captured
    tools = captured[0].get("tools") or []
    types = [tool.get("type") for tool in tools if isinstance(tool, dict)]
    names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
    assert "image_generation" not in types
    assert "image_generation" not in names
    assert "apply_patch" in names
    assert "shell" in names


def test_grok_responses_does_not_inject_image_generation(client):
    key = _import_grok_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_text_sse("resp-no-inject"),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "grok-4.6", "input": "only reply ok", "stream": True},
        headers=key_headers(key, **{"User-Agent": "codex_cli_rs/0.154.0"}),
    )
    assert response.status_code == 200, response.text
    assert captured
    dumped = json.dumps(captured[0])
    assert "image_generation" not in dumped
    assert "tools" not in captured[0]


def test_grok_responses_edit_forwards_input_image_to_responses(client):
    key = _import_grok_key(client)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"edit").decode()
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_image_sse(encoded, "resp-grok-edit"),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "make it darker"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aaaa"},
                    ],
                }
            ],
            "tools": [{"type": "image_generation"}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert any(path.endswith("/responses") for path in seen)
    assert _image_paths(seen) == []
    dumped = json.dumps(captured[0])
    assert "data:image/png;base64,aaaa" in dumped
    assert captured[0]["tools"] == [{"type": "image_generation"}]
    assert response.json()["output"][0]["result"] == encoded


def test_images_http_grok_url_is_fetched_to_b64(client):
    key = _import_grok_key(client)
    png = b"\x89PNG\r\n\x1a\n" + b"from-url"
    encoded = base64.b64encode(png).decode()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and str(request.url) == "http://img.test/cat.png":
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"url": "http://img.test/cat.png"}]})
        if request.url.path.endswith("/responses"):
            return httpx.Response(400, json={"error": {"message": "hosted tool rejected"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/generations",
        json={"model": "grok-imagine-image-2.0", "prompt": "draw a cat"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == encoded
    assert any(item.startswith("GET ") for item in seen)
    assert any(item.endswith("/images/generations") for item in seen)
    assert not any(item.endswith("/responses") for item in seen)


def test_grok_responses_keeps_image_generation_with_web_search(client):
    key = _import_grok_key(client)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"search").decode()
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_image_sse(encoded, "resp-grok-search"),
            )
        if "/images/" in request.url.path:
            return httpx.Response(500, json={"error": {"message": "should not generate"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": "draw the weather",
            "tools": [{"type": "image_generation"}, {"type": "web_search"}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    types = [tool.get("type") for tool in captured[0].get("tools") or [] if isinstance(tool, dict)]
    assert "image_generation" in types
    assert "web_search" in types
