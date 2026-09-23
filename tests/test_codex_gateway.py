from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging

import httpx
from conftest import ADMIN_HEADERS, ADMIN_PASSWORD, auth_payload, import_pool

from app import __version__
import app.codex_gateway as codex_gateway_module
from app.config import settings
from app.http_client import set_http_transport


def sse(response_id: str, text: str = "OK", usage: dict | None = None) -> bytes:
    usage = usage or {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "gpt-5.6-sol"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": text}),
        ("response.completed", {"type": "response.completed", "response": {"id": response_id, "model": "gpt-5.6-sol", "output_text": text, "usage": usage}}),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def key_headers(key: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", **extra}


def test_text_request_retries_after_5_10_20_seconds_before_success(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-retry-a"), auth_payload("acct-retry-b"))
    key = body["generated_api_key"]["key"]
    delays: list[float] = []
    accounts: list[str] = []

    async def fake_pause(delay_seconds: float) -> None:
        delays.append(delay_seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        accounts.append(request.headers["ChatGPT-Account-Id"])
        if len(accounts) < 4:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-after-retries"),
        )

    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "retry me"}]},
        headers=key_headers(key),
    )

    assert response.status_code == 200
    assert delays == [5.0, 10.0, 20.0]
    assert len(accounts) == 4 and len(set(accounts)) == 1


def test_text_request_returns_final_failure_after_three_retries(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-retry-fail"))
    key = body["generated_api_key"]["key"]
    delays: list[float] = []
    attempts = 0

    async def fake_pause(delay_seconds: float) -> None:
        delays.append(delay_seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": {"message": "temporary"}})

    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"input": "retry me"},
        headers=key_headers(key),
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert attempts == 4 and delays == [5.0, 10.0, 20.0]


def test_text_request_retries_immediate_upstream_400(client, monkeypatch, caplog):
    body = import_pool(client, auth_payload("acct-no-retry"))
    key = body["generated_api_key"]["key"]
    delays: list[float] = []
    attempts = 0

    async def fake_pause(delay_seconds: float) -> None:
        delays.append(delay_seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"error": {
            "message": "bad request containing private-upstream-text",
            "type": "invalid_request_error",
            "code": "invalid_value",
            "param": "input[2].call_id",
        }})

    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "bad upstream request"}]},
        headers=key_headers(key),
    )

    assert response.status_code == 400
    assert attempts == 1 and delays == []
    assert "error_type=invalid_request_error" in caplog.text
    assert "error_code=invalid_value" in caplog.text
    assert "error_param=input[2].call_id" in caplog.text
    assert '"item_types":{"message":1}' in caplog.text
    assert "private-upstream-text" not in caplog.text
    assert "bad upstream request" not in caplog.text


def test_health_home_and_only_codex_provider(client):
    health = client.get("/health").json()
    assert health["status"] == "degraded"
    assert health["codex_accounts"] == 0
    assert health["accounts"] == 0
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 200
    assert "MinKing" in home.text
    assert "下载" in home.text
    page = client.get("/admin").text
    assert "MinKing AI" in page
    assert "多供应商网关" in page
    assert "仪表盘" in page
    assert "调用明细" in page
    assert "会话绑定" not in page
    providers = client.get("/v1/providers").json()["data"]
    assert [item["id"] for item in providers] == ["codex", "grok", "antigravity", "workbuddy"]
    grok = next(item for item in providers if item["id"] == "grok")
    assert grok["video"] is True and grok["ready"] is True
    antigravity = next(item for item in providers if item["id"] == "antigravity")
    assert antigravity["ready"] is True
    assert client.post("/v1/grok/chat/completions").status_code == 404
    assert client.post("/v1/chatgpt/chat/completions").status_code == 404
    assert client.post("/v1/codebuddy/chat/completions").status_code == 404


def test_admin_page_honors_reverse_proxy_root_path(client):
    from app.main import app

    original_root_path = app.root_path
    app.root_path = "/maliang"
    try:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 200
        assert "MinKing" in root.text
        assert 'content="/maliang"' in root.text or "/maliang/static/site.css" in root.text

        page = client.get("/admin")
        assert 'content="/maliang"' in page.text
        assert f'href="/maliang/static/app.css?v={__version__}"' in page.text
        assert f'href="/maliang/static/app-features.css?v={__version__}"' in page.text
        assert f'src="/maliang/static/app.js?v={__version__}"' in page.text
        assert page.headers.get("cache-control") == "no-store"
        assert 'id="key-form"' in page.text
        assert 'id="key-provider-routes"' not in page.text
        assert 'id="key-primary-account"' not in page.text
        assert "switch-field" in page.text

        login = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        assert "Path=/maliang/admin" in login.headers["set-cookie"]
    finally:
        app.root_path = original_root_path


def test_admin_enable_switches_use_dedicated_column(client):
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )
    css = (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "app-features.css").read_text(
        encoding="utf-8"
    )
    assert js.count('"是否开启"') >= 2
    assert '[(row)=>chip(row.status),"状态"]' in js
    assert "], rows);" in js
    assert "], state.keys);" in js
    assert "pointer-events: none" not in css.split(".switch input", 1)[1].split(".switch-track", 1)[0]


def test_admin_requires_password_session_and_rejects_legacy_token(client):
    client.cookies.clear()
    response = client.get("/admin/api/accounts", headers={"X-Admin-Token": "test-admin"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "admin_auth_required"
    login = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert login.status_code == 200


def test_gateway_errors_are_logged_without_client_credentials(client, caplog):
    raw_key = "sk-ts-this-must-not-appear-in-logs"
    caplog.set_level(logging.WARNING, logger="transfer_station.errors")
    response = client.get("/v1/models", headers=key_headers(raw_key))
    assert response.status_code == 401
    assert "path=/v1/models" in caplog.text
    assert "code=invalid_api_key" in caplog.text
    assert raw_key not in caplog.text
    events = client.get("/admin/api/errors", headers=ADMIN_HEADERS).json()
    assert events["data"][0]["code"] == "invalid_api_key"
    assert raw_key.encode() not in (settings.data_dir / "gateway.sqlite3").read_bytes()


def test_codex_dual_auth_headers_prefer_bearer_and_fall_back_to_x_api_key(client):
    body = import_pool(client, auth_payload("acct-codex-dual-auth"))
    key = body["generated_api_key"]["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-codex-dual-auth"),
            )
        )
    )

    bearer_wins = client.post(
        "/v1/responses",
        json={"input": "bearer wins"},
        headers={"Authorization": f"Bearer {key}", "X-API-Key": "unrelated-codex-header"},
    )
    fallback_works = client.post(
        "/v1/responses",
        json={"input": "x-api-key fallback"},
        headers={"Authorization": "Bearer unrelated-bearer", "X-API-Key": key},
    )

    assert bearer_wins.status_code == 200
    assert fallback_works.status_code == 200


def test_error_log_attributes_authenticated_and_disabled_users(client):
    body = import_pool(client, auth_payload("acct-error-user"))
    key_info = body["generated_api_key"]
    key = key_info["key"]

    malformed = client.post(
        "/v1/responses",
        content="not-json",
        headers={**key_headers(key), "Content-Type": "application/json"},
    )
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "invalid_json"

    disabled = client.patch(
        f"/admin/api/keys/{key_info['id']}",
        json={"enabled": False},
        headers=ADMIN_HEADERS,
    )
    assert disabled.status_code == 200
    rejected = client.get("/v1/models", headers=key_headers(key))
    assert rejected.status_code == 401
    assert rejected.json()["error"]["code"] == "invalid_api_key"
    assert key.encode() not in (settings.data_dir / "gateway.sqlite3").read_bytes()


def test_recent_errors_use_a_fixed_size_circular_buffer(client, monkeypatch):
    del monkeypatch
    body = client.get("/admin/api/errors?limit=50", headers=ADMIN_HEADERS).json()
    assert "data" in body


def test_batch_import_partial_failure_update_and_no_token_leak(client):
    original = auth_payload("acct-1", "first")
    response = client.post(
        "/admin/api/accounts/import",
        files=[
            ("files[]", ("one.json", json.dumps(original), "application/json")),
            ("files[]", ("bad.json", "not json", "application/json")),
        ],
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["created"]) == 1 and len(body["failed"]) == 1
    assert body["generated_api_key"] is None
    key = client.post(
        "/admin/api/keys",
        json={"name": "default", "preferred_account_id": "acct-1"},
        headers=ADMIN_HEADERS,
    ).json()["key"]
    dumped = json.dumps(body)
    assert "refresh-first" not in dumped
    assert key.startswith("sk-ts-")

    updated_response = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("one.json", json.dumps(auth_payload("acct-1", "second")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    updated = updated_response.json()
    assert not updated["created"] and len(updated["updated"]) == 1
    assert updated["generated_api_key"] is None
    assert len(client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]) == 1


def test_import_limits_and_rejects_non_codex_account(client, monkeypatch):
    monkeypatch.setattr(settings, "gateway_max_auth_file_bytes", 16)
    too_large = client.post(
        "/admin/api/accounts/import", files={"files[]": ("auth.json", "x" * 17)}, headers=ADMIN_HEADERS
    )
    assert too_large.status_code == 200
    assert "200KB" in too_large.json()["failed"][0]["error"]
    monkeypatch.setattr(settings, "gateway_max_import_files", 1)
    too_many = client.post(
        "/admin/api/accounts/import",
        files=[("files[]", ("a.json", "{}")), ("files[]", ("b.json", "{}"))],
        headers=ADMIN_HEADERS,
    )
    assert too_many.status_code == 413
    assert too_many.json()["error"]["code"] == "too_many_files"


def test_models_require_generated_client_key(client):
    body = import_pool(client, auth_payload("acct-1"))
    denied = client.get("/v1/models")
    assert denied.status_code == 401
    allowed = client.get("/v1/models", headers=key_headers(body["generated_api_key"]["key"]))
    assert allowed.status_code == 200
    assert allowed.json()["object"] == "list"
    assert all(item["object"] == "model" for item in allowed.json()["data"])
    catalog = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    ids = {item["id"] for item in catalog}
    assert {
        "gpt-6-astra",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
        "gpt-5.6-sol",
        "gpt-image-2",
        "grok-4.6",
        "grok-imagine-image-2.0",
        "grok-imagine-video-1.5",
    }.issubset(ids)
    image = next(item for item in catalog if item["id"] == "gpt-image-2.5-flare")
    assert image["type"] == "image" and image["default"] is True
    compat = next(item for item in catalog if item["id"] == "gpt-image-2")
    assert compat["type"] == "image" and compat["capabilities"] == ["图片生成", "图片编辑"]
    astra = next(item for item in catalog if item["id"] == "gpt-6-astra")
    assert astra["available"] is True and astra["provider"] == "codex"
    grok = next(item for item in catalog if item["id"] == "grok-4.6")
    assert grok["available"] is False and grok["provider"] == "grok"


def test_api_key_dynamic_routing_and_session_distribution(client):
    body = import_pool(client, auth_payload("acct-a", "a"), auth_payload("acct-b", "b"))
    key = body["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["ChatGPT-Account-Id"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(f"resp-{len(seen)}"))

    set_http_transport(httpx.MockTransport(handler))
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    for _ in range(3):
        assert client.post("/v1/chat/completions", json=payload, headers=key_headers(key)).status_code == 200
    assert len(set(seen)) == 2

    seen.clear()
    for number in range(24):
        headers = key_headers(key, **{"X-Session-ID": f"session-{number}"})
        assert client.post("/v1/chat/completions", json=payload, headers=headers).status_code == 200
    assert len(set(seen)) == 2
    assert abs(seen.count("acct-a") - seen.count("acct-b")) <= 2
    assert set(seen) <= {"acct-a", "acct-b"}


def test_previous_response_returns_to_original_account(client):
    body = import_pool(client, auth_payload("acct-a", "a"), auth_payload("acct-b", "b"))
    key = body["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["ChatGPT-Account-Id"])
        response_id = "resp-origin" if len(seen) == 1 else "resp-next"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(response_id))

    set_http_transport(httpx.MockTransport(handler))
    headers = key_headers(key, **{"X-Session-ID": "thread-a"})
    first = client.post("/v1/responses", json={"model": "gpt-5.6-sol", "input": "hi"}, headers=headers)
    assert first.status_code == 200 and first.json()["id"] == "resp-origin"
    second = client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": "again", "previous_response_id": "resp-origin"},
        headers=key_headers(key),
    )
    assert second.status_code == 200
    unknown = client.post(
        "/v1/responses",
        json={"input": "again", "previous_response_id": "unknown-response"},
        headers=key_headers(key),
    )
    assert unknown.status_code == 409
    assert unknown.json()["error"]["code"] == "continuation_unavailable"


def test_response_binding_unavailable_is_409_but_full_history_chat_rebinds(client):
    body = import_pool(client, auth_payload("acct-a", "a"), auth_payload("acct-b", "b"))
    key = body["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["ChatGPT-Account-Id"])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse("resp-bound"))

    set_http_transport(httpx.MockTransport(handler))
    session_headers = key_headers(key, **{"X-Session-ID": "sticky"})
    assert client.post("/v1/responses", json={"input": "one"}, headers=session_headers).status_code == 200
    original = seen[-1]
    assert client.patch(f"/admin/api/accounts/{original}", json={"enabled": False}, headers=ADMIN_HEADERS).status_code == 200
    unavailable = client.post("/v1/responses", json={"input": "two", "previous_response_id": "resp-bound"}, headers=key_headers(key))
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "continuation_unavailable"
    assert seen[-1] == original  # No request sent to an account without the history.

    chat = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "x"}]}, headers=session_headers)
    assert chat.status_code == 200
    assert seen[-1] != original


def test_stream_usage_and_binding_are_recorded(client):
    body = import_pool(client, auth_payload("acct-usage"))
    key = body["generated_api_key"]["key"]
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        content=sse("resp-stream", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}),
    )))
    response = client.post("/v1/responses", json={"input": "hello", "stream": True}, headers=key_headers(key))
    assert response.status_code == 200 and "response.completed" in response.text
    usage = client.get("/admin/api/usage", headers=ADMIN_HEADERS).json()
    assert usage["data"][0]["total_tokens"] == 8
    assert client.get("/admin/api/bindings", headers=ADMIN_HEADERS).status_code == 404


def test_fast_mode_crud_overrides_text_tier_and_survives_rotation_and_route(client):
    imported = import_pool(client, auth_payload("acct-fast-a"), auth_payload("acct-fast-b"))
    default_key = imported["generated_api_key"]
    assert default_key["fast_enabled"] is False
    fast_key = client.post(
        "/admin/api/keys",
        json={
            "name": "Fast User",
            "preferred_account_id": "acct-fast-a",
            "fast_enabled": True,
        },
        headers=ADMIN_HEADERS,
    ).json()
    assert fast_key["fast_enabled"] is True
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(f"resp-fast-{len(seen)}"),
        )

    set_http_transport(httpx.MockTransport(handler))
    assert client.post(
        "/v1/responses",
        json={"input": "fast response", "service_tier": "default"},
        headers=key_headers(fast_key["key"]),
    ).status_code == 200
    assert client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "fast chat"}],
            "service_tier": "auto",
        },
        headers=key_headers(fast_key["key"]),
    ).status_code == 200
    assert [payload["service_tier"] for payload in seen] == ["priority", "priority"]

    assert client.patch(
        f"/admin/api/keys/{fast_key['id']}",
        json={"fast_enabled": False},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    assert client.post(
        "/v1/responses",
        json={"input": "normal response", "service_tier": "default"},
        headers=key_headers(fast_key["key"]),
    ).status_code == 200
    assert seen[-1]["service_tier"] == "default"

    assert client.patch(
        f"/admin/api/keys/{fast_key['id']}",
        json={"fast_enabled": True},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    assert client.put(
        f"/admin/api/keys/{fast_key['id']}/route",
        json={"preferred_account_id": "acct-fast-b"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    rotated = client.post(
        f"/admin/api/keys/{fast_key['id']}/rotate", headers=ADMIN_HEADERS
    ).json()
    listed = {
        row["id"]: row for row in client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"]
    }
    assert listed[fast_key["id"]]["fast_enabled"] is True
    assert rotated["id"] == fast_key["id"]
    assert client.patch(
        f"/admin/api/keys/{fast_key['id']}", json={}, headers=ADMIN_HEADERS
    ).status_code == 422


def test_fast_mode_remains_priority_during_account_failover(client, monkeypatch):
    import_pool(client, auth_payload("acct-fast-primary"), auth_payload("acct-fast-backup"))
    fast_key = client.post(
        "/admin/api/keys",
        json={
            "name": "Failover Fast",
            "preferred_account_id": "acct-fast-primary",
            "fast_enabled": True,
        },
        headers=ADMIN_HEADERS,
    ).json()
    seen: list[tuple[str, str | None]] = []

    async def no_pause(_delay_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((request.headers["ChatGPT-Account-Id"], payload.get("service_tier")))
        if len(seen) == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-fast-failover"),
        )

    monkeypatch.setattr(codex_gateway_module, "_retry_pause", no_pause)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "retry"}]},
        headers=key_headers(fast_key["key"]),
    )
    assert response.status_code == 200
    assert len(seen) == 2 and len({account for account, _tier in seen}) == 1
    assert all(tier == "priority" for _account, tier in seen)


def test_fast_mode_does_not_inject_tier_into_direct_image_protocol(client):
    import_pool(client, auth_payload("acct-fast-image"))
    fast_key = client.post(
        "/admin/api/keys",
        json={
            "name": "Fast Image",
            "preferred_account_id": "acct-fast-image",
            "fast_enabled": True,
        },
        headers=ADMIN_HEADERS,
    ).json()
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/generations",
        json={"prompt": "cat"},
        headers=key_headers(fast_key["key"]),
    )
    assert response.status_code == 200
    assert "service_tier" not in seen[0]


def test_usage_ranges_follow_shanghai_natural_dates_and_match_detail(client):
    import_pool(client, auth_payload("acct-range"))
    assert client.get("/admin/api/usage/summary?range=day", headers=ADMIN_HEADERS).status_code == 200
    assert client.get("/admin/api/usage?range=day", headers=ADMIN_HEADERS).status_code == 200
    assert client.get("/admin/api/calls", headers=ADMIN_HEADERS).status_code == 200
    assert client.get("/admin/api/dashboard", headers=ADMIN_HEADERS).status_code == 200


def test_image_generation_and_multipart_edit_passthrough_without_files(client):
    body = import_pool(client, auth_payload("acct-image"))
    key = body["generated_api_key"]["key"]
    seen: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.content))
        return httpx.Response(200, json={"created": 1, "data": [{"url": "https://example.test/image.png"}]})

    set_http_transport(httpx.MockTransport(handler))
    generated = client.post("/v1/images/generations", json={"prompt": "cat"}, headers=key_headers(key))
    assert generated.json()["data"][0]["url"].startswith("https://")
    edited = client.post(
        "/v1/images/edits", data={"prompt": "dark"}, files={"image": ("tiny.png", b"pngbytes", "image/png")},
        headers=key_headers(key),
    )
    assert edited.status_code == 200
    assert seen[0][0].endswith("/images/generations") and seen[1][0].endswith("/images/edits")
    assert not (settings.data_dir / "media").exists()


def test_key_revocation_and_storage_does_not_capture_content(client):
    imported = import_pool(client, auth_payload("acct-safe", "credential-secret"))
    key_info = imported["generated_api_key"]
    raw_key = key_info["key"]
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse("resp-secret", "private-reply")
    )))
    session = "private-session-id"
    prompt = "private-prompt"
    response = client.post(
        "/v1/chat/completions", json={"messages": [{"role": "user", "content": prompt}]},
        headers=key_headers(raw_key, **{"X-Session-ID": session}),
    )
    assert response.status_code == 200
    listed = client.get("/admin/api/keys", headers=ADMIN_HEADERS)
    assert listed.headers["cache-control"] == "no-store"
    visible = next(row for row in listed.json()["data"] if row["id"] == key_info["id"])
    assert visible["key"] == raw_key
    assert visible["recoverable"] is True
    db = (settings.data_dir / "gateway.sqlite3").read_bytes()
    for secret in (raw_key, session, prompt, "private-reply", "resp-secret", "credential-secret"):
        assert secret.encode() not in db
    revoked = client.delete(f"/admin/api/keys/{key_info['id']}", headers=ADMIN_HEADERS)
    assert revoked.status_code == 200
    assert client.get("/v1/models", headers=key_headers(raw_key)).status_code == 401


def test_legacy_key_can_be_rotated_to_a_visible_encrypted_key(client):
    imported = import_pool(client, auth_payload("acct-rotate"))
    key_info = imported["generated_api_key"]
    old_key = key_info["key"]
    from app.store.gateway import gateway_store

    client.portal.call(
        gateway_store.execute,
        "UPDATE api_keys SET key_ciphertext=NULL WHERE id=?",
        (key_info["id"],),
    )
    legacy = next(
        row
        for row in client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"]
        if row["id"] == key_info["id"]
    )
    assert legacy["key"] is None and legacy["recoverable"] is False

    rotated = client.post(
        f"/admin/api/keys/{key_info['id']}/rotate", headers=ADMIN_HEADERS
    )
    assert rotated.status_code == 200
    new_key = rotated.json()["key"]
    assert new_key != old_key
    assert client.get("/v1/models", headers=key_headers(old_key)).status_code == 401
    assert client.get("/v1/models", headers=key_headers(new_key)).status_code == 200
    db = (settings.data_dir / "gateway.sqlite3").read_bytes()
    assert new_key.encode() not in db


def test_upstream_401_marks_account_invalid(client):
    body = import_pool(client, auth_payload("acct-invalid"))
    key = body["generated_api_key"]["key"]
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"message": "expired"}})))
    response = client.post("/v1/responses", json={"input": "hello"}, headers=key_headers(key))
    assert response.status_code == 401
    accounts = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert accounts[0]["status"] == "invalid"


def test_chat_preserves_image_url_and_tools(client):
    body = import_pool(client, auth_payload("acct-vision"))
    key = body["generated_api_key"]["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse("resp-vision"))

    set_http_transport(httpx.MockTransport(handler))
    assert client.put(
        "/admin/api/billing/prices/gpt-custom",
        json={"provider": "codex", "modality": "text", "official_input_usd_per_1m": "1", "official_output_usd_per_1m": "2"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-custom",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "https://example.test/cat.png"}},
            ]}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200
    dumped = json.dumps(upstream[0])
    assert "input_image" in dumped and "https://example.test/cat.png" in dumped
    assert upstream[0]["tools"][0]["name"] == "lookup"
    assert "function" not in upstream[0]["tools"][0]


def test_lite_model_flattens_chat_function_tools_for_additional_tools(client):
    body = import_pool(client, auth_payload("acct-tools"))
    key = body["generated_api_key"]["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-tools"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-5.6-sol",
            "messages": [{"role": "user", "content": "use lookup"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup a value",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200
    additional = upstream[0]["input"][0]
    assert additional["type"] == "additional_tools"
    assert additional["tools"][0]["name"] == "lookup"
    assert "function" not in additional["tools"][0]
    assert upstream[0]["tool_choice"] == {"type": "function", "name": "lookup"}


def test_image_retries_once_on_same_account_until_second_failure(client):
    body = import_pool(client, auth_payload("acct-one"), auth_payload("acct-two"))
    key = body["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["ChatGPT-Account-Id"])
        if len(seen) == 1:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post("/v1/images/generations", json={"prompt": "cat"}, headers=key_headers(key))
    assert response.status_code == 200
    assert len(seen) == 2 and seen[0] == seen[1]


def test_files_api_keeps_inline_image_out_of_client_context(client):
    body = import_pool(client, auth_payload("acct-file"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"small-image"
    created = client.post(
        "/v1/files",
        data={"purpose": "user_data"},
        files={"file": ("screen.png", png, "image/png")},
        headers=key_headers(key),
    )
    assert created.status_code == 200, created.text
    file_id = created.json()["id"]
    assert file_id.startswith("file_")

    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-file"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "file_id": file_id}],
            }]
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200
    dumped = json.dumps(upstream[0])
    assert file_id not in dumped
    assert "data:image/png;base64," in dumped
    assert client.get(f"/v1/files/{file_id}", headers=key_headers(key)).status_code == 200
    assert client.get(f"/v1/files/{file_id}/content", headers=key_headers(key)).content == png


def test_file_id_resolver_ignores_json_schema_type_objects(client):
    body = import_pool(client, auth_payload("acct-schema-type"))
    key = body["generated_api_key"]["key"]
    tools = [{
        "type": "function",
        "name": "classify",
        "parameters": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "category"},
                "items": {"type": ["string", "null"]},
            },
        },
    }]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-schema-type"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "gpt-6-astra",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "tools": tools,
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    chat = client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-6-astra",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": tools,
        },
        headers=key_headers(key),
    )
    assert chat.status_code == 200, chat.text
    assert len(seen) == 2


def test_image_url_mode_is_opt_in_and_signed_download_works(client):
    body = import_pool(client, auth_payload("acct-image-url"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"generated"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": encoded}], "output_format": "png"}
    )))

    official = client.post(
        "/v1/images/generations", json={"prompt": "cat"}, headers=key_headers(key)
    )
    assert official.status_code == 200
    assert official.json()["data"][0]["b64_json"] == encoded

    compact = client.post(
        "/v1/images/generations",
        json={"prompt": "cat", "response_format": "url"},
        headers=key_headers(key),
    )
    assert compact.status_code == 200, compact.text
    item = compact.json()["data"][0]
    assert "b64_json" not in item
    downloaded = client.get(item["url"])
    assert downloaded.status_code == 200
    assert downloaded.content == png


def test_chat_tool_call_round_trip_uses_official_shapes(client):
    body = import_pool(client, auth_payload("acct-tool-roundtrip"))
    key = body["generated_api_key"]["key"]
    upstream: list[dict] = []

    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": "resp-tool", "model": "gpt-5.6-sol"}}),
        ("response.output_item.added", {"type": "response.output_item.added", "item": {"id": "item-1", "type": "function_call", "call_id": "call-next", "name": "lookup", "arguments": ""}}),
        ("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": "item-1", "delta": "{\"q\":\"next\"}"}),
        ("response.output_item.done", {"type": "response.output_item.done", "item": {"id": "item-1", "type": "function_call", "call_id": "call-next", "name": "lookup", "arguments": "{\"q\":\"next\"}"}}),
        ("response.completed", {"type": "response.completed", "response": {"id": "resp-tool", "model": "gpt-5.6-sol", "output": [{"id": "item-1", "type": "function_call", "call_id": "call-next", "name": "lookup", "arguments": "{\"q\":\"next\"}"}], "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}}}),
    ]
    stream = "".join(
        f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-openai-internal-codex-responses-lite"] == "true"
        upstream.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "look it up"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-old", "type": "function",
                    "function": {"name": "lookup", "arguments": "{\"q\":\"old\"}"},
                }]},
                {"role": "tool", "tool_call_id": "call-old", "content": "old result"},
            ],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    kinds = [item["type"] for item in upstream[0]["input"]]
    assert "function_call" in kinds and "function_call_output" in kinds
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["arguments"] == "{\"q\":\"next\"}"
    assert response.json()["usage"] == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}


def test_lite_header_matches_every_advertised_model_and_is_omitted_otherwise(client):
    body = import_pool(client, auth_payload("acct-lite-header-matrix"))
    key = body["generated_api_key"]["key"]
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((payload["model"], request.headers.get("x-openai-internal-codex-responses-lite")))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(f"resp-{len(seen)}"),
        )

    set_http_transport(httpx.MockTransport(handler))
    assert client.put(
        "/admin/api/billing/prices/gpt-4.1",
        json={"provider": "codex", "modality": "text", "official_input_usd_per_1m": "2", "official_output_usd_per_1m": "8"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    lite_models = [
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-6-luna-fast",
        "gpt-5.6-sol",
        "gpt-5.6-sol-wm",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.6-luna-fast",
        "codex-auto-review",
    ]
    for model in [*lite_models, "gpt-4.1"]:
        response = client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "header"}]},
            headers=key_headers(key),
        )
        assert response.status_code == 200

    assert seen == [(model, "true") for model in lite_models] + [("gpt-4.1", None)]


def test_lite_models_accept_but_do_not_forward_unsupported_output_token_limits(client):
    body = import_pool(client, auth_payload("acct-lite-token-limit"))
    key = body["generated_api_key"]["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(f"resp-{len(upstream)}"),
        )

    set_http_transport(httpx.MockTransport(handler))
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        request_body = {
            "model": "gpt-5.6-luna",
            "messages": [{"role": "user", "content": "token limit"}],
            field: 1024,
        }
        response = client.post(
            "/v1/chat/completions" if field != "max_output_tokens" else "/v1/responses",
            json=request_body if field != "max_output_tokens" else {
                "model": "gpt-5.6-luna",
                "input": "token limit",
                field: 1024,
            },
            headers=key_headers(key),
        )
        assert response.status_code == 200

    assert all("max_output_tokens" not in payload for payload in upstream)


def test_non_lite_responses_model_forwards_standard_output_token_limit(client):
    body = import_pool(client, auth_payload("acct-standard-token-limit"))
    key = body["generated_api_key"]["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-standard-limit"),
        )

    set_http_transport(httpx.MockTransport(handler))
    assert client.put(
        "/admin/api/billing/prices/gpt-4.1",
        json={"provider": "codex", "modality": "text", "official_input_usd_per_1m": "2", "official_output_usd_per_1m": "8"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-4.1", "input": "token limit", "max_output_tokens": 1024},
        headers=key_headers(key),
    )
    assert response.status_code == 200
    assert upstream[0]["max_output_tokens"] == 1024


def test_immediate_sse_failure_retries_before_client_output(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-stream-retry"))
    key = body["generated_api_key"]["key"]
    attempts = 0
    delays: list[float] = []

    async def fake_pause(delay_seconds: float) -> None:
        delays.append(delay_seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            content = (
                b'event: response.created\r\ndata: {"type":"response.created","response":{"id":"failed"}}\r\n\r\n'
                b'event: response.failed\r\ndata: {"type":"response.failed","response":{"error":{"message":"capacity"}}}\r\n\r\n'
            )
        else:
            content = sse("resp-recovered", "recovered")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content)

    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "stream": True},
        headers=key_headers(key),
    )
    assert response.status_code == 200
    assert "recovered" in response.text and "capacity" not in response.text
    assert attempts == 4 and delays == [5.0, 10.0, 20.0]


def test_first_token_watchdog_fails_stream_without_retry(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-first-token-timeout"))
    key = body["generated_api_key"]["key"]
    attempts = 0

    class StalledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield sse("resp-too-late")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=StalledStream(),
        )

    monkeypatch.setattr(settings, "gateway_first_token_timeout_seconds", 0.01)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "stream": True},
        headers=key_headers(key),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "response.failed" in response.text
    assert "server_error" in response.text
    assert attempts == 1


def test_pool_timeout_rotates_pool_and_retries(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-pool-timeout"))
    key = body["generated_api_key"]["key"]
    attempts = 0
    rotations: list[object] = []

    class RecoveringClient:
        def build_request(self, method: str, url: str, **kwargs) -> httpx.Request:
            timeout = kwargs.pop("timeout")
            request = httpx.Request(method, url, **kwargs)
            request.extensions["timeout"] = timeout.as_dict()
            return request

        async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.PoolTimeout("pool unavailable", request=request)
            return httpx.Response(
                200,
                request=request,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-pool-recovered", "recovered"),
            )

    recovering_client = RecoveringClient()

    async def fake_shared_client():
        return recovering_client

    async def fake_rotate(expected_client, *, reason: str):
        rotations.append((expected_client, reason))
        return True

    async def fake_pause(_delay_seconds: float) -> None:
        return None

    monkeypatch.setattr(codex_gateway_module, "shared_client", fake_shared_client)
    monkeypatch.setattr(codex_gateway_module, "rotate_shared_client", fake_rotate)
    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)

    response = client.post(
        "/v1/responses",
        json={"input": "hello", "stream": True},
        headers=key_headers(key),
    )

    assert response.status_code == 200
    assert "recovered" in response.text
    assert attempts == 2
    assert rotations == [(recovering_client, "pool_timeout")]


def test_first_token_watchdog_includes_response_headers(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-header-timeout"))
    key = body["generated_api_key"]["key"]

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-header-too-late"),
        )

    monkeypatch.setattr(settings, "gateway_first_token_timeout_seconds", 0.01)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"input": "hello"},
        headers=key_headers(key),
    )

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_first_token_timeout"


def test_reasoning_delta_satisfies_first_token_watchdog(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-reasoning-first-token"))
    key = body["generated_api_key"]["key"]
    created = (
        'event: response.created\n'
        'data: {"type":"response.created","response":{"id":"resp-reasoning"}}\n\n'
    ).encode()
    reasoning = (
        'event: response.reasoning_summary_text.delta\n'
        'data: {"type":"response.reasoning_summary_text.delta","delta":"thinking"}\n\n'
    ).encode()

    class ReasoningFirstStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield created + reasoning
            await asyncio.sleep(0.15)
            yield sse("resp-reasoning", "done")

    monkeypatch.setattr(settings, "gateway_first_token_timeout_seconds", 0.1)
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=ReasoningFirstStream(),
    )))
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "stream": True},
        headers=key_headers(key),
    )

    assert response.status_code == 200
    assert "response.reasoning_summary_text.delta" in response.text
    assert "done" in response.text


def _image_generation_sse(encoded: str, response_id: str = "resp-image") -> bytes:
    blocks = [
        ("response.created", {"type": "response.created", "response": {"id": response_id, "model": "gpt-5.6-sol"}}),
        ("response.image_generation_call.partial_image", {"type": "response.image_generation_call.partial_image", "partial_image_b64": encoded}),
        ("response.output_item.done", {"type": "response.output_item.done", "item": {"id": "img-1", "type": "image_generation_call", "result": encoded}}),
        ("response.completed", {"type": "response.completed", "response": {"id": response_id, "model": "gpt-5.6-sol", "output": [{"id": "img-1", "type": "image_generation_call", "result": encoded}], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}}),
    ]
    return "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks).encode()


def _images_json_or_sse(encoded: str, response_id: str = "resp-image"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations") or request.url.path.endswith("/images/edits"):
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_image_generation_sse(encoded, response_id),
        )

    return handler


def test_responses_compact_image_stream_suppresses_base64(client):
    body = import_pool(client, auth_payload("acct-compact-stream"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"streamed"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(_images_json_or_sse(encoded)))
    response = client.post(
        "/v1/responses",
        json={"input": "draw", "stream": True, "tools": [{"type": "image_generation"}]},
        headers=key_headers(key, **{"X-TS-Image-Mode": "url"}),
    )
    assert response.status_code == 200
    assert encoded not in response.text
    assert "partial_image_b64" not in response.text
    assert "/v1/files/file_" in response.text


def test_codex_ua_defaults_responses_images_to_urls(client):
    body = import_pool(client, auth_payload("acct-codex-ua-url"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"codex-default"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(_images_json_or_sse(encoded, "resp-codex-ua")))
    streamed = client.post(
        "/v1/responses",
        json={"input": "draw", "stream": True, "tools": [{"type": "image_generation"}]},
        headers=key_headers(key, **{"User-Agent": "codex_exec/0.155.0-alpha.2.6"}),
    )
    assert streamed.status_code == 200
    assert encoded not in streamed.text
    assert "/v1/files/file_" in streamed.text

    collected = client.post(
        "/v1/responses",
        json={"input": "draw", "tools": [{"type": "image_generation"}]},
        headers=key_headers(key, **{"User-Agent": "codex_cli_rs/0.154.0"}),
    )
    assert collected.status_code == 200, collected.text
    result = collected.json()["output"][0]["result"]
    assert encoded not in result
    assert result.startswith("http")
    assert "/v1/files/file_" in result


def test_codex_ua_image_url_mode_can_keep_base64(client):
    body = import_pool(client, auth_payload("acct-codex-ua-b64"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"keep-b64"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(_images_json_or_sse(encoded, "resp-keep-b64")))
    response = client.post(
        "/v1/responses",
        json={"input": "draw", "stream": True, "tools": [{"type": "image_generation"}]},
        headers=key_headers(
            key,
            **{"User-Agent": "codex_exec/0.155.0-alpha.2.6", "X-TS-Image-Mode": "b64"},
        ),
    )
    assert response.status_code == 200
    assert encoded in response.text


def test_sdk_responses_images_stay_base64_without_codex_ua(client):
    body = import_pool(client, auth_payload("acct-sdk-b64"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"sdk-b64"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(_images_json_or_sse(encoded, "resp-sdk")))
    response = client.post(
        "/v1/responses",
        json={"input": "draw", "stream": True, "tools": [{"type": "image_generation"}]},
        headers=key_headers(key, **{"User-Agent": "openai-python/1.40.0"}),
    )
    assert response.status_code == 200
    assert encoded in response.text
    assert "/v1/files/file_" not in response.text


def test_retry_after_is_propagated_on_final_rate_limit(client, monkeypatch):
    body = import_pool(client, auth_payload("acct-rate-limit"))
    key = body["generated_api_key"]["key"]

    async def fake_pause(_delay_seconds: float) -> None:
        return None

    monkeypatch.setattr(codex_gateway_module, "_retry_pause", fake_pause)
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        429,
        headers={"Retry-After": "17"},
        json={"error": {"message": "rate limited"}},
    )))
    response = client.post(
        "/v1/responses", json={"input": "hello"}, headers=key_headers(key)
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"


def test_chatgpt_auth_image_endpoint_falls_back_to_responses_tool(client):
    body = import_pool(client, auth_payload("acct-image-tool"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"tool-image"
    encoded = base64.b64encode(png).decode()
    seen: list[str] = []
    tool_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        tool_requests.append(json.loads(request.content))
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp-image-tool",
                "model": "gpt-5.6-sol",
                "output": [{"id": "img-1", "type": "image_generation_call", "result": encoded}],
                "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: response.completed\ndata: {json.dumps(completed)}\n\n".encode(),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-2", "prompt": "draw", "quality": "high"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == encoded
    assert seen[-1].endswith("/responses")
    assert tool_requests[0]["tools"][0] == {
        "type": "image_generation",
        "action": "generate",
        "model": "gpt-image-2",
        "quality": "high",
    }


def test_image_edit_fallback_preserves_multiple_images_and_mask(client):
    body = import_pool(client, auth_payload("acct-image-edit-tool"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"edit-image"
    encoded = base64.b64encode(png).decode()
    tool_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/edits"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        tool_requests.append(json.loads(request.content))
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp-image-edit",
                "model": "gpt-5.6-sol",
                "output": [{"id": "img-1", "type": "image_generation_call", "result": encoded}],
            },
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: response.completed\ndata: {json.dumps(completed)}\n\n".encode(),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/edits",
        data={"prompt": "edit", "input_fidelity": "high"},
        files=[
            ("image[]", ("one.png", png, "image/png")),
            ("image[]", ("two.png", png, "image/png")),
            ("mask", ("mask.png", png, "image/png")),
        ],
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    payload = tool_requests[0]
    image_blocks = payload["input"][0]["content"][1:]
    assert len(image_blocks) == 2
    tool = payload["tools"][0]
    assert tool["action"] == "edit" and tool["input_fidelity"] == "high"
    assert tool["input_image_mask"]["image_url"].startswith("data:image/png;base64,")


def test_image_edit_fallback_reads_result_from_output_item_done(client):
    body = import_pool(client, auth_payload("acct-image-edit-stream-item"))
    key = body["generated_api_key"]["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"stream-item-image"
    encoded = base64.b64encode(png).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/edits"):
            return httpx.Response(404, json={"error": {"message": "not found"}})
        events = [
            (
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "img-stream-item",
                        "type": "image_generation_call",
                        "result": encoded,
                    },
                },
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-image-edit-stream-item",
                        "output": [
                            {"id": "img-stream-item", "type": "image_generation_call"}
                        ],
                    },
                },
            ),
        ]
        content = "".join(
            f"event: {event}\ndata: {json.dumps(payload)}\n\n"
            for event, payload in events
        ).encode()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/edits",
        data={"prompt": "edit"},
        files={"image": ("source.png", png, "image/png")},
        headers=key_headers(key),
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["b64_json"] == encoded


def test_image_result_uses_last_partial_when_final_event_omits_result():
    events = [
        (
            "response.image_generation_call.partial_image",
            json.dumps({
                "type": "response.image_generation_call.partial_image",
                "partial_image_b64": "first",
            }),
        ),
        (
            "response.image_generation_call.partial_image",
            json.dumps({
                "type": "response.image_generation_call.partial_image",
                "partial_image_b64": "last",
            }),
        ),
    ]

    result = codex_gateway_module._image_results_from_sse(
        events,
        {"output": [{"type": "image_generation_call"}]},
    )

    assert result == [{"b64_json": "last"}]


def test_files_are_isolated_by_client_key(client):
    import_pool(client, auth_payload("acct-file-isolation"))
    first = client.post("/admin/api/keys", json={"name": "first", "preferred_account_id": "acct-file-isolation"}, headers=ADMIN_HEADERS).json()
    second = client.post("/admin/api/keys", json={"name": "second", "preferred_account_id": "acct-file-isolation"}, headers=ADMIN_HEADERS).json()
    png = b"\x89PNG\r\n\x1a\n" + b"private"
    created = client.post(
        "/v1/files",
        files={"file": ("private.png", png, "image/png")},
        data={"purpose": "user_data"},
        headers=key_headers(first["key"]),
    )
    file_id = created.json()["id"]
    assert client.get(f"/v1/files/{file_id}", headers=key_headers(first["key"])).status_code == 200
    denied = client.get(f"/v1/files/{file_id}", headers=key_headers(second["key"]))
    assert denied.status_code == 404


def test_stamp_sse_created_at_fills_missing_field():
    from app.providers.codex_protocol import stamp_sse_created_at

    raw = 'event: response.created\ndata: {"type":"response.created","response":{"id":"resp-1","object":"response","status":"in_progress"}}'
    stamped = stamp_sse_created_at(raw, created_at=1700000000)
    payload = json.loads(stamped.split("data: ", 1)[1])
    assert payload["response"]["created_at"] == 1700000000
    already = stamp_sse_created_at(
        'event: response.created\ndata: {"type":"response.created","response":{"id":"resp-1","created_at":99}}',
        created_at=1700000000,
    )
    assert '"created_at": 99' in already or '"created_at":99' in already


def test_responses_stream_adds_created_at_for_grok_clients(client):
    body = import_pool(client, auth_payload("acct-created-at"))
    key = body["generated_api_key"]["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-created-at", "hello"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={"model": "gpt-5.6-sol", "input": "hi", "stream": True},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    created_fields = []
    for raw in response.text.split("\n\n"):
        if "response.created" not in raw and "response.completed" not in raw:
            continue
        data = raw.split("data: ", 1)[1]
        payload = json.loads(data)
        nested = payload.get("response")
        if isinstance(nested, dict):
            created_fields.append(nested.get("created_at"))
    assert created_fields
    assert all(isinstance(item, int) and item > 0 for item in created_fields)
