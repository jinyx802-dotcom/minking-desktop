from __future__ import annotations

import base64
import json
import sqlite3

import httpx
from conftest import ADMIN_HEADERS, auth_payload, import_pool

from app.codex_gateway import codex_gateway
from app.http_client import set_http_transport
from app.store.gateway import GatewayStore, gateway_store


def key_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def test_legacy_api_key_routes_migrate_to_provider_pk(tmp_path):
    store = GatewayStore()
    await store.start(tmp_path / "routes.sqlite3")
    try:
        columns = await store.all("PRAGMA table_info(api_key_routes)")
        names = {row["name"] for row in columns}
        assert {"key_id", "provider"} <= names
    finally:
        await store.stop()


def test_key_can_be_created_without_primary_and_account_is_soft_deleted(client):
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(auth_payload("acct-primary")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert imported.status_code == 200
    assert imported.json()["generated_api_key"] is None
    created = client.post(
        "/admin/api/keys", json={"name": "unbound"}, headers=ADMIN_HEADERS
    )
    assert created.status_code == 200
    ignored = client.post(
        "/admin/api/keys",
        json={"name": "ignored-route", "preferred_account_id": "missing"},
        headers=ADMIN_HEADERS,
    )
    assert ignored.status_code == 200
    removed = client.delete("/admin/api/accounts/acct-primary", headers=ADMIN_HEADERS)
    assert removed.status_code == 200
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed == []
    restored = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(auth_payload("acct-primary")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200
    body = restored.json()
    assert body["created"] == []
    assert body["updated"][0]["account_id"] == "acct-primary"
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["account_id"] == "acct-primary"
    assert listed[0]["status"] == "active"


def test_next_independent_request_rebalances_after_failure(client):
    imported = import_pool(
        client, auth_payload("acct-primary"), auth_payload("acct-backup")
    )
    key = imported["generated_api_key"]["key"]
    seen: list[str] = []
    failed: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        account_id = request.headers["ChatGPT-Account-Id"]
        seen.append(account_id)
        if failed is None:
            failed = account_id
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        if account_id == failed:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    first = client.post(
        "/v1/images/generations", json={"prompt": "first"}, headers=key_headers(key)
    )
    assert first.status_code == 502
    assert failed is not None
    assert seen[0] == failed
    assert seen == [failed, failed]

    seen.clear()
    assert client.post(
        "/v1/images/generations", json={"prompt": "second"}, headers=key_headers(key)
    ).status_code == 200
    assert failed not in seen
    seen.clear()
    # Future independent requests may rebalance again until model cooldown excludes it.


def test_manual_route_remains_pinned_until_restored_to_automatic(client):
    imported = import_pool(
        client,
        auth_payload("acct-primary"),
        auth_payload("acct-busy"),
        auth_payload("acct-idle"),
    )
    key = imported["generated_api_key"]["key"]
    assert client.put(
        f"/admin/api/keys/{imported['generated_api_key']['id']}/route",
        json={"provider": "codex", "preferred_account_id": "acct-primary"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    busy = codex_gateway._semaphore("acct-busy")
    client.portal.call(busy.acquire)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        account_id = request.headers["ChatGPT-Account-Id"]
        seen.append(account_id)
        if account_id == "acct-primary":
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    try:
        set_http_transport(httpx.MockTransport(handler))
        response = client.post(
            "/v1/images/generations", json={"prompt": "balance"}, headers=key_headers(key)
        )
        assert response.status_code == 502
        seen.clear()
        assert client.post(
            "/v1/images/generations", json={"prompt": "balance-next"}, headers=key_headers(key)
        ).status_code == 502
        seen.clear()
        response = client.post(
            "/v1/images/generations", json={"prompt": "balance-switched"}, headers=key_headers(key)
        )
        assert response.status_code == 502
        assert seen[0] == "acct-primary"
        client.put(f"/admin/api/keys/{imported['generated_api_key']['id']}/route",
                   json={"provider":"codex","preferred_account_id":None},headers=ADMIN_HEADERS)
        seen.clear()
        response=client.post('/v1/images/generations',json={'prompt':'automatic'},headers=key_headers(key))
        assert response.status_code==200
        assert seen[-1]=='acct-idle'
    finally:
        client.portal.call(busy.release)


def test_key_routes_stick_per_provider_and_manual_selection_isolated(client, tmp_path):
    async def select(key, provider, model):
        return await codex_gateway.select_account(
            key, kind="chat", provider=provider, model=model
        )
    async def add_account(provider, suffix):
        return await gateway_store.upsert_account(
            f"{provider}-{suffix}", tmp_path / f"{provider}-{suffix}.json", None,
            provider=provider, label=f"{provider.upper()} {suffix}",
        )
    import_pool(client, auth_payload("codex-a"), auth_payload("codex-b"))
    for provider in ("grok", "antigravity"):
        for suffix in ("a", "b"):
            client.portal.call(add_account, provider, suffix)
    first = client.post("/admin/api/keys", json={"name": "Alice"}, headers=ADMIN_HEADERS).json()
    second = client.post("/admin/api/keys", json={"name": "Bob"}, headers=ADMIN_HEADERS).json()
    keys = [client.portal.call(codex_gateway.authenticate_key, row["key"]) for row in (first, second)]
    for provider in ("codex", "grok", "antigravity"):
        selections = [
            client.portal.call(select, key, provider, f"{provider}-model")["account_id"]
            for key in keys
        ]
        assert selections[0] != selections[1]
        assert client.portal.call(
            select, keys[0], provider, f"{provider}-another-model"
        )["account_id"] == selections[0]

    changed = client.put(
        f"/admin/api/keys/{first['id']}/route",
        json={"provider": "codex", "preferred_account_id": "codex-b"},
        headers=ADMIN_HEADERS,
    )
    assert changed.status_code == 200
    listed = client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"]
    route = next(item for item in listed if item["id"] == first["id"])["routes"]
    assert next(item for item in route if item["provider"] == "codex")["mode"] == "manual"
    assert client.portal.call(
        select, keys[0], "codex", "gpt-6-astra"
    )["account_id"] == "codex-b"
    assert client.put(
        f"/admin/api/keys/{first['id']}/route",
        json={"provider": "grok", "preferred_account_id": "codex-a"},
        headers=ADMIN_HEADERS,
    ).status_code == 422
    assert client.put(
        f"/admin/api/keys/{first['id']}/route",
        json={"provider": "codex", "preferred_account_id": None},
        headers=ADMIN_HEADERS,
    ).status_code == 200


def test_success_resets_key_provider_failure_streak(client):
    imported = import_pool(client, auth_payload("streak-a"), auth_payload("streak-b"))
    key = imported["generated_api_key"]
    client.put(f"/admin/api/keys/{key['id']}/route",
               json={"provider":"codex","preferred_account_id":"streak-a"},headers=ADMIN_HEADERS)
    attempts = 0
    accounts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        accounts.append(request.headers["ChatGPT-Account-Id"])
        if attempts in {1, 2, 4, 5}:
            return httpx.Response(500, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})

    set_http_transport(httpx.MockTransport(handler))
    headers = key_headers(key["key"])
    assert client.post("/v1/images/generations", json={"prompt": "fail"}, headers=headers).status_code == 502
    assert client.post("/v1/images/generations", json={"prompt": "recover"}, headers=headers).status_code == 200
    assert client.post("/v1/images/generations", json={"prompt": "fail again"}, headers=headers).status_code == 502
    assert len(set(accounts)) == 1
    route = client.portal.call(
        gateway_store.one,
        "SELECT active_account_id FROM api_key_routes WHERE key_id=? AND provider='codex'",
        (key["id"],),
    )
    assert route["active_account_id"] == accounts[0]
    streak = client.portal.call(
        gateway_store.one,
        "SELECT failures FROM route_streaks WHERE key_id=? AND provider='codex'",
        (key["id"],),
    )
    assert streak["failures"] == 1


def _mcp_post(client, key: str, payload: dict) -> httpx.Response:
    return client.post(
        "/mcp",
        json=payload,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
        },
    )


def test_mcp_auth_tool_discovery_and_generated_image_result(client):
    imported = import_pool(client, auth_payload("acct-mcp"))
    key = imported["generated_api_key"]["key"]
    unauthorized = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert unauthorized.status_code == 401

    initialized = _mcp_post(client, key, {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "1"},
        },
    })
    assert initialized.status_code == 200, initialized.text

    tools = _mcp_post(client, key, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    })
    assert tools.status_code == 200, tools.text
    names = {item["name"] for item in tools.json()["result"]["tools"]}
    assert names == {"generate_image", "edit_image"}

    invalid_quality = _mcp_post(client, key, {
        "jsonrpc": "2.0",
        "id": 21,
        "method": "tools/call",
        "params": {
            "name": "generate_image",
            "arguments": {"prompt": "a test image", "quality": "minimal"},
        },
    })
    invalid_result = invalid_quality.json()["result"]
    assert invalid_result["isError"] is True
    assert invalid_result["content"][0]["text"] == (
        "quality must be one of auto, low, medium, high"
    )

    png = b"\x89PNG\r\n\x1a\n" + b"mcp-image"
    encoded = base64.b64encode(png).decode()
    set_http_transport(httpx.MockTransport(lambda request: httpx.Response(
        200,
        json={
            "created": 1,
            "data": [{"b64_json": encoded}],
            "output_format": "png",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        },
    )))
    generated = _mcp_post(client, key, {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "generate_image", "arguments": {"prompt": "a test image"}},
    })
    assert generated.status_code == 200, generated.text
    result = generated.json()["result"]
    assert result["isError"] is False
    image = next(item for item in result["content"] if item["type"] == "image")
    assert image["mimeType"] == "image/png" and image["data"] == encoded
    url = result["structuredContent"]["images"][0]["url"]
    downloaded = client.get(url)
    assert downloaded.status_code == 200 and downloaded.content == png

    uploaded = client.post(
        "/v1/files",
        data={"purpose": "user_data"},
        files={"file": ("source.png", png, "image/png")},
        headers=key_headers(key),
    )
    assert uploaded.status_code == 200
    upstream: list[dict] = []

    def edit_handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    set_http_transport(httpx.MockTransport(edit_handler))
    edited = _mcp_post(client, key, {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "edit_image",
            "arguments": {
                "prompt": "make a small edit",
                "file_ids": [uploaded.json()["id"]],
            },
        },
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["result"]["isError"] is False
    assert upstream[0]["image"][0].startswith("data:image/png;base64,")
