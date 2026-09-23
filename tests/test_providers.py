from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import httpx
from conftest import ADMIN_HEADERS, auth_payload, import_pool

from app.http_client import set_http_transport
from app.providers.antigravity import parse_oauth_payload
from app.providers.base import default_model_for, resolve_provider_model
from app.providers.codex import client_version_from_user_agent
from app.providers.codex_protocol import to_responses_payload
from app.providers.registry import is_image_model
from app.providers.grok import parse_auth_payload

ANTIGRAVITY_OAUTH = Path(__file__).resolve().parent / "fixtures" / "antigravity" / "oauth_account.example.json"


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


def grok_chat_sse(text: str = "OK", usage: dict | None = None, *, include_usage: bool = True) -> bytes:
    frames = [
        {
            "id": "chatcmpl-grok",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}}],
        },
        {
            "id": "chatcmpl-grok",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    if include_usage:
        frames.append({
            "id": "chatcmpl-grok",
            "object": "chat.completion.chunk",
            "choices": [],
            "usage": usage if usage is not None else {
                "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
            },
        })
    return ("".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n").encode()


GROK_BILLING_FIXTURE = {
    "config": {
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": "2026-09-15T10:49:25.011346+00:00",
            "end": "2026-09-22T10:49:25.011346+00:00",
        },
        "creditUsagePercent": 38.0,
        "onDemandCap": {"val": 0},
        "onDemandUsed": {"val": 0},
        "billingPeriodStart": "2026-09-15T10:49:25.011346+00:00",
        "billingPeriodEnd": "2026-09-22T10:49:25.011346+00:00",
    }
}
def key_headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def sse(response_id: str, text: str = "OK") -> bytes:
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


def test_codex_failed_code_maps_retry_whitelist():
    from app.providers.codex_protocol import codex_failed_code

    assert codex_failed_code(400) == "invalid_prompt"
    assert codex_failed_code(401) == "invalid_prompt"
    assert codex_failed_code(403) == "invalid_prompt"
    assert codex_failed_code(422) == "invalid_prompt"
    assert codex_failed_code(429) == "rate_limit_exceeded"
    assert codex_failed_code(500) == "server_error"
    assert codex_failed_code(502) == "server_error"


def test_resolve_provider_model_prefixes_and_aliases():
    assert resolve_provider_model("gpt-6") == ("codex", "gpt-6-astra")
    assert resolve_provider_model("gpt6") == ("codex", "gpt-6-astra")
    assert resolve_provider_model("gpt-image-2.5") == ("codex", "gpt-image-2.5-flare")
    assert resolve_provider_model("grok-4.6") == ("grok", "grok-4.6")
    assert resolve_provider_model("gpt-reserve") == ("grok", "grok-4.6")
    assert resolve_provider_model("grok-4.6-build") == ("grok", "grok-4.6-build")
    assert resolve_provider_model("grok/grok-4.6") == ("grok", "grok-4.6")
    assert resolve_provider_model("grok/gpt-reserve") == ("grok", "grok-4.6")
    assert resolve_provider_model("grok/grok-4.5") == ("grok", "grok-4.5")
    assert resolve_provider_model("codex/gpt-6-astra") == ("codex", "gpt-6-astra")
    assert resolve_provider_model("gemini-3-flash") == ("antigravity", "gemini-3-flash")
    assert resolve_provider_model("gemini-3.8-flash") == ("antigravity", "gemini-3.8-flash")
    assert resolve_provider_model("gemini-3.8-flash-medium") == ("antigravity", "gemini-3.8-flash")
    assert resolve_provider_model("gemini-3.6-flash-low") == ("antigravity", "gemini-3.8-flash")
    assert resolve_provider_model("gemini-2.5-flash") == ("antigravity", "gemini-2.5-flash")
    assert resolve_provider_model("claude-sonnet-4") == ("antigravity", "claude-sonnet-4")
    assert resolve_provider_model(None, default_provider="antigravity") == ("antigravity", "gemini-3.8-flash")
    assert resolve_provider_model("antigravity/") == ("antigravity", "gemini-3.8-flash")
    assert default_model_for("antigravity", "text") == "gemini-3.8-flash"
    assert default_model_for("antigravity", "image") == "gemini-3.1-flash-image"
    assert default_model_for("workbuddy", "image") == "hunyuan-image-v3.0"
    assert default_model_for("grok", "video") == "grok-imagine-video-1.5"
    assert default_model_for("codex", "video") == ""
    assert default_model_for("antigravity", "video") == ""
    assert is_image_model("hunyuan-image-v3.0")
    assert is_image_model("workbuddy/hunyuan-image-v3.0")
    assert resolve_provider_model("gpt-6-astra")[0] == "codex"
    assert resolve_provider_model("grok-4.6")[0] == "grok"
    assert is_image_model("gpt-image-2.5-flare")
    assert is_image_model("gpt-image-2.5")
    assert is_image_model("gpt-image-2")
    assert is_image_model("grok-imagine-image-2.0")
    assert is_image_model("gemini-3.1-flash-image")
    assert not is_image_model("gpt-6-astra")
    assert not is_image_model("grok-4.6")
    assert not is_image_model("gemini-3.8-flash")
    assert not is_image_model("grok-imagine-video-1.5")
    from app.providers.registry import is_video_model, resolve_video_request_model

    assert is_video_model("grok-imagine-video-1.5")
    assert is_video_model("grok-imagine-video")
    assert not is_video_model("grok-4.6")
    assert not is_video_model("gpt-6-astra")
    assert resolve_video_request_model("grok-4.6", default_provider="codex") == (
        "grok",
        "grok-imagine-video-1.5",
    )
    assert resolve_video_request_model(None, default_provider="codex") == (
        "grok",
        "grok-imagine-video-1.5",
    )
    assert resolve_video_request_model("gpt-6-astra", default_provider="codex") == (
        "codex",
        "gpt-6-astra",
    )


def test_codex_client_version_is_extracted_from_supported_user_agents():
    assert client_version_from_user_agent("codex_exec/0.155.0-alpha.9.2 (Windows)") == "0.155.0-alpha.9.2"
    assert client_version_from_user_agent("codex_cli_rs/0.154.0") == "0.154.0"
    assert client_version_from_user_agent("Codex Desktop/26.915.31945") == "26.915.31945"
    assert client_version_from_user_agent("openai-python/1.40.0") is None


def test_lite_contract_is_idempotent_and_drops_bare_reasoning_ids():
    from app.providers.codex_protocol import apply_responses_lite_contract, strip_bare_reasoning

    payload = {
        "model": "gpt-6-astra",
        "instructions": "be brief",
        "input": [
            {"type": "reasoning", "id": "rs_only"},
            {"type": "reasoning", "id": "rs_kept", "encrypted_content": "cipher"},
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "tools": [{"type": "function", "name": "lookup"}],
        "parallel_tool_calls": True,
        "max_output_tokens": 32,
        "reasoning": {"effort": "high"},
    }
    apply_responses_lite_contract(payload)
    strip_bare_reasoning(payload)
    apply_responses_lite_contract(payload)
    assert payload["instructions"] == ""
    assert payload["reasoning"] == {"effort": "high", "context": "all_turns"}
    assert payload["parallel_tool_calls"] is False
    assert "max_output_tokens" not in payload
    assert "tools" not in payload
    assert payload["input"][0]["type"] == "additional_tools"
    assert all(item.get("encrypted_content") != "cipher" for item in payload["input"])
    assert not any(item.get("type") == "reasoning" and item.get("id") == "rs_kept" for item in payload["input"])
    developer = [item for item in payload["input"] if item.get("role") == "developer" and item.get("type") == "message"]
    assert len(developer) == 1


def test_strip_bare_reasoning_keeps_visible_text_and_drops_foreign_ciphertext():
    from app.providers.codex_protocol import strip_bare_reasoning
    from app.providers.grok import COMPACT_SUMMARY_PREFIX

    payload = {
        "input": [
            {"type": "reasoning", "id": "rs_cipher", "encrypted_content": "gAAA-foreign"},
            {
                "type": "reasoning",
                "id": "rs_summary",
                "encrypted_content": "gAAA-foreign",
                "summary": [{"type": "summary_text", "text": "plan"}],
            },
            {"type": "compaction", "encrypted_content": f"{COMPACT_SUMMARY_PREFIX}\nkeep the auth change"},
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
                "encrypted_content": "gAAA-foreign",
            },
        ]
    }
    strip_bare_reasoning(payload)
    summary = next(item for item in payload["input"] if item.get("type") == "reasoning")
    assert "id" not in summary
    summary = payload["input"][0]
    assert summary["summary"][0]["text"] == "plan"
    assert "encrypted_content" not in summary
    compact = next(item for item in payload["input"] if item.get("type") == "compaction")
    assert compact["encrypted_content"].startswith(COMPACT_SUMMARY_PREFIX)
    message = next(item for item in payload["input"] if item.get("type") == "message")
    assert message["content"][0]["text"] == "done"
    assert "encrypted_content" not in message


def test_replayed_server_ids_are_removed_and_call_ids_stay():
    from app.providers.codex_protocol import strip_bare_reasoning

    payload = {
        "input": [
            {"type": "item_reference", "id": "msg_stored"},
            {"type": "message", "id": "msg_user", "role": "user", "content": [{"type": "input_text", "text": "继续"}]},
            {
                "type": "function_call",
                "id": "fc_old",
                "call_id": "call_lookup",
                "name": "lookup",
                "arguments": "{\"q\":\"a\"}",
            },
            {"type": "function_call_output", "id": "fco_old", "call_id": "call_lookup", "output": "ok"},
        ]
    }
    strip_bare_reasoning(payload)
    assert [item.get("type") for item in payload["input"]] == [
        "message",
        "function_call",
        "function_call_output",
    ]
    assert all("id" not in item for item in payload["input"])
    assert payload["input"][1]["call_id"] == "call_lookup"
    assert payload["input"][1]["name"] == "lookup"
    assert payload["input"][2]["call_id"] == "call_lookup"
    assert payload["input"][0]["content"][0]["text"] == "继续"


def test_discovered_grok_text_model_is_listed_without_a_code_change():
    from app.providers.grok import GrokAdapter, parse_openai_model_ids, remember_text_models

    assert parse_openai_model_ids({"data": [{"id": "grok-new"}, {"id": "grok-imagine-image-2.0"}, " "]}) == [
        "grok-new",
        "grok-imagine-image-2.0",
    ]
    remember_text_models(["grok-new", "grok-imagine-image-2.0", "other-model"])
    try:
        ids = [item.id for item in GrokAdapter().catalog()]
        assert "grok-new" in ids
        assert ids.count("grok-imagine-image-2.0") == 1
        assert "other-model" not in ids
    finally:
        remember_text_models([])


def test_responses_payload_preserves_codex_client_negotiation_fields():
    payload, _lite = to_responses_payload(
        {
            "model": "gpt-6-astra",
            "input": "hello",
            "client_metadata": {"source": "codex"},
            "prompt_cache_key": "cache-key",
            "metadata": {"task": "probe"},
        },
        default_model="gpt-6-astra",
    )
    assert payload["client_metadata"] == {"source": "codex"}
    assert payload["prompt_cache_key"] == "cache-key"
    assert payload["metadata"] == {"task": "probe"}


def test_parse_grok_cli_auth_json():
    imported = parse_auth_payload(grok_cli_payload(), source="pool", path="auth.json")
    assert len(imported) == 1
    assert imported[0].account_id == "grok:user-grok-1"
    assert imported[0].label == "grok@example.com"
    assert imported[0].auth_mode == "oauth"
    assert "video" in imported[0].capabilities
    assert "key" in imported[0].payload
    assert imported[0].payload["key"].startswith("eyJ")


def test_parse_grok_api_key():
    imported = parse_auth_payload({"XAI_API_KEY": "xai-abcdef123"}, source="pool", path="key.json")
    assert imported[0].account_id.startswith("grok:key:")
    assert imported[0].auth_mode == "api_key"


def test_import_defaults_to_codex_without_provider(client):
    response = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(auth_payload("acct-codex")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200
    rows = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert rows[0]["provider"] == "codex"
    assert rows[0]["account_id"] == "acct-codex"


def test_import_grok_provider_and_list_fields(client):
    response = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["account_id"] == "grok:user-grok-1"
    rows = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert rows[0]["provider"] == "grok"
    assert rows[0]["label"] == "grok@example.com"
    assert "video" in rows[0]["capabilities"]
    created = client.post(
        "/admin/api/keys",
        json={"name": "picker", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    openai_list = client.get("/v1/models", headers=key_headers(key)).json()["data"]
    openai_ids = {item["id"] for item in openai_list}
    assert "grok-4.6" in openai_ids
    assert "grok-imagine-image-2.0" in openai_ids
    assert "slug" not in next(item for item in openai_list if item["id"] == "grok-4.6")
    picker = client.get(
        "/v1/models",
        params={"client_version": "0.154.0"},
        headers=key_headers(key),
    )
    assert picker.status_code == 200, picker.text
    body = picker.json()
    assert "data" not in body
    slugs = {item["slug"] for item in body["models"]}
    assert "grok-4.6" in slugs
    grok = next(item for item in body["models"] if item["slug"] == "grok-4.6")
    assert grok["display_name"] == "Grok 4.6"
    assert grok["visibility"] == "list"
    assert grok["supported_in_api"] is True
    assert grok["experimental_supported_tools"] == ["image_generation"]
    assert "id" not in grok
    assert "grok-imagine-image-2.0" not in slugs
    ua = client.get(
        "/v1/models",
        headers={**key_headers(key), "User-Agent": "codex_cli_rs/0.154.0"},
    )
    assert "grok-4.6" in {item["slug"] for item in ua.json()["models"]}
    exec_ua = client.get(
        "/v1/models",
        headers={**key_headers(key), "User-Agent": "codex_exec/0.155.0-alpha.2.6"},
    )
    assert exec_ua.status_code == 200
    assert "data" not in exec_ua.json()
    assert "grok-4.6" in {item["slug"] for item in exec_ua.json()["models"]}


def test_import_local_grok(client, tmp_path, monkeypatch):
    from app.config import settings

    grok_dir = tmp_path / "grok-home"
    grok_dir.mkdir()
    (grok_dir / "auth.json").write_text(json.dumps(grok_cli_payload("local-user", "local@example.com")), encoding="utf-8")
    monkeypatch.setattr(settings, "grok_home", str(grok_dir))
    monkeypatch.setattr(settings, "gateway_import_local_enabled", True)
    response = client.post(
        "/admin/api/accounts/import-local",
        json={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    created = response.json()["created"]
    assert created[0]["account_id"] == "grok:local-user"
    assert created[0]["label"] == "local@example.com"


def _antigravity_oauth_bytes() -> bytes:
    return ANTIGRAVITY_OAUTH.read_bytes()


def test_antigravity_json_import_catalog_and_inference(client):
    response = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("oauth.json", _antigravity_oauth_bytes(), "application/json")},
        data={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["created"]) == 1
    assert body["created"][0]["account_id"] == "antigravity:user@example.com"
    assert body["created"][0]["provider"] == "antigravity"
    created = client.post(
        "/admin/api/keys",
        json={"name": "antigravity-picker", "preferred_account_id": "antigravity:user@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    key = created.json()["key"]
    openai_list = client.get("/v1/models", headers=key_headers(key)).json()["data"]
    openai_ids = {item["id"] for item in openai_list}
    assert {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.1-pro",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
    } <= openai_ids
    assert "gemini-3-flash" not in openai_ids
    assert "gemini-3.1-flash-image" in openai_ids
    assert "claude-opus-4-6" not in openai_ids
    assert "gpt-oss-120b-medium" not in openai_ids
    picker = client.get(
        "/v1/models",
        params={"client_version": "0.154.0"},
        headers=key_headers(key),
    )
    assert picker.status_code == 200, picker.text
    slugs = {item["slug"] for item in picker.json()["models"]}
    assert slugs == {
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.1-pro",
        "claude-sonnet-4-6",
        "claude-opus-4-6-thinking",
    }
    for item in picker.json()["models"]:
        if item["slug"].startswith("gemini-"):
            assert item["experimental_supported_tools"] == ["image_generation"]
        else:
            assert item["experimental_supported_tools"] == []
        assert item["slug"] != "gemini-3.1-flash-image"
    captured: list[str] = []
    stream_chunk = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "antigravity" / "cloudcode_stream_text_chunk.json").read_text(
            encoding="utf-8"
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url.host)
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, json=stream_chunk)
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    chat = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert chat.status_code == 200, chat.text
    responses = client.post(
        "/v1/responses",
        json={"model": "gemini-3.8-flash", "input": "hi"},
        headers=key_headers(key),
    )
    assert responses.status_code == 200, responses.text
    assert "daily-cloudcode-pa.googleapis.com" in captured


def test_import_local_antigravity(client, tmp_path, monkeypatch):
    from app.config import settings
    from app.providers import antigravity as antigravity_mod

    oauth_dir = tmp_path / "antigravity"
    oauth_dir.mkdir()
    (oauth_dir / "oauth.json").write_bytes(_antigravity_oauth_bytes())
    monkeypatch.setattr(antigravity_mod, "local_oauth_roots", lambda: [oauth_dir])
    monkeypatch.setattr(antigravity_mod, "windows_credential_payloads", lambda: [])
    monkeypatch.setattr(settings, "gateway_import_local_enabled", True)
    response = client.post(
        "/admin/api/accounts/import-local",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    created = response.json()["created"]
    assert created[0]["account_id"] == "antigravity:user@example.com"
    assert created[0]["label"] == "user@example.com"
    stored = parse_oauth_payload(json.loads(_antigravity_oauth_bytes()), filename="oauth.json")
    assert stored[0].payload["accessToken"].startswith("ya29.example")


def test_import_local_antigravity_from_windows_credman(client, tmp_path, monkeypatch):
    from app.config import settings
    from app.providers import antigravity as antigravity_mod

    monkeypatch.setattr(antigravity_mod, "local_oauth_roots", lambda: [tmp_path / "missing-antigravity"])
    monkeypatch.setattr(
        antigravity_mod,
        "windows_credential_payloads",
        lambda: [
            {
                "id_token": (
                    "eyJhbGciOiJub25lIn0."
                    "eyJlbWFpbCI6InVzZXJAZXhhbXBsZS5jb20ifQ."
                    "x"
                ),
                "token": {
                    "access_token": "ya29.example-access-token",
                    "refresh_token": "1//0example-refresh-token",
                    "expiry": "2099-01-01T00:00:00Z",
                },
            }
        ],
    )
    monkeypatch.setattr(settings, "gateway_import_local_enabled", True)
    response = client.post(
        "/admin/api/accounts/import-local",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    created = response.json()["created"]
    assert created[0]["account_id"] == "antigravity:user@example.com"


def test_import_local_antigravity_missing(client, tmp_path, monkeypatch):
    from app.config import settings
    from app.providers import antigravity as antigravity_mod

    monkeypatch.setattr(antigravity_mod, "local_oauth_roots", lambda: [tmp_path / "missing-antigravity"])
    monkeypatch.setattr(antigravity_mod, "windows_credential_payloads", lambda: [])
    monkeypatch.setattr(settings, "gateway_import_local_enabled", True)
    response = client.post(
        "/admin/api/accounts/import-local",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "local_auth_missing"
    message = error["message"].lower()
    assert "oauth" in message
    assert "ya29" not in message
    assert "1//" not in message


def test_grok_chat_uses_cli_headers_and_falls_back_to_responses(client):
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-user", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/chat/completions"):
            assert request.headers.get("X-XAI-Token-Auth") == "xai-grok-cli"
            assert request.headers.get("x-grok-client-version")
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse("resp-grok"),
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert any(path.endswith("/chat/completions") for path in seen)
    assert any("/responses" in path for path in seen)
    assert response.json()["choices"][0]["message"]["content"] == "OK"
    _ = imported


def test_grok_upstream_rejection_logs_sanitized_metadata(client, caplog):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-diagnostics", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "message": "private upstream detail should not be logged",
                    "type": "invalid_request_error",
                    "code": "invalid_value",
                    "param": "input[2].type",
                }
            },
        )

    caplog.set_level(logging.WARNING)
    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                }
            ],
        },
        headers=key_headers(key),
    )

    assert response.status_code == 422
    assert "grok_upstream_rejected status=422" in caplog.text
    assert "error_type=invalid_request_error" in caplog.text
    assert "error_code=invalid_value" in caplog.text
    assert "error_param=input[2].type" in caplog.text
    assert "reject_hint=other" in caplog.text
    assert '"item_types":{"message":1}' in caplog.text
    assert "private upstream detail" not in caplog.text


def test_grok_streaming_responses_maps_4xx_to_codex_failed_sse(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-failed-sse", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"error": {"message": "unknown item type", "code": "invalid_value"}},
        )

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "response.failed" in response.text
    assert "invalid_prompt" in response.text
    assert "unknown item type" not in response.text


def test_grok_tool_search_cache_replays_discovered_tools_on_later_turns(client):
    from app.providers.grok import TOOL_SEARCH_WIRE_NAME

    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-tool-cache", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-cache"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    session_headers = {**key_headers(key), "X-Session-ID": "sess-tool-cache"}
    first = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {"type": "tool_search"},
                {
                    "type": "additional_tools",
                    "tools": [{"type": "tool_search"}],
                },
                {
                    "type": "tool_search_call",
                    "call_id": "call_search",
                    "arguments": {"query": "workspace"},
                },
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "codex_app",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "load_workspace_dependencies",
                                    "parameters": {"type": "object", "properties": {}},
                                }
                            ],
                        }
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]},
            ],
        },
        headers=session_headers,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
            ],
        },
        headers=session_headers,
    )
    assert second.status_code == 200, second.text
    assert len(captured) == 2
    first_names = [tool.get("name") or tool.get("type") for tool in captured[0].get("tools") or []]
    second_names = [tool.get("name") or tool.get("type") for tool in captured[1].get("tools") or []]
    assert TOOL_SEARCH_WIRE_NAME in first_names
    assert "load_workspace_dependencies" in first_names
    assert TOOL_SEARCH_WIRE_NAME in second_names
    assert "load_workspace_dependencies" in second_names


def test_grok_tool_search_cache_does_not_leak_across_sessions(client):
    from app.providers.grok import TOOL_SEARCH_WIRE_NAME

    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-tool-cache-iso", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-cache-iso"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    seed = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
                        {
                            "type": "function",
                            "name": "load_workspace_dependencies",
                            "parameters": {"type": "object", "properties": {}},
                        }
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "seed"}]},
            ],
        },
        headers={**key_headers(key), "X-Session-ID": "sess-a"},
    )
    assert seed.status_code == 200, seed.text
    other = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "other"}]},
            ],
        },
        headers={**key_headers(key), "X-Session-ID": "sess-b"},
    )
    assert other.status_code == 200, other.text
    other_names = [tool.get("name") or tool.get("type") for tool in captured[1].get("tools") or []]
    assert "load_workspace_dependencies" not in other_names
    assert TOOL_SEARCH_WIRE_NAME not in other_names


def test_cross_provider_does_not_failover(client):
    import_pool(client, auth_payload("acct-codex-a"), auth_payload("acct-codex-b"))
    grok = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert grok.status_code == 200
    keys = client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"]
    key = keys[0]["key"]
    seen_accounts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/responses"):
            seen_accounts.append(request.headers.get("ChatGPT-Account-Id") or "grok")
        return httpx.Response(500, json={"error": {"message": "temporary"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert response.status_code == 502
    assert "grok" not in seen_accounts
    assert set(seen_accounts) <= {"acct-codex-a", "acct-codex-b"}


def test_video_job_is_bound_to_creating_account(client):
    client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(grok_cli_payload()), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    created = client.post(
        "/admin/api/keys",
        json={"name": "video", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/videos/generations"):
            return httpx.Response(200, json={"id": "vid-1", "status": "queued"})
        if request.url.path.endswith("/videos/vid-1"):
            return httpx.Response(200, json={"id": "vid-1", "status": "completed"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    created_job = client.post(
        "/v1/videos/generations",
        json={"model": "grok-imagine-video-1.5", "prompt": "a cat"},
        headers=key_headers(key),
    )
    assert created_job.status_code == 200, created_job.text
    assert created_job.json()["id"] == "vid-1"
    polled = client.get("/v1/videos/vid-1", headers=key_headers(key))
    assert polled.status_code == 200
    assert polled.json()["status"] == "completed"


def test_codex_video_is_unsupported(client):
    body = import_pool(client, auth_payload("acct-no-video"))
    key = body["generated_api_key"]["key"]
    response = client.post(
        "/v1/videos/generations",
        json={"model": "gpt-6-astra", "prompt": "nope"},
        headers=key_headers(key),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "unsupported_capability"


def test_gemini_model_without_antigravity_account_is_unavailable(client):
    body = import_pool(client, auth_payload("acct-text"))
    key = body["generated_api_key"]["key"]
    response = client.post(
        "/v1/chat/completions",
        json={"model": "gemini-3-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_healthy_accounts"


def _import_grok(client, payload: dict) -> None:
    response = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(payload), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text


def test_grok_chat_records_usage_from_final_sse_frame(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-usage", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_chat_sse(),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.6",
            "messages": [{"role": "user", "content": "hi"}],
            "stream_options": {"foo": True},
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "usage" in response.json()
    assert response.json()["usage"]["total_tokens"] == 18
    assert bodies[0]["model"] == "grok-4.6"
    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"]["include_usage"] is True
    assert bodies[0]["stream_options"]["foo"] is True


def test_grok_chat_missing_or_zero_usage_is_unknown(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-unknown", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    payloads = iter([
        grok_chat_sse(include_usage=False),
        grok_chat_sse(usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=next(payloads),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    first = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    second = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert first.status_code == 200 and second.status_code == 200
    assert "usage" not in first.json()
    assert "usage" not in second.json()


def test_grok_streaming_chat_records_usage(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-stream", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_chat_sse(),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "data:" in response.text


def test_grok_responses_sends_normalized_input_image(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-vision", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            upstream.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-vision"),
            )
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
                        {"type": "input_text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/cat.png", "detail": "high"},
                        },
                    ],
                }
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert upstream
    content = upstream[0]["input"][0]["content"]
    assert content[1] == {
        "type": "input_image",
        "image_url": "https://example.test/cat.png",
        "detail": "high",
    }


def test_grok_responses_inlines_file_id_images(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-file-vision", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    png = b"\x89PNG\r\n\x1a\n" + b"small-image"
    uploaded = client.post(
        "/v1/files",
        data={"purpose": "user_data"},
        files={"file": ("screen.png", png, "image/png")},
        headers=key_headers(key),
    )
    assert uploaded.status_code == 200, uploaded.text
    file_id = uploaded.json()["id"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            upstream.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-file-vision"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [{"type": "input_image", "file_id": file_id}],
            }],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert upstream
    block = upstream[0]["input"][0]["content"][0]
    assert block["type"] == "input_image"
    assert isinstance(block["image_url"], str)
    assert block["image_url"].startswith("data:image/png;base64,")
    assert file_id not in json.dumps(upstream[0])


def test_grok_chat_sends_nested_image_url(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-chat-vision", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    upstream: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            upstream.append(json.loads(request.content))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_chat_sse(),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.6",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "https://example.test/cat.png", "detail": "high"},
                        {"type": "text", "text": "describe"},
                    ],
                }
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert upstream
    assert upstream[0]["messages"][0]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.test/cat.png", "detail": "high"},
        },
        {"type": "text", "text": "describe"},
    ]


def test_grok_responses_normalizes_codex_image_blocks():
    from app.providers.grok import sanitize_grok_chat_payload, sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.test/cat.png",
                                "detail": "high",
                            },
                            "extra": "drop-me",
                        },
                        {"type": "input_image", "file_id": "file_missing"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/webp;base64,xx",
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    ],
                },
            ],
        }
    )
    first = payload["input"][0]["content"]
    assert first[0] == {"type": "input_text", "text": "what is this"}
    assert first[1] == {
        "type": "input_image",
        "image_url": "https://example.test/cat.png",
        "detail": "high",
    }
    assert "extra" not in first[1]
    assert first[2] == {"type": "input_text", "text": "[image omitted: unresolved file]"}
    assert first[3] == {"type": "input_text", "text": "[image omitted: unsupported format]"}
    second = payload["input"][1]
    assert second["type"] == "message"
    assert second["content"] == [
        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
    ]

    chat = sanitize_grok_chat_payload(
        {
            "model": "grok-4.6",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": "https://example.test/cat.png", "detail": "high"},
                        {"type": "input_text", "text": "describe"},
                    ],
                }
            ],
        }
    )
    assert chat["messages"][0]["content"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://example.test/cat.png", "detail": "high"},
        },
        {"type": "text", "text": "describe"},
    ]

    with_tool_image = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_view",
                    "name": "view_image",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_view",
                    "output": [
                        {"type": "input_image", "image_url": "data:image/png;base64,abcd"},
                        {"type": "input_text", "text": "ok"},
                    ],
                },
            ],
        }
    )
    types = [item["type"] for item in with_tool_image["input"]]
    assert types == ["function_call", "function_call_output", "message"]
    assert with_tool_image["input"][1]["output"] == "[image]\nok"
    assert with_tool_image["input"][2]["content"][1]["image_url"] == "data:image/png;base64,abcd"


def test_grok_responses_maps_codex_additional_tools():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "include": ["reasoning.encrypted_content", "web_search_call_output"],
            "service_tier": "priority",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "web_search_preview"},
                        {"type": "local_shell"},
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        },
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert [item.get("type") for item in payload["input"]] == ["message"]
    assert payload["tools"][0] == {"type": "web_search"}
    assert payload["tools"][1]["name"] == "local_shell"
    assert payload["tools"][1]["parameters"]["required"] == ["command"]
    assert "[exit code:" in payload["tools"][1]["description"]
    assert payload["tools"][2] == {
        "type": "function",
        "name": "lookup",
        "parameters": {"type": "object", "properties": {}},
    }
    assert payload.get("include") == [
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    ]
    assert "service_tier" not in payload


def test_grok_responses_drops_xai_unsupported_fields_and_bad_tool_schemas():
    from app.providers.grok import grok_reject_hint, sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "metadata": {"session_id": "sess-should-drop"},
            "include": [
                "reasoning.encrypted_content",
                "web_search_call_output",
                "verbose_streaming",
                "not_a_real_include",
            ],
            "tools": [
                {"type": "web_search"},
                {"type": "file_search"},
                {
                    "type": "file_search",
                    "vector_store_ids": ["vs_keep"],
                },
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "x",
                    "parameters": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {"q": {"type": "string"}},
                            },
                            {"type": "null"},
                            {"type": "string"},
                        ]
                    },
                },
            ],
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert "metadata" not in payload
    assert payload.get("include") == [
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    ]
    assert payload["tools"][0] == {"type": "web_search"}
    assert payload["tools"][1] == {"type": "file_search", "vector_store_ids": ["vs_keep"]}
    assert payload["tools"][2]["name"] == "lookup"
    assert payload["tools"][2]["parameters"] == {
        "type": "object",
        "properties": {"q": {"type": "string"}},
    }
    mixed = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "tools": [
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "description": "x",
                    "parameters": {
                        "type": "object",
                        "properties": {"task": {"type": "string"}},
                        "anyOf": [
                            {"required": ["task"]},
                            {"type": "null"},
                        ],
                    },
                }
            ],
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert mixed["tools"][0]["parameters"] == {
        "type": "object",
        "properties": {"task": {"type": "string"}},
    }
    assert grok_reject_hint('Argument not supported: "web_search_call_output" in "include" field') == (
        "unsupported_argument:web_search_call_output"
    )
    assert grok_reject_hint("Argument not supported: metadata") == "unsupported_argument:metadata"
    assert grok_reject_hint(
        "lookup: tool parameter root must be an object type"
    ) == "tool_parameter_root:lookup"
    assert grok_reject_hint("private upstream detail should not be logged") == "other"


def test_grok_responses_maps_codex_freeform_and_shell_command():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "freeform",
                            "name": "apply_patch",
                            "description": "edit files",
                            "format": {"type": "grammar", "syntax": "lark"},
                        },
                        {
                            "type": "function",
                            "name": "shell_command",
                            "parameters": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        },
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0] == {
        "type": "function",
        "name": "apply_patch",
        "description": "edit files",
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": (
                        "Freeform tool input. For apply_patch this is the full patch document "
                        "including *** Begin Patch / *** Update File / *** Add File / *** Delete File."
                    ),
                }
            },
            "required": ["input"],
        },
    }
    assert payload["tools"][1]["name"] == "shell_command"
    assert payload["tools"][1]["parameters"] == {
        "type": "object",
        "properties": {"command": {"type": "string"}},
    }
    assert "[exit code:" in payload["tools"][1]["description"]


def test_grok_responses_drops_null_reasoning_content():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "summary": [{"type": "summary_text", "text": "plan"}],
                    "content": None,
                    "encrypted_content": None,
                },
                {
                    "type": "reasoning",
                    "id": "rs_2",
                    "content": [{"type": "reasoning_text", "text": "kept"}],
                },
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
            ],
        }
    )
    items = payload["input"]
    assert items[1]["type"] == "reasoning"
    assert "content" not in items[1]
    assert "encrypted_content" not in items[1]
    assert items[1]["summary"] == [{"type": "summary_text", "text": "plan"}]
    assert items[2]["content"] == [{"type": "reasoning_text", "text": "kept"}]


def test_grok_responses_converts_custom_tool_history_and_namespaces():
    from app.providers.grok import sanitize_grok_responses

    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "tool_choice": "auto",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "mcp__codex_app",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "read_file",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"path": {"type": "string"}},
                                        "required": ["path"],
                                    },
                                }
                            ],
                        },
                        {"type": "tool_search"},
                        {"type": "namespace", "name": "empty", "tools": []},
                    ],
                },
                {
                    "type": "custom_tool_call",
                    "id": "ctc_1",
                    "call_id": "call_1",
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_1",
                    "output": "Success",
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]},
            ],
            "tools": [
                {
                    "type": "freeform",
                    "name": "apply_patch",
                    "description": "edit files",
                }
            ],
        }
    )
    payload = result.payload
    assert result.freeform_tool_names == frozenset({"apply_patch"})
    assert [item["type"] for item in payload["input"]] == [
        "function_call",
        "function_call_output",
        "message",
    ]
    assert payload["input"][0]["arguments"] == json.dumps(
        {"input": "*** Begin Patch\n*** End Patch"}, ensure_ascii=False, separators=(",", ":")
    )
    assert payload["input"][1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "Success",
    }
    names = [tool.get("name") for tool in payload["tools"]]
    assert names == ["apply_patch", "read_file", "codex_tool_search"]
    apply_patch = next(tool for tool in payload["tools"] if tool["name"] == "apply_patch")
    assert apply_patch["parameters"]["required"] == ["input"]


def test_grok_responses_stringifies_shell_results_and_strips_previous_id():
    from app.providers.grok import sanitize_grok_responses

    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "previous_response_id": "resp_should_drop",
            "conversation": {"id": "conv_1"},
            "service_tier": "priority",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "write it"}]},
                {
                    "type": "function_call_output",
                    "call_id": "call_shell",
                    "output": [
                        {"type": "input_text", "text": "Exit code: 0"},
                        {"type": "output_text", "text": "我爱你"},
                    ],
                },
                {
                    "type": "shell_call",
                    "call_id": "call_shell",
                    "action": {"command": "Set-Content 111.md", "working_directory": "D:/Users/DELL/Desktop"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_empty",
                    "status": "completed",
                    "exit_code": 0,
                    "output": "",
                },
                {
                    "type": "function_call",
                    "call_id": "call_empty",
                    "name": "shell_command",
                    "arguments": {"command": "Get-Item 111.md"},
                },
                {
                    "type": "function_call",
                    "call_id": "call_prose",
                    "name": "shell_command",
                    "arguments": {"command": "Get-Content 111.md"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_prose",
                    "output": "Exit code: 0\nWall time: 0.3 seconds\nOutput:\n我爱你\n",
                },
            ],
        }
    )
    payload = result.payload
    assert "previous_response_id" not in payload
    assert "conversation" not in payload
    assert "service_tier" not in payload
    types = [item["type"] for item in payload["input"]]
    assert types == [
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
    ]
    assert payload["input"][1]["name"] == "shell_command"
    assert '"command":"Set-Content 111.md"' in payload["input"][1]["arguments"]
    shell_out = payload["input"][2]["output"]
    assert "I executed a terminal command: `Set-Content 111.md`" in shell_out
    assert "我爱你" in shell_out
    assert "[exit code: 0]" in shell_out
    assert payload["input"][3]["arguments"] == json.dumps(
        {"command": "Get-Item 111.md"}, ensure_ascii=False, separators=(",", ":")
    )
    empty_out = payload["input"][4]["output"]
    assert empty_out != ""
    assert "I executed a terminal command: `Get-Item 111.md`" in empty_out
    assert "[exit code: 0]" in empty_out
    prose_out = payload["input"][6]["output"]
    assert "I executed a terminal command: `Get-Content 111.md`" in prose_out
    assert "我爱你" in prose_out
    assert "[exit code: 0]" in prose_out


def test_grok_responses_maps_codex_client_tool_history():
    from app.providers.grok import sanitize_grok_responses

    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "input": [
                {
                    "type": "apply_patch_call",
                    "call_id": "call_patch",
                    "operation": {
                        "type": "add_file",
                        "path": "111.md",
                        "contents": "我爱你",
                    },
                },
                {
                    "type": "apply_patch_call_output",
                    "call_id": "call_patch",
                    "status": "completed",
                    "output": "",
                },
                {
                    "type": "shell_call",
                    "call_id": "call_embedded",
                    "status": "completed",
                    "action": {"command": ["Set-Content", "111.md"]},
                    "output": [
                        {
                            "stdout": "",
                            "stderr": "",
                            "outcome": {"type": "exit", "exit_code": 0},
                        }
                    ],
                },
                {
                    "type": "computer_call",
                    "call_id": "call_computer",
                    "action": {"type": "click", "x": 10, "y": 20},
                },
                {
                    "type": "computer_call_output",
                    "call_id": "call_computer",
                    "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,xx"},
                },
                {
                    "type": "mcp_call",
                    "call_id": "call_mcp",
                    "name": "mcp__weather__get-forecast",
                    "arguments": {"city": "SF"},
                },
                {
                    "type": "mcp_call_output",
                    "call_id": "call_mcp",
                    "output": {
                        "content": [{"type": "text", "text": "sunny"}],
                        "structuredContent": {"temp": 59},
                        "isError": False,
                    },
                },
                {"type": "context_compaction", "id": "cmp_1"},
                {"type": "item_reference", "id": "msg_old"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]},
            ],
            "tools": [
                {"type": "computer"},
                {"type": "custom", "name": "exec", "description": "run"},
            ],
        }
    )
    payload = result.payload
    types = [item["type"] for item in payload["input"]]
    assert types == [
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    patch_call = payload["input"][0]
    assert patch_call["name"] == "apply_patch"
    assert "*** Add File: 111.md" in json.loads(patch_call["arguments"])["input"]
    assert payload["input"][1]["output"] == "Success"
    embedded = payload["input"][2]
    assert embedded["name"] == "shell_command"
    assert "[exit code: 0]" in payload["input"][3]["output"]
    assert "I executed a terminal command:" in payload["input"][3]["output"]
    assert payload["input"][4]["name"] == "computer"
    assert json.loads(payload["input"][4]["arguments"])["action"]["type"] == "click"
    assert payload["input"][5]["output"] == "[computer screenshot]"
    screenshot = payload["input"][6]
    assert screenshot["role"] == "user"
    assert screenshot["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,xx",
        "detail": "high",
    }
    assert payload["input"][7]["name"] == "mcp__weather__get-forecast"
    mcp_out = payload["input"][8]["output"]
    assert "sunny" in mcp_out
    assert "59" in mcp_out
    tool_names = [tool.get("name") or tool.get("type") for tool in payload["tools"]]
    assert "computer" in tool_names
    assert "exec" in tool_names


def test_grok_maps_exec_custom_tool_empty_output():
    from app.providers.grok import sanitize_grok_responses

    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "custom", "name": "exec", "description": "Runs a command"},
                    ],
                },
                {
                    "type": "custom_tool_call",
                    "call_id": "c1",
                    "name": "exec",
                    "input": "Set-Content -LiteralPath 'D:\\Users\\DELL\\Desktop\\111.md' -Value '我爱你'",
                },
                {"type": "custom_tool_call_output", "call_id": "c1", "output": ""},
                {
                    "type": "custom_tool_call",
                    "call_id": "c2",
                    "name": "exec",
                    "input": "Get-Content 111.md",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "c2",
                    "output": "Exit code: 0\nWall time: 0.2 seconds\nOutput:\n我爱你\n",
                },
            ],
        }
    )
    payload = result.payload
    exec_tool = next(tool for tool in payload["tools"] if tool["name"] == "exec")
    assert exec_tool["name"] == "exec"
    assert exec_tool["parameters"]["properties"]["input"]["type"] == "string"
    assert "[exit code:" not in (exec_tool.get("description") or "")
    assert result.freeform_tool_names == frozenset({"exec"})
    empty = payload["input"][1]["output"]
    assert empty == ""
    filled = payload["input"][3]["output"]
    assert "我爱你" in filled
    assert "Command completed (no captured stdout)." not in filled
    assert "[exit code: 0]" in filled


def test_grok_maps_exec_command_keeps_cmd_and_desktop_output():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "reasoning": {"effort": "xhigh"},
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Runs a command",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "cmd": {"type": "string"},
                            "workdir": {"type": "string"},
                            "yield_time_ms": {"type": "number"},
                        },
                        "required": ["cmd"],
                    },
                },
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
                },
                {"type": "custom", "name": "exec", "description": "JS exec"},
            ],
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_cmd",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "ls", "workdir": "C:\\\\work"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_cmd",
                    "output": (
                        "Chunk ID: 1526b8\n"
                        "Wall time: 0.3609 seconds\n"
                        "Process exited with code 0\n"
                        "Original token count: 4\n"
                        "Output:\n"
                        "ok\n"
                    ),
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    names = [tool.get("name") for tool in payload["tools"]]
    assert names == ["exec_command", "spawn_agent", "exec"]
    exec_command = payload["tools"][0]
    assert exec_command["parameters"]["properties"]["cmd"]["type"] == "string"
    assert "command" not in exec_command["parameters"]["properties"]
    assert "[exit code:" in exec_command["description"]
    js_exec = payload["tools"][2]
    assert js_exec["parameters"]["properties"]["input"]["type"] == "string"
    assert payload["reasoning"]["effort"] == "xhigh"
    out = payload["input"][1]["output"]
    assert "I executed a terminal command: `ls`" in out
    assert "ok" in out
    assert "[exit code: 0]" in out


def test_grok_maps_empty_exec_command_schema_to_cmd():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6-build",
            "tools": [{"type": "function", "name": "exec_command"}],
            "input": "hi",
        }
    )
    tool = payload["tools"][0]
    assert tool["parameters"]["required"] == ["cmd"]
    assert "cmd" in tool["parameters"]["properties"]
    assert payload["model"] == "grok-4.6-build"


def test_grok_does_not_inject_reasoning_when_client_omits_it():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload({"model": "grok-4.6", "input": "hi"})
    assert "reasoning" not in payload


def test_grok_maps_exec_crlf_bom_empty_header():
    from app.providers.grok import sanitize_grok_responses

    header = "\ufeffExit code: 0\nWall time: 0.3 seconds\nOutput:\n"
    assert len(header.encode("utf-8")) == 47
    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "c1",
                    "name": "exec",
                    "input": "Get-Content 111.md",
                },
                {"type": "custom_tool_call_output", "call_id": "c1", "output": header},
            ],
        }
    )
    text = result.payload["input"][1]["output"]
    assert "Command completed (no captured stdout)." in text
    assert "[exit code: 0]" in text


def test_grok_maps_exec_mojibake_bom_and_crlf_header():
    from app.providers.grok import sanitize_grok_responses

    header = "Exit code: 0\r\nWall time: 0.3 seconds\r\nOutput:\r\n"
    assert len(header.encode("utf-8")) == 47
    mojibake = "ï»¿" + "Exit code: 0\nWall time: 0.3 seconds\nOutput:\n"
    for blob in (header, mojibake):
        result = sanitize_grok_responses(
            {
                "model": "grok-4.6",
                "input": [
                    {
                        "type": "custom_tool_call",
                        "call_id": "c1",
                        "name": "exec",
                        "input": "Get-Content 111.md",
                    },
                    {"type": "custom_tool_call_output", "call_id": "c1", "output": blob},
                ],
            }
        )
        text = result.payload["input"][1]["output"]
        assert "Command completed (no captured stdout)." in text, blob[:20]
        assert "[exit code: 0]" in text


def test_grok_rewrites_function_call_sse_to_custom_tool_call():
    from app.providers.grok import rewrite_grok_codex_sse_payload

    names = frozenset({"apply_patch"})
    state: dict = {}
    added = rewrite_grok_codex_sse_payload(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "apply_patch",
                "arguments": "",
            },
        },
        names,
        state,
    )
    assert added[0][1]["item"]["type"] == "custom_tool_call"
    assert added[0][1]["item"]["input"] == ""
    assert rewrite_grok_codex_sse_payload(
        "response.function_call_arguments.delta",
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "delta": "{\"input\":\"*** Begin Patch\"}",
        },
        names,
        state,
    ) == []
    done = rewrite_grok_codex_sse_payload(
        "response.function_call_arguments.done",
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "arguments": json.dumps({"input": "*** Begin Patch\n*** End Patch"}),
        },
        names,
        state,
    )
    assert [event for event, _payload in done] == [
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
    ]
    assert done[1][1]["input"] == "*** Begin Patch\n*** End Patch"
    completed = rewrite_grok_codex_sse_payload(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "apply_patch",
                        "arguments": json.dumps({"input": "*** Begin Patch\n*** End Patch"}),
                    }
                ],
            },
        },
        names,
        state,
    )
    assert completed[0][1]["response"]["output"][0]["type"] == "custom_tool_call"
    assert completed[0][1]["response"]["output"][0]["input"] == "*** Begin Patch\n*** End Patch"


def test_grok_responses_lowers_tool_search_and_injects_discovered_tools():
    from app.providers.grok import TOOL_SEARCH_WIRE_NAME, sanitize_grok_responses

    result = sanitize_grok_responses(
        {
            "model": "grok-4.6",
            "tool_choice": {"type": "tool_search"},
            "include": ["reasoning.encrypted_content"],
            "reasoning": {"effort": "xhigh"},
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "tool_search", "description": "find tools"},
                        {
                            "type": "namespace",
                            "name": "browser",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "open",
                                    "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
                                }
                            ],
                        },
                    ],
                },
                {
                    "type": "tool_search_call",
                    "call_id": "call_search",
                    "arguments": {"query": "workspace"},
                },
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "codex_app",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "load_workspace_dependencies",
                                    "parameters": {"type": "object", "properties": {}},
                                }
                            ],
                        }
                    ],
                },
                {"type": "compaction_trigger"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "again"}]},
            ],
        }
    )
    payload = result.payload
    assert result.compact_v2 is True
    assert result.tool_search_wire_name == TOOL_SEARCH_WIRE_NAME
    assert result.namespace_map["open"] == "browser"
    assert result.namespace_map["load_workspace_dependencies"] == "codex_app"
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["reasoning"] == {"summary": "concise", "effort": "xhigh"}
    types = [item["type"] for item in payload["input"]]
    assert types == ["function_call", "function_call_output", "message"]
    assert payload["input"][0]["name"] == TOOL_SEARCH_WIRE_NAME
    assert json.loads(payload["input"][0]["arguments"]) == {"query": "workspace"}
    names = [tool.get("name") or tool.get("type") for tool in payload["tools"]]
    assert names == [TOOL_SEARCH_WIRE_NAME, "open", "load_workspace_dependencies"]
    assert payload["tool_choice"] == "required"


def test_grok_rewrites_tool_search_and_namespace_sse():
    from app.providers.grok import GrokRewriteSpec, TOOL_SEARCH_WIRE_NAME, rewrite_grok_codex_sse_payload

    spec = GrokRewriteSpec(
        tool_search_wire_name=TOOL_SEARCH_WIRE_NAME,
        namespace_map={"open": "browser"},
    )
    state: dict = {}
    added = rewrite_grok_codex_sse_payload(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_search",
                "call_id": "call_search",
                "name": TOOL_SEARCH_WIRE_NAME,
                "arguments": "",
            },
        },
        spec,
        state,
    )
    assert added[0][1]["item"]["type"] == "tool_search_call"
    assert added[0][1]["item"]["execution"] == "client"
    assert rewrite_grok_codex_sse_payload(
        "response.function_call_arguments.delta",
        {"type": "response.function_call_arguments.delta", "item_id": "fc_search", "delta": "{\"query\":\"x\"}"},
        spec,
        state,
    ) == []
    done = rewrite_grok_codex_sse_payload(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_search",
                "call_id": "call_search",
                "name": TOOL_SEARCH_WIRE_NAME,
                "arguments": json.dumps({"query": "workspace"}),
            },
        },
        spec,
        state,
    )
    assert done[0][1]["item"]["type"] == "tool_search_call"
    assert done[0][1]["item"]["arguments"] == {"query": "workspace"}
    namespaced = rewrite_grok_codex_sse_payload(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "fc_open",
                "call_id": "call_open",
                "name": "open",
                "arguments": json.dumps({"url": "https://example.com"}),
            },
        },
        spec,
        state,
    )
    assert namespaced[0][1]["item"]["type"] == "function_call"
    assert namespaced[0][1]["item"]["namespace"] == "browser"


def test_grok_compact_v2_sse_has_single_compaction_item():
    from app.providers.grok import COMPACT_SUMMARY_PREFIX, grok_compact_encrypted_content, grok_compact_v2_sse

    text = grok_compact_encrypted_content("Keep the auth changes and run tests next.")
    assert text.startswith(COMPACT_SUMMARY_PREFIX)
    sse = grok_compact_v2_sse(response_id="resp_compact_test", encrypted_content=text)
    assert sse.count("type=compaction") == 0
    assert sse.count('"type": "compaction"') == 2
    assert "response.created" in sse
    assert "response.output_item.done" in sse
    assert "response.completed" in sse
    failed = grok_compact_v2_sse(
        response_id="resp_compact_fail",
        failed_code="invalid_prompt",
        failed_message="missing history",
    )
    assert "response.failed" in failed
    assert "invalid_prompt" in failed


def test_grok_responses_omits_tool_choice_without_tools():
    from app.providers.grok import sanitize_grok_responses_payload

    payload = sanitize_grok_responses_payload(
        {
            "model": "grok-4.6",
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "input": [
                {"type": "additional_tools", "tools": [{"type": "unknown_builtin"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ],
        }
    )
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "parallel_tool_calls" not in payload
    assert [item.get("type") for item in payload["input"]] == ["message"]


def test_grok_responses_strips_codex_additional_tools(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-tools", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-grok-tools"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured
    body = captured[0]
    assert body["model"] == "grok-4.6"
    assert all(item.get("type") != "additional_tools" for item in body["input"])
    assert body["input"][0]["type"] == "message"
    assert body["tools"][0]["name"] == "shell"
    assert "reasoning.encrypted_content" in (body.get("include") or [])


def _grok_responses_capture(client, account_payload: dict, *, fast_enabled: bool, account_id: str):
    _import_grok(client, account_payload)
    created = client.post(
        "/admin/api/keys",
        json={
            "name": "grok-fast",
            "preferred_account_id": account_id,
            "fast_enabled": fast_enabled,
        },
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(f"resp-grok-fast-{len(captured)}"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    return key, captured


def test_grok_oauth_fast_enabled_injects_priority(client):
    key, captured = _grok_responses_capture(
        client, grok_cli_payload(), fast_enabled=True, account_id="grok:user-grok-1"
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "service_tier": "fast",
            "input": "hi",
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured[0]["service_tier"] == "priority"


def test_grok_oauth_fast_disabled_omits_service_tier(client):
    key, captured = _grok_responses_capture(
        client, grok_cli_payload(), fast_enabled=False, account_id="grok:user-grok-1"
    )
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "service_tier": "priority",
            "input": "hi",
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "service_tier" not in captured[0]


def test_grok_api_key_fast_enabled_still_omits_service_tier(client):
    key, captured = _grok_responses_capture(
        client,
        {"XAI_API_KEY": "xai-abcdef123"},
        fast_enabled=True,
        account_id="grok:key:abcdef12",
    )
    response = client.post(
        "/v1/responses",
        json={"model": "grok-4.6", "service_tier": "priority", "input": "hi"},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "service_tier" not in captured[0]


def test_grok_tool_cache_does_not_reinsert_unused_desktop_tools(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-cache", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse(f"resp-cache-{len(captured)}"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    headers = {**key_headers(key), "X-Session-Id": "sess-tool-cache"}
    first = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
                {"type": "function", "name": "apply_patch", "parameters": {"type": "object", "properties": {"input": {"type": "string"}}}},
                {"type": "function", "name": "spawn_agent", "parameters": {"type": "object", "properties": {"task": {"type": "string"}}}},
                {"type": "function", "name": "list_mcp_resources", "parameters": {"type": "object", "properties": {}}},
            ],
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "ls"}),
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "first"}]},
            ],
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
                {"type": "custom", "name": "apply_patch"},
            ],
            "input": [
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "ls"}),
                },
                {"type": "function_call_output", "call_id": "c1", "output": "ok"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "second"}]},
            ],
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert len(captured) == 2
    first_names = [tool.get("name") for tool in captured[0]["tools"]]
    second_names = [tool.get("name") for tool in captured[1]["tools"]]
    assert "spawn_agent" in first_names
    assert "list_mcp_resources" in first_names
    assert "exec_command" in second_names
    assert "apply_patch" in second_names
    assert "spawn_agent" not in second_names
    assert "list_mcp_resources" not in second_names


def test_grok_responses_compacts_v2_into_single_compaction_item(client):
    from app.providers.grok import COMPACT_SUMMARY_PREFIX, COMPACT_SUMMARIZATION_PROMPT

    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-compact", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []
    summary = "Progress so far is the login form. Next Step: write tests for auth."

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            body = {
                "type": "response.completed",
                "response": {
                    "id": "resp-sum",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": summary}],
                        }
                    ],
                    "usage": {"input_tokens": 8, "output_tokens": 4, "total_tokens": 12},
                },
            }
            content = (
                'event: response.created\ndata: {"type":"response.created","response":{"id":"resp-sum","output":[]}}\n\n'
                f"event: response.completed\ndata: {json.dumps(body)}\n\n"
            )
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=content.encode())
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "implement login"}]},
                {"type": "compaction_trigger"},
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    text = response.text
    assert '"type": "compaction"' in text
    assert COMPACT_SUMMARY_PREFIX in text
    assert summary in text
    assert "response.failed" not in text
    assert captured
    upstream = captured[0]
    assert all(item.get("type") != "compaction_trigger" for item in upstream["input"])
    assert "tools" not in upstream
    prompt = upstream["input"][-1]["content"][0]["text"]
    assert prompt == COMPACT_SUMMARIZATION_PROMPT


def test_grok_responses_compact_without_history_fails_closed(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-compact-empty", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"path": request.url.path})
        return httpx.Response(500, json={"error": {"message": "should not call upstream"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [{"type": "compaction_trigger"}],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert "invalid_prompt" in response.text
    assert "response.failed" in response.text
    assert captured == []


def test_grok_responses_rewrites_apply_patch_function_call_for_codex(client):
    _import_grok(client, grok_cli_payload())
    created = client.post(
        "/admin/api/keys",
        json={"name": "grok-patch", "preferred_account_id": "grok:user-grok-1"},
        headers=ADMIN_HEADERS,
    )
    key = created.json()["key"]
    captured: list[dict] = []
    patch = "*** Begin Patch\n*** Add File: a.txt\n+hi\n*** End Patch"
    arguments = json.dumps({"input": patch})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/responses"):
            captured.append(json.loads(request.content.decode("utf-8")))
            blocks = [
                ("response.created", {"type": "response.created", "response": {"id": "resp-patch", "model": "grok-4.6"}}),
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "apply_patch",
                            "arguments": "",
                        },
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": "fc_1",
                        "arguments": arguments,
                    },
                ),
                (
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "apply_patch",
                            "arguments": arguments,
                        },
                    },
                ),
                (
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-patch",
                            "model": "grok-4.6",
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "fc_1",
                                    "call_id": "call_1",
                                    "name": "apply_patch",
                                    "arguments": arguments,
                                }
                            ],
                            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
                        },
                    },
                ),
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(
                    f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in blocks
                ).encode(),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/responses",
        json={
            "model": "grok-4.6",
            "stream": True,
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "freeform",
                            "name": "apply_patch",
                            "description": "edit files",
                        }
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "add a.txt"}],
                },
            ],
        },
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert captured[0]["tools"][0]["name"] == "apply_patch"
    assert captured[0]["tools"][0]["parameters"]["required"] == ["input"]
    assert "custom_tool_call" in response.text
    assert "custom_tool_call_input.done" in response.text
    assert "*** Begin Patch" in response.text
    assert "a.txt" in response.text
    assert '"type": "function_call"' not in response.text


def test_parse_grok_billing_snapshot_and_aliases():
    from app.providers.grok import parse_quota_snapshot

    snapshot = parse_quota_snapshot(GROK_BILLING_FIXTURE)
    assert snapshot is not None
    primary = snapshot["limits"][0]["primary"]
    assert snapshot["limits"][0]["limit_id"] == "grok"
    assert primary["used_percent"] == 38.0
    assert primary["remaining_percent"] == 62.0
    assert primary["window_minutes"] == 10_080
    assert primary["resets_at"] == int(
        dt.datetime.fromisoformat("2026-09-22T10:49:25.011346+00:00").timestamp()
    )
    assert snapshot["plan_type"] == "unknown"
    assert snapshot["quota_kind"] == "credits"
    ratio = parse_quota_snapshot(
        {"config": {"onDemandCap": {"val": 100}, "onDemandUsed": {"val": 25}}}
    )
    assert ratio is not None
    assert ratio["limits"][0]["primary"]["used_percent"] == 25.0
    assert parse_quota_snapshot({"config": {"onDemandCap": {"val": 0}}}) is None
    fresh = parse_quota_snapshot(
        {
            "config": {
                "currentPeriod": {
                    "start": "2026-09-15T10:49:25.011346+00:00",
                    "end": "2026-09-22T10:49:25.011346+00:00",
                },
                "onDemandCap": {"val": 0},
            }
        }
    )
    assert fresh is not None
    assert fresh["limits"][0]["primary"]["remaining_percent"] == 100.0
    counted = parse_quota_snapshot({"config": {"remainingCredits": 80, "totalCredits": 100}})
    assert counted is not None
    assert counted["limits"][0]["primary"]["remaining_percent"] == 80.0
    assert counted["limits"][0]["primary"]["remaining_amount"] == 80.0


def test_grok_oauth_quota_windows_and_api_key_metered(client):
    _import_grok(client, grok_cli_payload())
    client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("key.json", json.dumps({"XAI_API_KEY": "xai-abcdef123"}), "application/json")},
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-XAI-Token-Auth") == "xai-grok-cli"
        if request.url.path.endswith("/billing"):
            return httpx.Response(200, json=GROK_BILLING_FIXTURE)
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    by_id = {row["account_id"]: row for row in response.json()["data"]}
    oauth = by_id["grok:user-grok-1"]
    assert oauth["quota_kind"] == "credits"
    assert oauth["plan_type"] == "unknown"
    assert oauth["limits"][0]["primary"]["remaining_percent"] == 62.0
    assert oauth["reset_credits"] == {"available_count": 0, "credits": []}
    metered = next(row for row in by_id.values() if row["account_id"].startswith("grok:key:"))
    assert metered["quota_kind"] == "metered"
    assert metered["message"] == "API Key 按量计费"
    assert metered["limits"] == []


def test_grok_quota_failure_keeps_accounts_and_codex_windows(client):
    import_pool(client, auth_payload("acct-quota"))
    _import_grok(client, grok_cli_payload())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wham/usage"):
            return httpx.Response(
                200,
                json={
                    "plan_type": "plus",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 25,
                            "limit_window_seconds": 18_000,
                            "reset_at": 4_000_000_000,
                        }
                    },
                    "rate_limit_reset_credits": {"available_count": 0},
                },
            )
        if request.url.path.endswith("/wham/rate-limit-reset-credits"):
            return httpx.Response(200, json={"available_count": 0, "credits": []})
        if request.url.path.endswith("/billing"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    by_id = {row["account_id"]: row for row in response.json()["data"]}
    assert by_id["acct-quota"]["limits"][0]["limit_id"] == "codex"
    assert by_id["acct-quota"]["limits"][0]["primary"]["remaining_percent"] == 75.0
    grok = by_id["grok:user-grok-1"]
    assert grok["limits"] == []
    assert grok["message"] == "额度获取失败"
    assert grok["stale"] is True


def test_per_provider_pool_does_not_cross_failover(client, monkeypatch):
    import app.codex_gateway as gateway_module

    async def no_pause(_delay: float) -> None:
        return None

    monkeypatch.setattr(gateway_module, "_retry_pause", no_pause)
    client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", json.dumps(auth_payload("acct-codex-a")), "application/json")},
        headers=ADMIN_HEADERS,
    )
    _import_grok(client, grok_cli_payload("user-a", "a@example.com", token="token-user-a"))
    _import_grok(client, grok_cli_payload("user-b", "b@example.com", token="token-user-b"))
    created = client.post(
        "/admin/api/keys",
        json={"name": "multi"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    key = created.json()["key"]
    grok_seen: list[str] = []
    codex_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if request.url.path.endswith("/chat/completions"):
            grok_seen.append(auth)
            if "token-user-b" in auth:
                return httpx.Response(429, json={"error": {"message": "rate"}})
            if "token-user-a" in auth:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    content=grok_chat_sse(),
                )
            raise AssertionError(auth)
        if "backend-api/codex" in str(request.url) or request.url.path.endswith("/responses"):
            account = request.headers.get("ChatGPT-Account-Id")
            if account:
                codex_seen.append(account)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse("resp-codex"),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    grok = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert grok.status_code == 200, grok.text
    assert grok_seen
    assert all("token-user-a" in item or "token-user-b" in item for item in grok_seen)
    removed = client.delete("/admin/api/accounts/grok:user-b", headers=ADMIN_HEADERS)
    assert removed.status_code == 200
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert "grok:user-b" not in {row["account_id"] for row in listed}
    codex = client.post(
        "/v1/chat/completions",
        json={"model": "gpt-6-astra", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert codex.status_code == 200, codex.text
    assert set(codex_seen) <= {"acct-codex-a"}


def test_unconfigured_grok_route_picks_healthy_grok_account(client):
    import_pool(client, auth_payload("acct-codex-only"))
    _import_grok(client, grok_cli_payload())
    keys = client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"]
    key = keys[0]["key"]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            seen.append(request.headers.get("authorization", ""))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=grok_chat_sse(),
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/v1/chat/completions",
        json={"model": "grok-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=key_headers(key),
    )
    assert response.status_code == 200, response.text
    assert seen
    listed = client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"][0]
    grok_routes = [row for row in listed.get("routes") or [] if row["provider"] == "grok"]
    assert grok_routes == [] or grok_routes[0].get("preferred_account_id") in {None, "grok:user-grok-1"}
