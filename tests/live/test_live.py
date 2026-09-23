from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


def test_live_codex_account_pool(tmp_path, monkeypatch):
    if os.getenv("CODEX_LIVE") != "1":
        pytest.skip("set CODEX_LIVE=1")
    auth = Path.home() / ".codex" / "auth.json"
    if not auth.is_file():
        pytest.skip("no ~/.codex/auth.json")
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "admin_initial_password", "live-admin-password-2026")
    with TestClient(app) as client:
        login = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": "live-admin-password-2026"},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200, login.text
        admin_headers = {
            "Origin": "http://testserver",
            "X-CSRF-Token": login.json()["csrf_token"],
        }
        imported = client.post(
            "/admin/api/accounts/import", files={"files[]": ("auth.json", auth.read_bytes(), "application/json")},
            headers=admin_headers,
        )
        assert imported.status_code == 200, imported.text
        account_id = (imported.json()["created"] + imported.json()["updated"])[0]["account_id"]
        created_key = client.post(
            "/admin/api/keys",
            json={"name": "live", "preferred_account_id": account_id},
            headers=admin_headers,
        )
        assert created_key.status_code == 200, created_key.text
        key = created_key.json()["key"]
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Reply with OK only"}]},
            headers={"Authorization": f"Bearer {key}"}, timeout=180,
        )
        assert response.status_code == 200, response.text
        assert "OK" in response.json()["choices"][0]["message"]["content"].upper()
