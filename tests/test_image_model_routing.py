from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
from conftest import ADMIN_HEADERS, auth_payload, import_pool

from app.config import settings
from app.http_client import set_http_transport
from app.providers.antigravity import IMAGE_MODEL
from app.providers.registry import get_adapter, resolve_image_request_model

ANTIGRAVITY_OAUTH = Path(__file__).resolve().parent / "fixtures" / "antigravity" / "oauth_account.example.json"


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


def _cloudcode_image(encoded: str) -> dict:
    return {
        "response": {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}],
                    }
                }
            ]
        }
    }


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
        json={"name": "grok-image-route", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def _import_antigravity_key(client, monkeypatch) -> str:
    monkeypatch.setattr(get_adapter("antigravity"), "ready", True)
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("oauth.json", ANTIGRAVITY_OAUTH.read_bytes(), "application/json")},
        data={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/admin/api/keys",
        json={"name": "ag-image-route", "preferred_account_id": "antigravity:user@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def test_codex_responses_locks_image_generation_model(client):
    body = import_pool(client, auth_payload("acct-lock-image"))
    key = body["generated_api_key"]["key"]
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"locked").decode()
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/images/generations") or request.url.path.endswith("/images/edits"):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        return httpx.Response(400, json={"error": {"message": "hosted tool rejected"}})

    set_http_transport(httpx.MockTransport(handler))
    generated = client.post(
        "/v1/responses",
        json={
            "model": "gpt-6-astra",
            "input": "draw",
            "tools": [{"type": "image_generation", "model": "grok-imagine-image-2.0"}],
        },
        headers=key_headers(key),
    )
    edited = client.post(
        "/v1/responses",
        json={
            "model": "gpt-6-astra",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "draw"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aaaa"},
                    ],
                }
            ],
            "tools": [{"type": "image_generation", "model": "grok-imagine-image-2.0"}],
        },
        headers=key_headers(key),
    )
    assert generated.status_code == 200, generated.text
    assert edited.status_code == 200, edited.text
    assert any(path.endswith("/images/generations") for path in seen)
    assert any(path.endswith("/images/edits") for path in seen)
    assert not any(path.endswith("/responses") for path in seen)
    assert captured[0]["model"] == settings.codex_image_model
    assert captured[1]["model"] == settings.codex_image_model
    assert captured[1]["image"] == "data:image/png;base64,aaaa"
    assert generated.json()["output"][0]["type"] == "image_generation_call"
    assert generated.json()["output"][0]["result"] == encoded


def test_responses_rejects_image_model_as_top_level(client):
    body = import_pool(client, auth_payload("acct-reject-image-model"))
    key = body["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500, json={"error": {"message": "should not call upstream"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-image-2.5-flare", "input": "draw"},
        headers=key_headers(key),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert seen == []


def test_images_http_keeps_custom_codex_slug(client):
    body = import_pool(client, auth_payload("acct-custom-image"))
    key = body["generated_api_key"]["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-2", "prompt": "draw"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured[0]["model"] == "gpt-image-2"


def test_images_http_routes_grok_slug(client):
    key = _import_grok_key(client)
    seen: list[str] = []
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    generated = client.post(
        "/v1/images/generations",
        json={
            "model": "grok-imagine-image-2.0",
            "prompt": "draw",
            "size": "1024x1024",
            "quality": "hd",
            "background": "transparent",
        },
        headers=key_headers(key),
    )
    edited = client.post(
        "/v1/images/edits",
        json={"model": "grok-imagine-image-2.0", "prompt": "edit", "image": "data:image/png;base64,aaaa"},
        headers=key_headers(key),
    )
    assert generated.status_code == 200, generated.text
    assert edited.status_code == 200, edited.text
    assert seen[0].endswith("/images/generations")
    assert seen[1].endswith("/images/edits")
    assert captured[0]["model"] == settings.grok_image_model
    assert captured[0]["response_format"] == "b64_json"
    assert captured[0]["aspect_ratio"] == "1:1"
    assert captured[0]["quality"] == "medium"
    assert "size" not in captured[0]
    assert "background" not in captured[0]
    assert captured[1]["model"] == settings.grok_image_model
    assert captured[1]["response_format"] == "b64_json"
    assert captured[1]["image"] == {
        "url": "data:image/png;base64,aaaa",
        "type": "image_url",
    }


def test_images_http_grok_multipart_edit_sends_json_object(client):
    key = _import_grok_key(client)
    captured: list[dict] = []
    content_types: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        content_types.append(request.headers.get("content-type") or "")
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    edited = client.post(
        "/v1/images/edits",
        data={"model": "grok-imagine-image-2.0", "prompt": "edit"},
        files={"image": ("tiny.png", b"pngbytes", "image/png")},
        headers=key_headers(key),
    )
    assert edited.status_code == 200, edited.text
    assert captured
    assert "multipart" not in content_types[0]
    assert captured[0]["model"] == settings.grok_image_model
    assert captured[0]["response_format"] == "b64_json"
    assert captured[0]["image"]["type"] == "image_url"
    assert captured[0]["image"]["url"].startswith("data:image/")


def test_images_http_maps_text_model_to_provider_latest_image(client):
    grok_key = _import_grok_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    grok = client.post(
        "/v1/images/generations",
        json={"model": "grok-4.6", "prompt": "draw"},
        headers=key_headers(grok_key),
    )
    assert grok.status_code == 200, grok.text
    assert captured[-1]["path"].endswith("/images/generations")
    assert captured[-1]["body"]["model"] == settings.grok_image_model

    body = import_pool(client, auth_payload("acct-gpt-image-default"))
    gpt_key = body["generated_api_key"]["key"]
    gpt = client.post(
        "/v1/images/generations",
        json={"model": "gpt-6-astra", "prompt": "draw"},
        headers=key_headers(gpt_key),
    )
    assert gpt.status_code == 200, gpt.text
    assert captured[-1]["body"]["model"] == settings.codex_image_model


def test_resolve_image_request_model_uses_latest_per_provider():
    assert resolve_image_request_model("grok-4.6", default_provider="codex") == (
        "grok",
        settings.grok_image_model,
    )
    assert resolve_image_request_model("gpt-6-astra", default_provider="codex") == (
        "codex",
        settings.codex_image_model,
    )
    assert resolve_image_request_model("gemini-3.8-flash", default_provider="codex") == (
        "antigravity",
        settings.antigravity_image_model,
    )
    assert resolve_image_request_model("workbuddy/glm-5.2", default_provider="codex") == (
        "workbuddy",
        settings.workbuddy_image_model,
    )
    assert resolve_image_request_model(None, default_provider="grok") == (
        "grok",
        settings.grok_image_model,
    )
    assert resolve_image_request_model("gpt-image-2", default_provider="codex") == (
        "codex",
        "gpt-image-2",
    )


def test_gemini_images_http_generate_and_edit(client, monkeypatch):
    key = _import_antigravity_key(client, monkeypatch)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"gemini").decode()
    envelopes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            envelopes.append(json.loads(request.content))
            return httpx.Response(200, json=_cloudcode_image(encoded))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    generated = client.post(
        "/v1/images/generations",
        json={"model": IMAGE_MODEL, "prompt": "draw a cat"},
        headers=key_headers(key),
    )
    edited = client.post(
        "/v1/images/edits",
        json={"model": IMAGE_MODEL, "prompt": "darker", "image": "data:image/png;base64,aaaa"},
        headers=key_headers(key),
    )
    assert generated.status_code == 200, generated.text
    assert edited.status_code == 200, edited.text
    assert generated.json()["data"][0]["b64_json"] == encoded
    assert envelopes[0]["model"] == IMAGE_MODEL
    assert envelopes[0]["request"]["contents"][0]["parts"][0]["text"] == "draw a cat"
    assert envelopes[1]["request"]["contents"][0]["parts"][1]["inlineData"]["data"] == "aaaa"


def test_gemini_responses_generate_and_edit(client, monkeypatch):
    key = _import_antigravity_key(client, monkeypatch)
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"resp").decode()
    envelopes: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            envelopes.append(json.loads(request.content))
            return httpx.Response(200, json=_cloudcode_image(encoded))
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    generated = client.post(
        "/v1/responses",
        json={
            "model": "gemini-3.8-flash",
            "input": "draw a cat",
            "tools": [{"type": "image_generation", "model": "gpt-image-2"}],
        },
        headers=key_headers(key),
    )
    edited = client.post(
        "/v1/responses",
        json={
            "model": "gemini-3.8-flash",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "darker"},
                        {"type": "input_image", "image_url": "data:image/png;base64,aaaa"},
                    ],
                }
            ],
            "tools": [{"type": "image_generation"}],
        },
        headers=key_headers(key),
    )
    assert generated.status_code == 200, generated.text
    assert edited.status_code == 200, edited.text
    assert generated.json()["output"][0]["type"] == "image_generation_call"
    assert generated.json()["output"][0]["result"] == encoded
    assert envelopes[0]["model"] == IMAGE_MODEL
    assert "gpt-image-2" not in json.dumps(envelopes[0])
    assert envelopes[1]["request"]["contents"][0]["parts"][1]["inlineData"]["data"] == "aaaa"


def test_claude_responses_does_not_generate_image(client, monkeypatch):
    key = _import_antigravity_key(client, monkeypatch)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "streamGenerateContent" in request.url.path:
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "response": {
                        "candidates": [
                            {"content": {"role": "model", "parts": [{"text": "ok"}]}}
                        ]
                    }
                },
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "claude-sonnet-4-6",
            "input": "draw a cat",
            "tools": [{"type": "image_generation"}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured
    dumped = json.dumps(captured[0])
    assert "image_generation" not in dumped
    assert captured[0]["model"] != IMAGE_MODEL


def _import_workbuddy_key(client) -> str:
    payload = {
        "account": {"uid": "wb-image-1", "nickname": "WB image"},
        "auth": {
            "accessToken": "wb-access-test",
            "refreshToken": "wb-refresh-test",
            "expiresAt": 4_070_000_000,
            "domain": "copilot.tencent.com",
        },
    }
    imported = client.post(
        "/admin/api/accounts/import",
        data={"provider": "workbuddy"},
        files={"files[]": ("workbuddy-desktop.info", json.dumps(payload), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/admin/api/keys",
        json={"name": "wb-image-route", "preferred_account_id": "workbuddy:cn:wb-image-1"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    return created.json()["key"]


def test_image_generation_matrix_http_and_responses(client, monkeypatch):
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"matrix").decode()
    gpt_key = import_pool(client, auth_payload("acct-matrix-gpt"))["generated_api_key"]["key"]
    grok_key = _import_grok_key(client)
    gemini_key = _import_antigravity_key(client, monkeypatch)
    workbuddy_key = _import_workbuddy_key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        captured.append({"path": path, "model": body.get("model")})
        if "streamGenerateContent" in path:
            return httpx.Response(200, json=_cloudcode_image(encoded))
        if path.endswith("/images/generations") or "/v1/images/generations" in path:
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        if path.endswith("/responses"):
            item = {"id": "img-1", "type": "image_generation_call", "status": "completed", "result": encoded}
            sse = "".join(
                f"event: {event}\ndata: {json.dumps(data)}\n\n"
                for event, data in (
                    ("response.created", {"type": "response.created", "response": {"id": "resp-grok-img"}}),
                    ("response.output_item.done", {"type": "response.output_item.done", "item": item}),
                    (
                        "response.completed",
                        {"type": "response.completed", "response": {"id": "resp-grok-img", "output": [item]}},
                    ),
                )
            ).encode()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse)
        return httpx.Response(404, json={"error": {"message": path}})

    set_http_transport(httpx.MockTransport(handler))

    http_cases = [
        ("gpt", gpt_key, "gpt-6-astra", settings.codex_image_model, "/images/generations"),
        ("grok", grok_key, "grok-4.6", settings.grok_image_model, "/images/generations"),
        ("gemini", gemini_key, "gemini-3.8-flash", settings.antigravity_image_model, "streamGenerateContent"),
        ("workbuddy", workbuddy_key, "workbuddy/glm-5.2", settings.workbuddy_image_model, "/v1/images/generations"),
    ]
    for label, key, request_model, expected_model, expected_path in http_cases:
        captured.clear()
        response = client.post(
            "/v1/images/generations",
            json={"model": request_model, "prompt": "draw a cat"},
            headers=key_headers(key),
        )
        assert response.status_code == 200, f"{label} images http {response.text}"
        assert response.json()["data"][0]["b64_json"] == encoded, label
        hit = next((item for item in captured if expected_path in item["path"]), None)
        assert hit is not None, f"{label} missing {expected_path} in {captured}"
        if expected_path != "streamGenerateContent":
            assert hit["model"] == expected_model, f"{label} http model {hit['model']}"
        else:
            assert hit["model"] == expected_model, f"{label} gemini wire model {hit['model']}"

    responses_cases = [
        ("gpt", gpt_key, "gpt-6-astra", settings.codex_image_model, "/images/generations"),
        ("gemini", gemini_key, "gemini-3.8-flash", settings.antigravity_image_model, "streamGenerateContent"),
        ("workbuddy", workbuddy_key, "workbuddy/glm-5.2", settings.workbuddy_image_model, "/v1/images/generations"),
    ]
    for label, key, request_model, expected_model, expected_path in responses_cases:
        captured.clear()
        response = client.post(
            "/v1/responses",
            json={
                "model": request_model,
                "input": "draw a cat",
                "tools": [{"type": "image_generation"}],
                "tool_choice": {"type": "image_generation"},
            },
            headers=key_headers(key),
        )
        assert response.status_code == 200, f"{label} responses {response.text}"
        body = response.json()
        assert body["output"][0]["type"] == "image_generation_call", label
        assert body["output"][0]["result"] == encoded, label
        hit = next((item for item in captured if expected_path in item["path"]), None)
        assert hit is not None, f"{label} responses missing {expected_path} in {captured}"
        assert hit["model"] == expected_model, f"{label} responses model {hit['model']}"

    captured.clear()
    grok_responses = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": "draw a cat",
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
        },
        headers=key_headers(grok_key),
    )
    assert grok_responses.status_code == 200, grok_responses.text
    assert grok_responses.json()["output"][0]["type"] == "image_generation_call"
    assert any(item["path"].endswith("/responses") for item in captured)
    assert not any("/images/generations" in item["path"] for item in captured)
