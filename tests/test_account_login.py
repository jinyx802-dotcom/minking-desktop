from __future__ import annotations

import base64
import json

import httpx

from app.http_client import set_http_transport
from app.providers.codex import OAUTH_AUTH_URL as CODEX_AUTH_URL
from app.providers.grok import _device_page_url
from app.providers.codex import OAUTH_REDIRECT_URI as CODEX_REDIRECT
from conftest import ADMIN_HEADERS, auth_payload, import_pool


def _patch_loopback(monkeypatch, bound=False):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return bound

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)


def _segment(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _jwt(claims: dict) -> str:
    return f"{_segment({'alg': 'none'})}.{_segment(claims)}.x"


def test_grok_device_page_url_keeps_user_code_query():
    assert (
        _device_page_url("https://accounts.x.ai/oauth2/device?user_code=ZDSP-SSF7")
        == "https://accounts.x.ai/oauth2/device?user_code=ZDSP-SSF7"
    )
    assert (
        _device_page_url("https://accounts.x.ai/oauth2/device", user_code="BSRN-JHPR")
        == "https://accounts.x.ai/oauth2/device?user_code=BSRN-JHPR"
    )


def test_codex_oauth_start_returns_chatgpt_url(client, monkeypatch):
    _patch_loopback(monkeypatch)
    response = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "codex"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "codex"
    assert body["auth_url"].startswith(CODEX_AUTH_URL)
    assert "code_challenge" in body["auth_url"]
    assert body["redirect_uri"] == CODEX_REDIRECT
    assert "refresh" not in response.text.lower() or "refresh_token" not in response.text


def test_codex_oauth_callback_imports_account(client, monkeypatch):
    _patch_loopback(monkeypatch)
    account_id = "acct_oauth_login"
    access = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            "exp": 4_900_000_000,
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com" and request.url.path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": access,
                    "refresh_token": "refresh-oauth-login",
                    "id_token": _jwt({"email": "codex@example.com"}),
                },
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "codex"},
        headers=ADMIN_HEADERS,
    ).json()
    response = client.post(
        "/admin/api/accounts/oauth/callback",
        json={
            "provider": "codex",
            "state": started["state"],
            "callback_url": f"{CODEX_REDIRECT}?state={started['state']}&code=codex-login-code",
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["created"][0]["account_id"] == account_id
    assert body["created"][0]["provider"] == "codex"
    assert "refresh-oauth-login" not in response.text


def test_replaying_completed_codex_login_restores_deleted_account(client, monkeypatch):
    _patch_loopback(monkeypatch)
    account_id = "acct_oauth_replay"
    access = _jwt(
        {
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
            "exp": 4_900_000_000,
        }
    )
    token_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.openai.com" and request.url.path.endswith("/oauth/token"):
            token_calls["count"] += 1
            return httpx.Response(
                200,
                json={
                    "access_token": access,
                    "refresh_token": "refresh-oauth-replay",
                    "id_token": _jwt({"email": "replay@example.com"}),
                },
            )
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "codex"},
        headers=ADMIN_HEADERS,
    ).json()
    callback = {
        "provider": "codex",
        "state": started["state"],
        "callback_url": f"{CODEX_REDIRECT}?state={started['state']}&code=codex-replay-code",
    }
    first = client.post("/admin/api/accounts/oauth/callback", json=callback, headers=ADMIN_HEADERS)
    assert first.status_code == 200, first.text
    removed = client.delete(f"/admin/api/accounts/{account_id}", headers=ADMIN_HEADERS)
    assert removed.status_code == 200, removed.text
    assert client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"] == []
    replay = client.post("/admin/api/accounts/oauth/callback", json=callback, headers=ADMIN_HEADERS)
    assert replay.status_code == 200, replay.text
    assert token_calls["count"] == 1
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["account_id"] == account_id
    assert listed[0]["status"] == "active"
    assert "refresh-oauth-replay" not in replay.text


def test_grok_oauth_uses_device_code_then_imports(client, monkeypatch):
    _patch_loopback(monkeypatch)
    token_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "auth.x.ai" and path.endswith("/oauth2/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "device-secret-login",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://accounts.x.ai/oauth2/device",
                    "verification_uri_complete": "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        if request.url.host == "auth.x.ai" and path.endswith("/oauth2/token"):
            token_calls["count"] += 1
            if token_calls["count"] == 1:
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200,
                json={
                    "access_token": "grok-access-login",
                    "refresh_token": "grok-refresh-login",
                    "expires_in": 3600,
                    "id_token": _jwt({"sub": "user_grok_login", "email": "grok@example.com"}),
                },
            )
        if path.endswith("/oauth2/userinfo"):
            return httpx.Response(200, json={"sub": "user_grok_login", "email": "grok@example.com"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["login_mode"] == "device"
    assert body["user_code"] == "ABCD-1234"
    assert body["auth_url"] == "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"
    assert "user_code=ABCD-1234" in (body.get("auth_url") or "")
    assert "56121" not in (body.get("auth_url") or "")
    assert "device_code" not in body
    assert "device-secret-login" not in started.text
    pending = client.get(
        "/admin/api/accounts/oauth/status",
        params={"state": body["state"]},
        headers=ADMIN_HEADERS,
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    assert "device-secret-login" not in pending.text
    done = client.get(
        "/admin/api/accounts/oauth/status",
        params={"state": body["state"]},
        headers=ADMIN_HEADERS,
    )
    assert done.status_code == 200, done.text
    created = done.json()["created"][0]
    assert created["account_id"] == "grok:user_grok_login"
    assert created["provider"] == "grok"
    assert created["label"] == "grok@example.com"
    assert "grok-refresh-login" not in done.text


def test_export_account_returns_imported_json(client):
    imported = import_pool(client, auth_payload("acct-export-1", marker="export"))
    account_id = imported["created"][0]["account_id"]
    response = client.get(
        f"/admin/api/accounts/{account_id}/export",
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers.get("content-disposition", "")
    assert response.headers["content-disposition"].endswith('.json"')
    body = response.json()
    assert body["tokens"]["account_id"] == "acct-export-1"
    assert "source_path" not in body


def test_oauth_start_rejects_unknown_provider(client, monkeypatch):
    _patch_loopback(monkeypatch)
    response = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "claude"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 422
