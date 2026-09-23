from __future__ import annotations

from conftest import ADMIN_HEADERS, ADMIN_PASSWORD
from fastapi.testclient import TestClient

from app.config import settings
from app.store.gateway import gateway_store, iso_now


def test_card_batch_plaintext_once_disable_and_list_hides_code(client):
    created = client.post(
        "/admin/api/cards/batches",
        json={"amount_usd": "12.50", "count": 2, "note": "batch-a"},
        headers=ADMIN_HEADERS,
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["count"] == 2
    assert body["amount_usd"] == "12.50"
    assert body["note"] == "batch-a"
    assert len(body["codes"]) == 2
    assert all(code.startswith("MK-") and code.count("-") == 3 for code in body["codes"])
    listed = client.get("/admin/api/cards", headers=ADMIN_HEADERS)
    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["total"] >= 2
    blob = listed.text
    for code in body["codes"]:
        assert code not in blob
    assert "code_hash" not in blob
    assert all(row.get("note") == "batch-a" for row in payload["data"] if row["batch_id"] == body["batch_id"])
    first_id = payload["data"][0]["id"]
    disabled = client.post(f"/admin/api/cards/{first_id}/disable", headers=ADMIN_HEADERS)
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["status"] == "disabled"


def test_redeem_counts_success_and_failure_toward_daily_limit(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent: list[tuple[str, str]] = []
    answers: list[str] = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    origin = {"Origin": "https://portal.example"}
    with TestClient(app, base_url="https://portal.example") as client:
        admin = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers=origin,
        ).json()
        admin_headers = {**origin, "X-CSRF-Token": admin["csrf_token"]}
        batch = client.post(
            "/admin/api/cards/batches",
            json={"amount_usd": "1.00", "count": 1},
            headers=admin_headers,
        )
        assert batch.status_code == 200, batch.text
        code = batch.json()["codes"][0]
        cap = client.get("/portal/api/captcha").json()
        mailed = client.post(
            "/portal/api/auth/send-code",
            json={"name": "Ada", "email": "limit@example.com", "captcha_id": cap["id"], "captcha": answers[-1]},
            headers=origin,
        )
        assert mailed.status_code == 200, mailed.text
        verified = client.post(
            "/portal/api/auth/verify",
            json={"challenge_id": mailed.json()["challenge_id"], "email": "limit@example.com", "code": sent[-1][1]},
            headers=origin,
        )
        assert verified.status_code == 200, verified.text
        headers = {**origin, "X-CSRF-Token": verified.json()["csrf_token"]}
        first = client.post("/portal/api/wallet/redeem", json={"code": code}, headers=headers)
        assert first.status_code == 200, first.text
        for _ in range(9):
            failed = client.post("/portal/api/wallet/redeem", json={"code": "MK-AAAA-AAAA-AAAA"}, headers=headers)
            assert failed.status_code == 404, failed.text
        blocked = client.post("/portal/api/wallet/redeem", json={"code": "MK-BBBB-BBBB-BBBB"}, headers=headers)
        assert blocked.status_code == 429, blocked.text
        assert blocked.json()["error"]["code"] == "redeem_rate_limited"


def test_redeem_once_then_409(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent: list[tuple[str, str]] = []
    answers: list[str] = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    origin = {"Origin": "https://portal.example"}
    with TestClient(app, base_url="https://portal.example") as client:
        admin = client.post(
            "/admin/api/auth/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
            headers=origin,
        ).json()
        admin_headers = {**origin, "X-CSRF-Token": admin["csrf_token"]}
        batch = client.post(
            "/admin/api/cards/batches",
            json={"amount_usd": "3.00", "count": 1},
            headers=admin_headers,
        )
        assert batch.status_code == 200, batch.text
        code = batch.json()["codes"][0]
        cap = client.get("/portal/api/captcha").json()
        mailed = client.post(
            "/portal/api/auth/send-code",
            json={"name": "Ada", "email": "ada@example.com", "captcha_id": cap["id"], "captcha": answers[-1]},
            headers=origin,
        )
        assert mailed.status_code == 200, mailed.text
        verified = client.post(
            "/portal/api/auth/verify",
            json={"challenge_id": mailed.json()["challenge_id"], "email": "ada@example.com", "code": sent[-1][1]},
            headers=origin,
        )
        assert verified.status_code == 200, verified.text
        csrf = verified.json()["csrf_token"]
        first = client.post(
            "/portal/api/wallet/redeem",
            json={"code": code},
            headers={**origin, "X-CSRF-Token": csrf},
        )
        assert first.status_code == 200, first.text
        assert first.json()["amount_usd"] == "3.00"
        assert first.json()["balance_after"] == "3.00"
        assert code not in first.text
        wallet = client.get("/portal/api/wallet")
        assert wallet.json()["usd_credit"] == "3.00"
        second = client.post(
            "/portal/api/wallet/redeem",
            json={"code": code},
            headers={**origin, "X-CSRF-Token": csrf},
        )
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "card_already_redeemed"
        listed = client.get("/admin/api/cards", headers=admin_headers).json()["data"]
        assert any(item["status"] == "redeemed" and item["prefix"].startswith("MK-") for item in listed)
        for item in listed:
            assert "code" not in item or item.get("code") in (None, "")
