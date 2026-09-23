from __future__ import annotations

import json

import httpx

from app.http_client import set_http_transport
from conftest import ADMIN_HEADERS, auth_payload, import_pool


def usage_payload(*, used_percent: float = 25, available_count: int = 1) -> dict:
    return {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": used_percent,
                "limit_window_seconds": 18_000,
                "reset_at": 4_000_000_000,
            },
            "secondary_window": {
                "used_percent": 60,
                "limit_window_seconds": 604_800,
                "reset_at": 4_000_100_000,
            },
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "8.50"},
        "rate_limit_reset_credits": {"available_count": available_count},
        "additional_rate_limits": [
            {
                "metered_feature": "codex_other",
                "limit_name": "Other",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 3600,
                        "reset_at": 4_000_200_000,
                    }
                },
            }
        ],
    }


def reset_cards_payload() -> dict:
    return {
        "available_count": 1,
        "credits": [
            {
                "id": "credit-1",
                "reset_type": "codex_rate_limits",
                "status": "available",
                "granted_at": "2026-09-01T00:00:00Z",
                "expires_at": "2026-10-01T00:00:00Z",
                "title": "Full reset",
                "description": "Ready to redeem",
                "profile_image_url": "must-not-leak",
            }
        ],
    }


def test_account_quota_refresh_parses_windows_cards_and_uses_cache(client):
    import_pool(client, auth_payload("acct-quota"))
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["chatgpt-account-id"] == "acct-quota"
        if request.url.path.endswith("/wham/usage"):
            return httpx.Response(200, json=usage_payload())
        if request.url.path.endswith("/wham/rate-limit-reset-credits"):
            return httpx.Response(200, json=reset_cards_payload())
        raise AssertionError(request.url)

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["refresh_interval_seconds"] == 600
    quota = body["data"][0]
    assert quota["account_id"] == "acct-quota"
    assert quota["plan_type"] == "plus"
    assert quota["limits"][0]["primary"] == {
        "used_percent": 25.0,
        "remaining_percent": 75.0,
        "window_minutes": 300,
        "resets_at": 4_000_000_000,
    }
    assert quota["limits"][0]["secondary"]["window_minutes"] == 10_080
    assert len(quota["limits"]) == 1
    assert quota["limits"][0]["limit_id"] == "codex"
    assert quota["next_reset_at"] == 4_000_000_000
    assert quota["reset_credits"]["available_count"] == 1
    assert quota["reset_credits"]["credits"][0]["id"] == "credit-1"
    assert "profile_image_url" not in quota["reset_credits"]["credits"][0]

    cached = client.get("/admin/api/accounts/quotas", headers=ADMIN_HEADERS)
    assert cached.status_code == 200
    assert cached.json()["data"][0]["cached"] is True
    assert calls == [
        "/backend-api/wham/usage",
        "/backend-api/wham/rate-limit-reset-credits",
    ]


def test_reset_quota_uses_idempotency_and_refreshes_snapshot_with_mock_upstream(client):
    import_pool(client, auth_payload("acct-reset"))
    posts: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(json.loads(request.content))
            return httpx.Response(200, json={"code": "reset", "windows_reset": 2})
        if request.url.path.endswith("/wham/usage"):
            return httpx.Response(200, json=usage_payload(used_percent=0, available_count=0))
        if request.url.path.endswith("/wham/rate-limit-reset-credits"):
            return httpx.Response(200, json={"available_count": 0, "credits": []})
        raise AssertionError(request.url)

    set_http_transport(httpx.MockTransport(handler))
    response = client.post(
        "/admin/api/accounts/acct-reset/quota/reset",
        headers=ADMIN_HEADERS,
        json={
            "idempotency_key": "redeem-request-123",
            "credit_id": "credit-1",
            "confirmation_phrase": "确定重置",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["outcome"] == "reset"
    assert response.json()["windows_reset"] == 2
    assert response.json()["quota"]["limits"][0]["primary"]["used_percent"] == 0
    assert posts == [{"redeem_request_id": "redeem-request-123", "credit_id": "credit-1"}]


def test_reset_quota_requires_exact_confirmation_phrase(client):
    for confirmation_phrase in (None, "重置", " 确定重置", "确定重置 "):
        body = {"idempotency_key": "redeem-request-123", "credit_id": "credit-1"}
        if confirmation_phrase is not None:
            body["confirmation_phrase"] = confirmation_phrase
        response = client.post(
            "/admin/api/accounts/acct-reset/quota/reset",
            headers=ADMIN_HEADERS,
            json=body,
        )
        assert response.status_code == 422, response.text


def test_quota_read_keeps_usage_when_reset_card_detail_endpoint_is_unavailable(client):
    import_pool(client, auth_payload("acct-summary"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/wham/usage"):
            return httpx.Response(200, json=usage_payload(available_count=2))
        return httpx.Response(404, json={"error": {"message": "not enabled"}})

    set_http_transport(httpx.MockTransport(handler))
    response = client.get("/admin/api/accounts/quotas?refresh=true", headers=ADMIN_HEADERS)
    assert response.status_code == 200
    quota = response.json()["data"][0]
    assert quota["error"] is None
    assert quota["reset_credits"] == {"available_count": 2, "credits": []}
