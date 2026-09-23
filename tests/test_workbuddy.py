from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

import httpx

from app.http_client import set_http_transport
from app.providers.workbuddy import (
    WorkBuddyAdapter,
    load_credentials,
    prepare_chat_payload,
    responses_to_chat,
)
from conftest import ADMIN_HEADERS


def _auth(*, expires_at: int = 4_070_000_000) -> dict:
    return {
        "account": {"uid": "wb-user-1", "nickname": "WorkBuddy test"},
        "auth": {
            "accessToken": "wb-access-test", "refreshToken": "wb-refresh-test",
            "expiresAt": expires_at, "domain": "copilot.tencent.com",
        },
    }


def _key(client) -> str:
    uploaded = client.post(
        "/admin/api/accounts/import",
        data={"provider": "workbuddy"},
        files={"files[]": ("workbuddy-desktop.info", json.dumps(_auth()), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["created"][0]["account_id"] == "workbuddy:cn:wb-user-1"
    key = client.post("/admin/api/keys", json={"name": "wb-test"}, headers=ADMIN_HEADERS)
    assert key.status_code == 200, key.text
    return key.json()["key"]


def _chat_sse(*, tool: bool = False) -> bytes:
    chunks = [
        {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
    ]
    if tool:
        chunks.extend([
            {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_wb", "type": "function", "function": {"name": "lookup", "arguments": ""}}]}}]},
            {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"x\":1}"}}]}}]},
            {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ])
    else:
        chunks.extend([
            {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {"content": "OK"}}]},
            {"id": "chatcmpl-wb", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])
    chunks.append({"id": "chatcmpl-wb", "choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
    return ("".join("data: " + json.dumps(part) + "\n\n" for part in chunks) + "data: [DONE]\n\n").encode()


def test_import_accepts_desktop_file_and_rejects_unrelated_json(client):
    response = client.post(
        "/admin/api/accounts/import",
        data={"provider": "workbuddy"},
        files={"files[]": ("workbuddy-desktop.info", json.dumps(_auth()), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["created"][0]["provider"] == "workbuddy"
    assert "wb-access-test" not in response.text
    bad = client.post(
        "/admin/api/accounts/import",
        data={"provider": "workbuddy"},
        files={"files[]": ("settings.json", '{}', "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert bad.status_code == 200
    assert bad.json()["failed"]


def test_workbuddy_web_login_poll_imports_account(client):
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json={"code": 0, "data": {"state": "workbuddy-state-12345", "authUrl": "https://www.codebuddy.cn/login"}})
        if request.url.path.endswith("/auth/token"):
            polls += 1
            if polls == 1:
                return httpx.Response(200, json={"code": 11217, "msg": "11217:login ing...", "data": {}})
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "wb-login-access", "refreshToken": "wb-login-refresh", "expiresIn": 3600, "domain": "copilot.tencent.com"}})
        if request.url.path.endswith("/login/account"):
            assert request.headers["authorization"] == "Bearer wb-login-access"
            return httpx.Response(200, json={"code": 0, "data": {"uid": "wb-login-user", "nickname": "Logged in"}})
        return httpx.Response(404)

    set_http_transport(httpx.MockTransport(handler))
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "workbuddy", "realm": "cn"},
        headers=ADMIN_HEADERS,
    )
    assert started.status_code == 200, started.text
    assert started.json()["login_mode"] == "poll"
    state = started.json()["state"]
    first = client.get("/admin/api/accounts/oauth/status", params={"state": state}, headers=ADMIN_HEADERS)
    assert first.json()["status"] == "pending"
    done = client.get("/admin/api/accounts/oauth/status", params={"state": state}, headers=ADMIN_HEADERS)
    assert done.status_code == 200, done.text
    assert done.json()["created"][0]["account_id"] == "workbuddy:cn:wb-login-user"
    assert "wb-login-access" not in done.text


def test_workbuddy_web_login_rejection_is_not_left_pending(client):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json={"code": 0, "data": {"state": "workbuddy-rejected-state", "authUrl": "https://www.codebuddy.cn/login"}})
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 11999, "msg": "authorization expired"})
        return httpx.Response(404)

    set_http_transport(httpx.MockTransport(handler))
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "workbuddy", "realm": "cn"},
        headers=ADMIN_HEADERS,
    )
    assert started.status_code == 200
    state = started.json()["state"]
    result = client.get("/admin/api/accounts/oauth/status", params={"state": state}, headers=ADMIN_HEADERS)
    assert result.status_code == 422
    status = client.get("/admin/api/accounts/oauth/status", params={"state": state}, headers=ADMIN_HEADERS)
    assert status.json()["status"] == "failed"


def test_workbuddy_chat_and_responses_use_workbuddy_account(client):
    key = _key(client)
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((request.url.path, json.loads(request.content), request.headers))
        return httpx.Response(200, content=_chat_sse(tool=len(captured) == 2), headers={"content-type": "text/event-stream"})

    set_http_transport(httpx.MockTransport(handler))
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "workbuddy/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert chat.status_code == 200, chat.text
    assert chat.json()["choices"][0]["message"]["content"] == "OK"
    responses = client.post(
        "/v1/responses",
        json={"model": "workbuddy/glm-5.2", "input": "run lookup", "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert responses.status_code == 200, responses.text
    output = responses.json()["output"]
    assert output[0]["type"] == "function_call"
    assert output[0]["name"] == "lookup"
    assert output[0]["arguments"] == '{"x":1}'
    assert captured[0][0] == captured[1][0] == "/v2/chat/completions"
    assert captured[0][1]["model"] == "glm-5.2"
    assert captured[1][1]["model"] == "glm-5.2"
    assert captured[0][2]["x-user-id"] == "wb-user-1"


def test_workbuddy_responses_stream_emits_text_and_tool_deltas(client):
    key = _key(client)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=_chat_sse(tool=calls == 2),
            headers={"content-type": "text/event-stream"},
        )

    set_http_transport(httpx.MockTransport(handler))
    for expected_delta in ('"delta": "OK"', '"delta": "{\\"x\\":1}"'):
        response = client.post(
            "/v1/responses",
            json={"model": "workbuddy/glm-5.2", "input": "hi", "stream": True},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert response.status_code == 200, response.text
        assert expected_delta in response.text
        assert "event: response.completed" in response.text


def test_workbuddy_sends_developer_instructions_as_system():
    chat = responses_to_chat(
        {
            "instructions": "follow the repo",
            "input": [
                {"role": "developer", "content": "use the system role"},
                {"role": "user", "content": "hi"},
            ],
        },
        model="glm-5.1",
    )
    assert [item["role"] for item in chat["messages"]] == ["system", "system", "user"]
    prepared = prepare_chat_payload(
        {"messages": [{"role": "developer", "content": "rules"}, {"role": "user", "content": "hi"}]},
        realm="cn",
    )
    assert [item["role"] for item in prepared["messages"]] == ["system", "user"]


def test_workbuddy_drops_oversized_input_images():
    huge = "data:image/png;base64," + ("A" * 400_001)
    chat = responses_to_chat(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "see this"},
                        {"type": "input_image", "image_url": huge},
                        {"type": "input_image", "image_url": "data:image/png;base64,QQ=="},
                    ],
                }
            ]
        },
        model="auto",
    )
    parts = chat["messages"][0]["content"]
    assert parts[0]["type"] == "text"
    urls = [part["image_url"]["url"] for part in parts if part.get("type") == "image_url"]
    assert urls == ["data:image/png;base64,QQ=="]


def test_workbuddy_maps_hosted_image_and_web_search_tools():
    chat = responses_to_chat(
        {
            "input": "draw then lookup",
            "tools": [
                {"type": "image_generation"},
                {"type": "web_search"},
                {"type": "function", "name": "lookup", "description": "find", "parameters": {"type": "object"}},
                {"type": "custom", "name": "apply_patch", "description": "patch files"},
            ],
            "tool_choice": {"type": "image_generation"},
        },
        model="auto",
    )
    names = [item["function"]["name"] for item in chat["tools"]]
    assert names == ["ImageGen", "WebSearch", "lookup", "apply_patch"]
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "ImageGen"}}
    prepared = prepare_chat_payload(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "image_generation"}, {"type": "function", "function": {"name": "lookup"}}],
        },
        realm="cn",
    )
    assert [item["function"]["name"] for item in prepared["tools"]] == ["ImageGen", "lookup"]
    catalog = {item.id: item for item in WorkBuddyAdapter().catalog()}
    assert catalog["workbuddy/hunyuan-image-v3.0"].type == "image"


def test_workbuddy_images_http_uses_latest_image_model(client):
    key = _key(client)
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/images/generations",
        json={"model": "workbuddy/glm-5.2", "prompt": "draw a cat"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    assert captured[0]["path"].endswith("/v1/images/generations")
    assert captured[0]["body"]["model"] == "hunyuan-image-v3.0"


def test_workbuddy_hosted_image_generation_intercept(client):
    key = _key(client)
    encoded = base64.b64encode(b"fake-image").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/images/generations" in request.url.path:
            return httpx.Response(200, json={"data": [{"b64_json": encoded}]})
        return httpx.Response(404, json={"code": 1, "msg": "missing"})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "workbuddy/glm-5.2",
            "input": "draw a cat",
            "stream": True,
            "tools": [{"type": "image_generation"}],
            "tool_choice": {"type": "image_generation"},
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    assert "image_generation_call" in response.text
    assert encoded in response.text


def test_workbuddy_response_input_rejects_missing_inline_history():
    try:
        responses_to_chat({"input": "hello", "previous_response_id": "resp_old"}, model="auto")
    except ValueError as exc:
        assert "inline" in str(exc)
    else:
        raise AssertionError("previous_response_id was accepted")


def test_workbuddy_refresh_window_and_global_realm():
    payload = _auth(expires_at=int(dt.datetime.now(dt.UTC).timestamp()) + 60)
    payload["auth"]["domain"] = "www.workbuddy.ai"
    parsed = load_credentials(payload, path=Path("workbuddy.json"))
    assert parsed.realm == "global"
    assert WorkBuddyAdapter().parse_import(payload, "workbuddy.json")[0].account_id == "workbuddy:global:wb-user-1"


def test_workbuddy_expired_token_refreshes_before_chat(client):
    payload = _auth(expires_at=int(dt.datetime.now(dt.UTC).timestamp()) + 30)
    imported = client.post(
        "/admin/api/accounts/import",
        data={"provider": "workbuddy"},
        files={"files[]": ("workbuddy-desktop.info", json.dumps(payload), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200
    key = client.post("/admin/api/keys", json={"name": "wb-refresh"}, headers=ADMIN_HEADERS).json()["key"]
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/auth/token/refresh"):
            assert request.headers["x-refresh-token"] == "wb-refresh-test"
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "wb-refreshed", "refreshToken": "wb-new-refresh",
                "expiresIn": 3600,
            }})
        assert request.headers["authorization"] == "Bearer wb-refreshed"
        return httpx.Response(200, content=_chat_sse(), headers={"content-type": "text/event-stream"})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "workbuddy/glm-5.2", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    assert paths == ["/v2/plugin/auth/token/refresh", "/v2/chat/completions"]


def test_workbuddy_quota_uses_credit_progress(client):
    from app.providers.workbuddy import parse_quota_snapshot

    snapshot = parse_quota_snapshot({
        "code": 0,
        "data": {
            "Response": {
                "Data": {
                    "Accounts": [
                        {
                            "PackageName": "Pro",
                            "CapacityRemainPrecise": 700,
                            "CapacityUsed": 300,
                            "CapacitySize": 1000,
                            "CycleEndTime": "2026-10-01 00:00:00",
                        }
                    ]
                }
            }
        },
    })
    assert snapshot is not None
    primary = snapshot["limits"][0]["primary"]
    assert snapshot["quota_kind"] == "credits"
    assert primary["window_label"] == "积分"
    assert primary["remaining_percent"] == 70.0
    assert primary["remaining_amount"] == 700.0
    assert primary["total_amount"] == 1000.0

    _key(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get-user-resource"):
            return httpx.Response(200, json={
                "code": 0,
                "data": {
                    "Response": {
                        "Data": {
                            "Accounts": [
                                {
                                    "PackageName": "Pro",
                                    "CycleCapacityRemain": 40,
                                    "CycleCapacityUsed": 60,
                                    "CycleCapacitySize": 100,
                                    "CycleEndTime": "2026-10-01 00:00:00",
                                }
                            ]
                        }
                    }
                },
            })
        return httpx.Response(404, json={"code": 1, "msg": "missing"})

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    row = next(item for item in response.json()["data"] if item["provider"] == "workbuddy")
    assert row["quota_kind"] == "credits"
    assert row["message"] is None
    assert row["limits"][0]["primary"]["window_label"] == "积分"
    assert row["limits"][0]["primary"]["remaining_percent"] == 40.0
    assert "订阅额度" not in json.dumps(row, ensure_ascii=False)
