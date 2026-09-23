from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def _live_client(tmp_path, monkeypatch):
    auth_path = Path(os.getenv("CODEX_LIVE_AUTH_PATH", ""))
    if os.getenv("CODEX_LIVE") != "1" or not auth_path.is_file():
        pytest.skip("set CODEX_LIVE=1 and CODEX_LIVE_AUTH_PATH")
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "admin_initial_password", "live-admin-password-2026")
    monkeypatch.setattr(settings, "routing_secret", "live-isolated-routing-secret")
    client = TestClient(app)
    client.__enter__()
    login = client.post(
        "/admin/api/auth/login",
        json={"username": "admin", "password": "live-admin-password-2026"},
        headers={"Origin": "http://testserver"},
    )
    admin_headers = {
        "Origin": "http://testserver",
        "X-CSRF-Token": login.json()["csrf_token"],
    }
    imported = client.post(
        "/admin/api/accounts/import",
        files={"files[]": ("auth.json", auth_path.read_bytes(), "application/json")},
        headers=admin_headers,
    )
    account_id = (imported.json()["created"] + imported.json()["updated"])[0]["account_id"]
    key = client.post(
        "/admin/api/keys",
        json={"name": "live", "preferred_account_id": account_id},
        headers=admin_headers,
    ).json()["key"]
    return client, {"Authorization": f"Bearer {key}"}


def test_live_lite_chat_plain_tools_history_and_stream(tmp_path, monkeypatch):
    auth_path = Path(os.getenv("CODEX_LIVE_AUTH_PATH", ""))
    if os.getenv("CODEX_LIVE") != "1" or not auth_path.is_file():
        pytest.skip("set CODEX_LIVE=1 and CODEX_LIVE_AUTH_PATH")

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "admin_initial_password", "live-admin-password-2026")
    monkeypatch.setattr(settings, "routing_secret", "live-isolated-routing-secret")
    with TestClient(app) as client:
        login = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": "live-admin-password-2026"},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200
        admin_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": login.json()["csrf_token"],
        }
        imported = client.post(
            "/admin/api/accounts/import",
            files={"files[]": ("auth.json", auth_path.read_bytes(), "application/json")},
            headers=admin_headers,
        )
        assert imported.status_code == 200
        account_id = (imported.json()["created"] + imported.json()["updated"])[0]["account_id"]
        created_key = client.post(
            "/admin/api/keys",
            json={"name": "live", "preferred_account_id": account_id},
            headers=admin_headers,
        )
        assert created_key.status_code == 200
        key = created_key.json()["key"]
        headers = {"Authorization": f"Bearer {key}"}

        plain = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.6-luna",
                "messages": [{"role": "user", "content": "Reply with OK only"}],
            },
            headers=headers,
            timeout=180,
        )
        assert plain.status_code == 200

        tools_history = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.6-luna",
                "messages": [
                    {"role": "system", "content": "Reply briefly."},
                    {"role": "user", "content": "Use the supplied result."},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_live_history",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{\"q\":\"status\"}"},
                        }],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_live_history",
                        "content": "OK",
                    },
                    {"role": "user", "content": "Return the result only."},
                ],
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Return a short status",
                        "parameters": {
                            "type": "object",
                            "properties": {"q": {"type": "string"}},
                            "required": ["q"],
                            "additionalProperties": False,
                        },
                    },
                }],
            },
            headers=headers,
            timeout=180,
        )
        assert tools_history.status_code == 200

        streamed = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.6-luna",
                "stream": True,
                "messages": [{"role": "user", "content": "Reply with OK only"}],
            },
            headers=headers,
            timeout=180,
        )
        assert streamed.status_code == 200
        assert "data:" in streamed.text and '"error"' not in streamed.text


def test_live_lite_reasoning_tools_and_token_matrix(tmp_path, monkeypatch):
    import app.codex_gateway as gateway_module

    monkeypatch.setattr(gateway_module, "TEXT_RETRY_DELAYS_SECONDS", ())
    client, headers = _live_client(tmp_path, monkeypatch)
    try:
        statuses: dict[str, int] = {}
        base = {
            "model": "gpt-5.6-luna",
            "messages": [
                {"role": "system", "content": "Reply briefly."},
                {"role": "user", "content": "Reply with OK only"},
            ],
        }
        for effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
            response = client.post(
                "/v1/chat/completions",
                json={**base, "reasoning_effort": effort},
                headers=headers,
            )
            statuses[f"effort:{effort}"] = response.status_code

        tools = [{
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "Return status",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        } for index in range(23)]
        response = client.post(
            "/v1/chat/completions",
            json={**base, "reasoning_effort": "high", "tools": tools},
            headers=headers,
        )
        statuses["tools:23"] = response.status_code

        for max_tokens in (16, 1024):
            response = client.post(
                "/v1/chat/completions",
                json={**base, "reasoning_effort": "high", "max_tokens": max_tokens},
                headers=headers,
            )
            statuses[f"max_tokens:{max_tokens}"] = response.status_code
        print("LITE_MATRIX=" + json.dumps(statuses, sort_keys=True))
        assert all(status == 200 for name, status in statuses.items() if name != "effort:ultra")
    finally:
        client.__exit__(None, None, None)
