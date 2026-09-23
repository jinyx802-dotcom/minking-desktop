from __future__ import annotations

import json
import logging

import httpx
from conftest import ADMIN_HEADERS, auth_payload, import_pool

from app.codex_gateway import codex_gateway, model_family, models_mismatch
from app.config import settings
from app.http_client import set_http_transport
from app.store.gateway import gateway_store, iso_now
from test_codex_gateway import key_headers, sse


def test_model_family_treats_suffix_variants_as_same_and_flags_cross_family():
    assert model_family("grok-4.6") == "grok-4.6"
    assert model_family("grok-4.6-build") == "grok-4.6"
    assert model_family("gpt-6-astra") == "gpt-6"
    assert model_family("gpt-5-sol") == "gpt-5"
    assert model_family("gpt-5.6-sol") == "gpt-5.6"
    assert models_mismatch("grok-4.6", "grok-4.6-build") is False
    assert models_mismatch("gpt-6", "gpt-6-astra") is False
    assert models_mismatch("gpt-6", "gpt-5-sol") is True
    assert models_mismatch("gpt-6-astra", "gpt-5.6-sol") is True
    assert models_mismatch("gpt-6", None) is False
    assert models_mismatch("gpt-6", "") is False


def test_call_record_uses_requested_model_not_codex_default(client):
    imported = import_pool(client, auth_payload("acct-requested-model"))
    key = imported["generated_api_key"]["key"]
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "model": "grok-4.6"},
        headers=key_headers(key),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_accounts"


def test_invalid_json_call_does_not_record_default_codex_model(client):
    imported = import_pool(client, auth_payload("acct-invalid-model"))
    key = imported["generated_api_key"]["key"]
    response = client.post(
        "/v1/responses",
        content="not-json",
        headers={**key_headers(key), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_call_records_keep_request_model_and_flag_family_mismatch(client):
    imported = import_pool(client, auth_payload("acct-model"))
    key = imported["generated_api_key"]["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-model"),
            )
        )
    )
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "model": "gpt-6-astra"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "gpt-5.6-sol"
    assert response.headers["X-TS-Requested-Model"] == "gpt-6-astra"
    assert response.headers["X-TS-Actual-Model"] == "gpt-5.6-sol"
    assert response.headers["X-TS-Model-Mismatch"] == "true"


def test_codex_upstream_logs_sent_astra_and_returned_sol(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "codex_client_version", "0.155.1")
    imported = import_pool(client, auth_payload("acct-model-log"))
    key = imported["generated_api_key"]["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-model-log"),
            )
        )
    )
    caplog.set_level(logging.INFO, logger="transfer_station.errors")
    response = client.post(
        "/v1/responses",
        json={"input": "hello-secret-prompt", "model": "gpt-6-astra"},
        headers={**key_headers(key), "User-Agent": "Codex Desktop/0.155.0-alpha.2.6"},
    )
    assert response.status_code == 200, response.text
    dispatch = [record.message for record in caplog.records if record.message.startswith("codex_upstream_dispatch ")]
    returned = [record.message for record in caplog.records if record.message.startswith("codex_upstream_returned ")]
    assert len(dispatch) == 1
    assert "sent_model=gpt-6-astra" in dispatch[0]
    assert "client_model=gpt-6-astra" in dispatch[0]
    assert "lite=True" in dispatch[0]
    assert "originator=codex_cli_rs" in dispatch[0]
    assert "version=0.155.1" in dispatch[0]
    assert "url=" in dispatch[0] and "/responses" in dispatch[0]
    assert "Codex Desktop/0.155.0-alpha.2.6" in dispatch[0]
    assert any("event=response.created" in item and "returned_model=gpt-5.6-sol" in item for item in returned)
    assert any("event=response.completed" in item and "returned_model=gpt-5.6-sol" in item for item in returned)
    combined = "\n".join(dispatch + returned)
    assert "hello-secret-prompt" not in combined
    assert key not in combined
    assert "Authorization" not in combined


def test_codex_upstream_uses_global_client_version(client, caplog, monkeypatch):
    monkeypatch.setattr(settings, "codex_client_version", "0.156.0")
    imported = import_pool(client, auth_payload("acct-client-version"))
    key = imported["generated_api_key"]["key"]
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["client_version"] = request.url.params.get("client_version", "")
        seen["version"] = request.headers.get("version", "")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-client-version"),
        )

    set_http_transport(httpx.MockTransport(handler))
    caplog.set_level(logging.INFO, logger="transfer_station.errors")
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "model": "gpt-6-astra"},
        headers={
            **key_headers(key),
            "User-Agent": "codex_exec/0.155.0-alpha.9.2 (Windows; x86_64)",
        },
    )
    assert response.status_code == 200, response.text
    assert seen["client_version"] == "0.156.0"
    assert seen["version"] == "0.156.0"
    dispatch = [
        record.message
        for record in caplog.records
        if record.message.startswith("codex_upstream_dispatch ")
    ]
    assert len(dispatch) == 1
    assert "version=0.156.0" in dispatch[0]


def test_streaming_response_headers_expose_buffered_actual_model(client):
    imported = import_pool(client, auth_payload("acct-stream-model"))
    key = imported["generated_api_key"]["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-stream-model"),
            )
        )
    )
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "model": "gpt-6-astra", "stream": True},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-TS-Actual-Model"] == "gpt-5.6-sol"
    assert response.headers["X-TS-Model-Mismatch"] == "true"


def test_usage_summary_hides_deleted_keys_and_calls_use_current_name(client):
    imported = import_pool(client, auth_payload("acct-usage-name"))
    key_info = imported["generated_api_key"]
    key = key_info["key"]
    set_http_transport(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(
                    "resp-usage-name",
                    usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18, "input_tokens_details": {"cached_tokens": 4}},
                ),
            )
        )
    )
    response = client.post(
        "/v1/responses",
        json={"input": "hello", "model": "gpt-6-astra"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    client.portal.call(
        gateway_store.execute,
        "UPDATE api_keys SET name=? WHERE id=?",
        ("张三", key_info["id"]),
    )
    calls = client.get("/admin/api/calls", headers=ADMIN_HEADERS).json()["data"]
    assert calls[0]["key_name"] == "张三"
    assert calls[0]["input_tokens"] == 11
    assert calls[0]["output_tokens"] == 7
    assert calls[0]["cached_tokens"] == 4
    assert calls[0]["usage_unknown"] is False
    summary = client.get("/admin/api/usage/summary?range=all", headers=ADMIN_HEADERS).json()["data"]
    assert any(row["key_id"] == key_info["id"] and row["name"] == "张三" for row in summary)
    removed = client.delete(f"/admin/api/keys/{key_info['id']}", headers=ADMIN_HEADERS)
    assert removed.status_code == 200
    summary_after = client.get("/admin/api/usage/summary?range=all", headers=ADMIN_HEADERS).json()["data"]
    assert all(row["key_id"] != key_info["id"] for row in summary_after)
    calls_after = client.get("/admin/api/calls", headers=ADMIN_HEADERS).json()["data"]
    assert calls_after[0]["key_name"] == "default"


def test_soft_deleted_account_cannot_be_toggled_and_reimport_restores(client):
    import_pool(client, auth_payload("acct-soft"))
    disabled = client.patch(
        "/admin/api/accounts/acct-soft",
        json={"enabled": False},
        headers=ADMIN_HEADERS,
    )
    assert disabled.status_code == 200
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["status"] == "disabled"
    enabled = client.patch(
        "/admin/api/accounts/acct-soft",
        json={"enabled": True},
        headers=ADMIN_HEADERS,
    )
    assert enabled.status_code == 200
    removed = client.delete("/admin/api/accounts/acct-soft", headers=ADMIN_HEADERS)
    assert removed.status_code == 200
    assert client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"] == []
    revived = client.patch(
        "/admin/api/accounts/acct-soft",
        json={"enabled": True},
        headers=ADMIN_HEADERS,
    )
    assert revived.status_code == 404
    restored = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(auth_payload("acct-soft")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200
    assert restored.json()["updated"][0]["account_id"] == "acct-soft"
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["status"] == "active"


def test_ten_model_failures_cool_only_that_account_model(client):
    imported = import_pool(client, auth_payload("acct-cool-a"))
    key = imported["generated_api_key"]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        account_id = request.headers["ChatGPT-Account-Id"]
        seen.append(account_id)
        return httpx.Response(500, json={"error": {"message": "temporary"}})

    set_http_transport(httpx.MockTransport(handler))
    for _ in range(10):
        response = client.post(
            "/v1/images/generations", json={"prompt": "cool"}, headers=key_headers(key)
        )
        assert response.status_code == 502
    assert len(seen) == 20
    cooled = client.portal.call(
        gateway_store.one,
        "SELECT failures,cooldown_until FROM account_model_health WHERE account_id=? AND model=?",
        ("acct-cool-a", settings.codex_image_model),
    )
    assert cooled["failures"] == 10
    assert cooled["cooldown_until"] > iso_now()
    assert client.post(
        "/v1/images/generations", json={"prompt": "again"}, headers=key_headers(key)
    ).status_code == 503
    client.portal.call(
        gateway_store.execute,
        "UPDATE account_model_health SET cooldown_until=? WHERE account_id=? AND model=?",
        ("2000-01-01T00:00:00Z", "acct-cool-a", settings.codex_image_model),
    )
    assert client.post(
        "/v1/images/generations", json={"prompt": "probe"}, headers=key_headers(key)
    ).status_code == 502
    probed = client.portal.call(
        gateway_store.one,
        "SELECT failures,cooldown_until FROM account_model_health WHERE account_id=? AND model=?",
        ("acct-cool-a", settings.codex_image_model),
    )
    assert probed["failures"] == 1 and probed["cooldown_until"] is None
    route_key = client.portal.call(codex_gateway.authenticate_key, key)
    async def select_text():
        return await codex_gateway.select_account(
            route_key, kind="chat", provider="codex", model="gpt-6-astra"
        )
    text = client.portal.call(select_text)
    assert text["account_id"] == "acct-cool-a"
