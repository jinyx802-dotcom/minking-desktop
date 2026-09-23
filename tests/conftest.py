from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import settings
from app.http_client import reset_http_transport

ADMIN_PASSWORD = "test-admin-password-2026"
ADMIN_HEADERS = {"Origin": "http://testserver", "X-CSRF-Token": ""}


def auth_payload(account_id: str, marker: str = "token") -> dict:
    claims = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "exp": 4_900_000_000,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": f"eyJhbGciOiJub25lIn0.{encoded}.",
            "refresh_token": f"refresh-{marker}",
            "account_id": account_id,
        },
    }


def import_pool(client: TestClient, *payloads: dict) -> dict:
    files = [("files[]", (f"auth-{index}.json", json.dumps(payload), "application/json")) for index, payload in enumerate(payloads)]
    response = client.post("/admin/api/accounts/import", files=files, headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    if body.get("generated_api_key") is None and payloads:
        account_id = str(payloads[0]["tokens"]["account_id"])
        created = client.post(
            "/admin/api/keys",
            json={"name": "default", "preferred_account_id": account_id},
            headers=ADMIN_HEADERS,
        )
        assert created.status_code == 200, created.text
        body["generated_api_key"] = created.json()
    generated = body.get("generated_api_key") or {}
    if generated.get("id"):
        from app.store.gateway import gateway_store
        client.portal.call(
            gateway_store.execute,
            "UPDATE api_keys SET usd_credit=? WHERE id=?",
            ("100.00", generated["id"]),
        )
    return body


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "admin_initial_password", ADMIN_PASSWORD)
    monkeypatch.setattr(settings, "routing_secret", "test-routing-secret")
    from app.codex_gateway import codex_gateway
    from app.store.gateway import gateway_store

    original = codex_gateway.create_api_key

    async def funded(*args, **kwargs):
        created = await original(*args, **kwargs)
        await gateway_store.execute(
            "UPDATE api_keys SET usd_credit=? WHERE id=?",
            ("100.00", created["id"]),
        )
        return created

    monkeypatch.setattr(codex_gateway, "create_api_key", funded)
    reset_http_transport()
    yield
    reset_http_transport()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as test_client:
        login = test_client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers={"Origin": "http://testserver"},
        )
        assert login.status_code == 200, login.text
        ADMIN_HEADERS["X-CSRF-Token"] = login.json()["csrf_token"]
        yield test_client
