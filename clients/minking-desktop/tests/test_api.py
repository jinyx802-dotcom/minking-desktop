from __future__ import annotations

from minking_desktop.api import ApiError, PortalClient, direct_opener
from minking_desktop.paths import public_root_url, public_v1_url


def test_full_catalog_uses_plain_models_endpoint_and_server_media_types():
    def request(method, url, payload, headers, timeout):
        assert method == "GET" and url == "https://portal.example/v1/models"
        assert headers["Authorization"] == "Bearer test-key"
        assert "codex" not in headers["User-Agent"].lower()
        return 200, {"data": [{"id": "gpt-6-astra"}, {"id": "grok-imagine-image"},
            {"id": "grok-imagine-video"}, {"id": "new-media", "type": "video"},
            {"id": "grok-imagine-image"}, {}, "invalid"]}
    result = PortalClient("https://portal.example/v1", request=request).fetch_model_catalog("test-key")
    assert [item["type"] for item in result] == ["text", "image", "video", "video"]
    assert all(item["availability"] == "cloud_listed" for item in result)


def test_full_catalog_failure_is_visible_instead_of_text_only_fallback():
    import pytest
    for status, body in [(401, {}), (200, {"models": [{"slug": "text-only"}]})]:
        client = PortalClient("https://portal.example/v1", request=lambda *args: (status, body))
        with pytest.raises(ApiError):
            client.fetch_model_catalog("test-key")


def test_direct_opener_skips_system_proxy():
    opener = direct_opener()
    found = [getattr(handler, "proxies", None) for handler in opener.handlers]
    assert not any(isinstance(item, dict) and item for item in found)


def test_public_url_normalization():
    assert public_v1_url("https://ceshi.007ka.cn/maliang") == "https://ceshi.007ka.cn/maliang/v1"
    assert public_v1_url("https://ceshi.007ka.cn/maliang/v1/") == "https://ceshi.007ka.cn/maliang/v1"
    assert public_root_url("https://ceshi.007ka.cn/maliang/v1") == "https://ceshi.007ka.cn/maliang"


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []
        self.token = "desktop-token-not-a-key"

    def __call__(self, method, url, payload, headers, timeout):
        self.calls.append({"method": method, "url": url, "payload": payload, "headers": dict(headers or {})})
        path = url.rsplit("/portal/api", 1)[-1]
        if path == "/captcha" and method == "GET":
            return 200, {"id": "cap1", "image": "data:image/png;base64,QQ=="}
        if path == "/desktop/auth/send-code":
            assert payload["email"] == "ada@example.com"
            return 200, {"challenge_id": "ch1", "expires_in": "600"}
        if path == "/desktop/auth/verify":
            return 200, {
                "name": "Ada",
                "email": "ada@example.com",
                "token": self.token,
                "csrf_token": "csrf",
                "expires_in": 2592000,
                "token_type": "Bearer",
            }
        if path == "/desktop/bootstrap":
            assert headers["Authorization"] == f"Bearer {self.token}"
            return 200, {
                "user": {"name": "Ada", "email": "ada@example.com", "usd_credit": "12.5"},
                "key": {"prefix": "sk-ts-abcdef", "status": "active"},
                "public_base_url": "https://portal.example/v1",
                "models": ["gpt-6-astra"],
                "harnesses": [],
            }
        if path == "/desktop/key":
            return 200, {"key": "sk-ts-secret", "prefix": "sk-ts-abcdef"}
        if path == "/desktop/logout":
            return 200, {"ok": True}
        if path == "/desktop/skills" and method == "GET":
            return 200, {"skills": [{"name": "minking-media", "sha256": "abc", "files": ["SKILL.md"]}]}
        if path == "/dashboard":
            return 200, {
                "user": {"usd_credit": "12.5"},
                "metrics": {
                    "calls": 8,
                    "successes": 7,
                    "success_rate": 87.5,
                    "failure_rate": 12.5,
                    "input_tokens": 100,
                    "output_tokens": 40,
                    "cached_tokens": 12,
                },
                "day": {"calls": 2, "success_rate": 100, "failure_rate": 0, "input_tokens": 20, "output_tokens": 8, "cached_tokens": 1},
                "week": {"calls": 5, "success_rate": 80, "failure_rate": 20, "input_tokens": 50, "output_tokens": 20, "cached_tokens": 5},
                "month": {"calls": 8, "success_rate": 87.5, "failure_rate": 12.5, "input_tokens": 100, "output_tokens": 40, "cached_tokens": 12},
            }
        if path.startswith("/calls"):
            return 200, {
                "data": [
                    {
                        "started_at": "2026-09-20T00:00:00Z",
                        "model": "gpt-6-astra",
                        "status": "success",
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "cached_tokens": 2,
                        "total_tokens": 16,
                    }
                ],
                "page": 1,
                "page_size": 20,
                "total": 1,
            }
        if path.startswith("/wallet/ledger"):
            return 200, {
                "data": [{"created_at": "2026-09-20", "type": "redeem", "amount": "10.00", "balance": "22.50"}],
                "page": 1,
                "total": 1,
            }
        if path == "/wallet/redeem":
            assert payload["code"] == "CARD-9"
            return 200, {"usd_credit": "22.50"}
        if path == "/wallet" or path.startswith("/wallet?"):
            return 200, {"usd_credit": "12.5"}
        return 404, {"error": {"message": "missing", "code": "not_found"}}


def test_login_errors_are_chinese():
    expired = ApiError("Image code expired or incorrect", code="invalid_captcha")
    assert expired.message == "图片验证码已过期或不正确，请换一张再试"
    mailed = ApiError("Verification email could not be sent", code="mail_unavailable")
    assert "Verification" not in mailed.as_dict()["error"]
    assert "发不出去" in mailed.as_dict()["error"]


def test_login_bootstrap_and_key_mocked():
    transport = FakeTransport()
    client = PortalClient("https://portal.example", request=transport)
    captcha = client.captcha()
    assert captcha["id"] == "cap1"
    mailed = client.send_code(name="Ada", email="ada@example.com", captcha_id="cap1", captcha="AB12")
    assert mailed["challenge_id"] == "ch1"
    verified = client.verify(challenge_id="ch1", email="ada@example.com", code="123456", device_id="ab" * 32)
    assert verified["token"] == transport.token
    assert not verified["token"].startswith("sk-")
    boot = client.bootstrap()
    assert boot["key"]["prefix"].startswith("sk-ts-")
    assert "sk-ts-secret" not in str(boot)
    key = client.reveal_key()
    assert key["key"].startswith("sk-ts-")
    bodies = [json_payload(call) for call in transport.calls]
    assert all("chatgpt-official" not in item for item in bodies)
    assert all("Cookie" not in call["headers"] for call in transport.calls)
    auth_calls = [call for call in transport.calls if call["url"].endswith("/desktop/bootstrap")]
    assert auth_calls[0]["headers"]["Authorization"].startswith("Bearer ")


def json_payload(call) -> str:
    return str(call.get("payload") or "")


def test_unregistered_email_is_not_missing_route():
    def unregistered(method, url, payload, headers, timeout):
        return 404, {"error": {"message": "该邮箱尚未注册，请先注册", "code": "email_not_registered"}}

    client = PortalClient("https://portal.example", request=unregistered)
    try:
        client.send_code(name="", email="new@example.com", captcha_id="cap1", captcha="AB12")
    except ApiError as exc:
        assert exc.code == "email_not_registered"
        assert "尚未注册" in exc.message
        assert "桌面登录接口" not in exc.message
    else:
        raise AssertionError("expected ApiError")


def test_desktop_missing_route_message():
    def missing(method, url, payload, headers, timeout):
        return 404, {"error": {"message": "Not Found", "code": "not_found"}}

    client = PortalClient("https://portal.example", request=missing)
    try:
        client.send_code(name="Ada", email="ada@example.com", captcha_id="cap1", captcha="AB12")
    except ApiError as exc:
        assert exc.status == 404
        assert "桌面登录接口" in exc.message
    else:
        raise AssertionError("expected ApiError")


def test_dashboard_calls_wallet_parse_mock_json():
    transport = FakeTransport()
    client = PortalClient("https://portal.example", token=transport.token, request=transport)
    dash = client.dashboard()
    assert dash["metrics"]["calls"] == 8
    assert dash["day"]["input_tokens"] == 20
    assert dash["week"]["cached_tokens"] == 5
    assert dash["month"]["failure_rate"] == 12.5
    calls = client.calls(page=1, page_size=20)
    assert calls["data"][0]["model"] == "gpt-6-astra"
    assert calls["data"][0]["cached_tokens"] == 2
    wallet = client.wallet()
    assert wallet["usd_credit"] == "12.5"
    ledger = client.wallet_ledger(page=1, page_size=20)
    assert ledger["data"][0]["amount"] == "10.00"
    redeemed = client.redeem("CARD-9")
    assert redeemed["usd_credit"] == "22.50"
    called = [item["url"] for item in transport.calls]
    assert any(item.endswith("/portal/api/calls?page=1&page_size=20") for item in called)
    assert any(item.endswith("/portal/api/wallet") for item in called)
    assert any(item.endswith("/portal/api/wallet/ledger?page=1&page_size=20") for item in called)
    assert any(item.endswith("/portal/api/wallet/redeem") for item in called)


def test_list_skills_mocked():
    transport = FakeTransport()
    client = PortalClient("https://portal.example", token=transport.token, request=transport)
    listed = client.list_skills()
    assert listed["skills"][0]["name"] == "minking-media"


def test_download_skill_uses_bytes_transport():
    blob = b"PK\x03\x04skill"

    def bytes_fn(method, url, payload, headers, timeout):
        assert method == "GET"
        assert url.endswith("/portal/api/desktop/skills/minking-media")
        assert headers["Authorization"].startswith("Bearer ")
        return 200, blob

    client = PortalClient("https://portal.example", token="desktop-token", request_bytes=bytes_fn)
    assert client.download_skill("minking-media") == blob
    try:
        client.download_skill("../etc")
    except ApiError as exc:
        assert exc.code == "invalid_skill"
    else:
        raise AssertionError("expected ApiError")


def test_account_api_missing_is_friendly():
    def missing(method, url, payload, headers, timeout):
        return 404, {"error": {"message": "Not Found", "code": "not_found"}}

    client = PortalClient("https://portal.example", token="desktop-token", request=missing)
    for fn in (client.calls, client.wallet, client.wallet_ledger):
        try:
            fn()
        except ApiError as exc:
            assert exc.status == 404
            assert exc.code == "portal_api_missing"
            assert "尚未更新" in exc.message
        else:
            raise AssertionError("expected ApiError")
    try:
        client.redeem("CARD-9")
    except ApiError as exc:
        assert exc.status == 404
        assert "尚未更新" in exc.message
    else:
        raise AssertionError("expected ApiError")


def test_redeem_requires_code():
    def boom(method, url, payload, headers, timeout):
        raise AssertionError("should not request")

    client = PortalClient("https://portal.example", token="desktop-token", request=boom)
    try:
        client.redeem("  ")
    except ApiError as exc:
        assert exc.code == "redeem_code_required"
    else:
        raise AssertionError("expected ApiError")


def test_error_shape():
    def boom(method, url, payload, headers, timeout):
        return 401, {"error": {"message": "Email login required", "code": "portal_auth_required"}}

    client = PortalClient("https://portal.example/v1", token="x", request=boom)
    try:
        client.bootstrap()
    except ApiError as exc:
        assert exc.status == 401
        assert exc.code == "portal_auth_required"
    else:
        raise AssertionError("expected ApiError")
