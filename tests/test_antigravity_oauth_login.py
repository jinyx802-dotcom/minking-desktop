from __future__ import annotations

import base64
import json

import httpx

from app.http_client import set_http_transport
from app.providers.antigravity import (
    OAUTH_AUTH_URL,
    TOKEN_URL,
    build_oauth_auth_url,
    oauth_redirect_uri,
    parse_oauth_callback,
)
from conftest import ADMIN_HEADERS


def _segment(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _id_token(email: str) -> str:
    return f"{_segment({'alg': 'none'})}.{_segment({'email': email})}.x"


def test_parse_oauth_callback_accepts_url_query_and_raw_code():
    parsed = parse_oauth_callback(
        "http://localhost:51121/oauth-callback?state=abc12345&code=4/0Aean-login"
    )
    assert parsed == {"code": "4/0Aean-login", "state": "abc12345"}
    assert parse_oauth_callback("4/0Aean-login") == {"code": "4/0Aean-login"}
    assert parse_oauth_callback("code=4/0Aean-login&state=abc12345") == {
        "code": "4/0Aean-login",
        "state": "abc12345",
    }
    denied = parse_oauth_callback(
        "http://localhost:51121/oauth-callback?error=access_denied&state=abc12345"
    )
    assert denied["error"] == "access_denied"
    assert denied["state"] == "abc12345"


def test_build_oauth_auth_url_uses_loopback_and_offline_access(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_OAUTH_CLIENT_ID", "test-client.apps.googleusercontent.com")
    url = build_oauth_auth_url("state-token-value")
    assert url.startswith(OAUTH_AUTH_URL)
    assert "test-client.apps.googleusercontent.com" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "localhost%3A51121" in url or "localhost:51121" in url
    assert "GOCSPX" not in url
    assert oauth_redirect_uri() == "http://localhost:51121/oauth-callback"


def _install_google_login_transport(**overrides):
    token_body = {
        "access_token": "ya29.login-access",
        "refresh_token": "1//0login-refresh",
        "expires_in": 3600,
        "id_token": _id_token("login@example.com"),
        "scope": "https://www.googleapis.com/auth/cloud-platform",
        **overrides.pop("token", {}),
    }
    userinfo_status = overrides.pop("userinfo_status", 200)
    assist_status = overrides.pop("assist_status", 200)
    token_status = overrides.pop("token_status", 200)
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(f"{request.method} {request.url.host}{request.url.path}")
        if str(request.url).startswith(TOKEN_URL):
            return httpx.Response(token_status, json=token_body)
        if "userinfo" in request.url.path:
            return httpx.Response(userinfo_status, json={"email": "login@example.com"})
        if "loadCodeAssist" in request.url.path:
            if assist_status >= 400:
                return httpx.Response(assist_status, json={"error": {"message": "unavailable"}})
            return httpx.Response(200, json={"cloudaicompanionProject": "oauth-cloud-project"})
        return httpx.Response(404, json={"error": {"message": "missing"}})

    set_http_transport(httpx.MockTransport(handler))
    return captured


def test_antigravity_oauth_start_requires_admin(client):
    response = client.post("/admin/api/accounts/oauth/start", json={"provider": "antigravity"})
    assert response.status_code in {401, 403}


def test_admin_accounts_page_has_login_and_file_modes(client):
    page = client.get("/admin")
    assert page.status_code == 200
    assert "网页登录" in page.text
    assert "文件导入/导出" in page.text
    assert 'id="oauth-start-form"' in page.text
    assert 'id="oauth-panel"' in page.text
    assert 'id="account-files-pane"' in page.text
    assert "Google 登录" in page.text
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "data-export-account" in js.text


def test_antigravity_oauth_start_returns_google_url(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    response = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "antigravity"
    assert body["auth_url"].startswith(OAUTH_AUTH_URL)
    assert body["state"]
    assert body["redirect_uri"] == "http://localhost:51121/oauth-callback"
    assert body["listen_bound"] is False
    assert "GOCSPX" not in response.text


def test_antigravity_oauth_listen_bound_only_when_admin_is_local(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return True

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    remote = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    assert remote.status_code == 200, remote.text
    assert remote.json()["listen_bound"] is False
    assert CodexGateway._admin_is_local("127.0.0.1") is True
    assert CodexGateway._admin_is_local("localhost") is True
    assert CodexGateway._admin_is_local("10.1.102.36") is False


def test_antigravity_oauth_callback_imports_account(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    _install_google_login_transport()
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    ).json()
    response = client.post(
        "/admin/api/accounts/oauth/callback",
        json={
            "provider": "antigravity",
            "state": started["state"],
            "callback_url": (
                f"http://localhost:51121/oauth-callback?state={started['state']}&code=4/0Aean-login"
            ),
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["created"][0]["account_id"] == "antigravity:login@example.com"
    assert body["created"][0]["provider"] == "antigravity"
    assert "ya29." not in response.text
    assert "1//0" not in response.text
    status = client.get(
        "/admin/api/accounts/oauth/status",
        params={"state": started["state"]},
        headers=ADMIN_HEADERS,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["created"][0]["account_id"] == "antigravity:login@example.com"


def test_antigravity_oauth_callback_rejects_wrong_state(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    )
    response = client.post(
        "/admin/api/accounts/oauth/callback",
        json={
            "provider": "antigravity",
            "state": "not-the-real-state-value",
            "code": "4/0Aean-login",
        },
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 410


def test_antigravity_oauth_callback_requires_refresh_token(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    _install_google_login_transport(token={"refresh_token": ""})
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    ).json()
    response = client.post(
        "/admin/api/accounts/oauth/callback",
        json={"provider": "antigravity", "state": started["state"], "code": "4/0Aean-login"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 422
    assert "refresh token" in response.json()["error"]["message"].lower()
    assert "4/0Aean-login" not in response.text


def test_antigravity_oauth_still_imports_when_project_lookup_fails(client, monkeypatch):
    from app.codex_gateway import CodexGateway

    async def fake_listen(self, *args, **kwargs):
        return False

    monkeypatch.setattr(CodexGateway, "_ensure_oauth_loopback", fake_listen)
    _install_google_login_transport(assist_status=503)
    started = client.post(
        "/admin/api/accounts/oauth/start",
        json={"provider": "antigravity"},
        headers=ADMIN_HEADERS,
    ).json()
    response = client.post(
        "/admin/api/accounts/oauth/callback",
        json={"provider": "antigravity", "state": started["state"], "code": "4/0Aean-login"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json()["created"][0]["account_id"] == "antigravity:login@example.com"
