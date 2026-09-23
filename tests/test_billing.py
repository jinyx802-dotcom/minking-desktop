from __future__ import annotations

import asyncio
import time
import uuid
from decimal import Decimal

from conftest import ADMIN_HEADERS
from fastapi.testclient import TestClient

from app.billing import (
    charge_usage,
    money2,
    quote_usage,
    sign_credit_request,
    usd_text,
)
from app.config import settings
from app.store.gateway import gateway_store, iso_now


def _price_row(**overrides):
    row = {
        "model": "demo-text",
        "provider": "codex",
        "modality": "text",
        "status": "active",
        "official_input_usd_per_1m": "3",
        "official_output_usd_per_1m": "10",
        "official_cached_usd_per_1m": "0.30",
    }
    row.update(overrides)
    return row


def test_quote_usage_multiplier_override_and_unpriced():
    settings_row = {"price_multiplier": "0.12"}
    quoted = quote_usage(
        "demo-text",
        input_tokens=1_000_000,
        settings_row=settings_row,
        price_row=_price_row(),
    )
    assert quoted["official_usd"] == Decimal("3.0000")
    assert quoted["sell_usd"] == Decimal("0.3600")
    assert quoted["unpriced"] is False

    overridden = quote_usage(
        "demo-text",
        input_tokens=1_000_000,
        settings_row=settings_row,
        price_row=_price_row(multiplier_override="0.50"),
    )
    assert overridden["sell_usd"] == Decimal("1.5000")
    assert overridden["multiplier"] == Decimal("0.5000")

    sell_override = quote_usage(
        "demo-text",
        input_tokens=1_000_000,
        settings_row=settings_row,
        price_row=_price_row(sell_override_input="1.25"),
    )
    assert sell_override["sell_usd"] == Decimal("0.3600")
    assert sell_override["official_usd"] == Decimal("3.0000")

    missing = quote_usage(
        "unknown-model",
        input_tokens=1_000_000,
        settings_row=settings_row,
        price_row=None,
    )
    assert missing["unpriced"] is True
    assert missing["official_usd"] == Decimal("0.0000")
    assert missing["sell_usd"] == Decimal("0.0000")


def _insert_user(client: TestClient, email: str, credit: str = "1.00") -> str:
    user_id = uuid.uuid4().hex
    client.portal.call(
        gateway_store.execute,
        "INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES(?,?,?,?,?)",
        (user_id, email, "Ada", credit, iso_now()),
    )
    client.portal.call(
        gateway_store.execute,
        "INSERT INTO api_keys(id,name,key_prefix,fingerprint,owner_user_id,status,created_at,usd_credit) VALUES(?,?,?,?,?,'active',?,?)",
        (user_id, "Ada", "sk-testuser", user_id, user_id, iso_now(), credit),
    )
    return user_id


def test_admin_billing_settings_prices_sync_keeps_override(client):
    settings_body = client.get("/admin/api/billing/settings", headers=ADMIN_HEADERS).json()
    assert Decimal(settings_body["price_multiplier"]) == Decimal("0.12")
    assert settings_body["enforced"] is False
    assert settings_body["new_user_usd"] == "0.00"

    updated = client.put(
        "/admin/api/billing/settings",
        json={"price_multiplier": "0.12", "enforced": False, "new_user_usd": "1.50"},
        headers=ADMIN_HEADERS,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["new_user_usd"] == "1.50"

    synced = client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS)
    assert synced.status_code == 200, synced.text
    assert synced.json()["total"] >= 1

    patched = client.put(
        "/admin/api/billing/prices/gpt-6-astra",
        json={"multiplier_override": "0.20", "sell_override_input": "1.11"},
        headers=ADMIN_HEADERS,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["multiplier_override"] == "0.2000"
    assert patched.json()["sell_override_input"] == "1.1100"

    client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS)
    after = client.get("/admin/api/billing/prices", headers=ADMIN_HEADERS).json()["data"]
    astra = next(item for item in after if item["model"] == "gpt-6-astra")
    assert astra["multiplier_override"] == "0.2000"
    assert astra["sell_override_input"] == "1.1100"
    assert astra["official_input_usd_per_1m"] in {"10", "10.0000", "10.0"}


def test_admin_wallet_credit_csrf_and_ledger(client):
    user_id = _insert_user(client, "credit@example.com", "0.00")
    denied = client.post(
        "/admin/api/wallet/credit",
        json={"email": "credit@example.com", "amount": "5.00", "reason": "test", "idempotency_key": "k1"},
        headers={"X-CSRF-Token": ADMIN_HEADERS["X-CSRF-Token"]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "admin_origin_required"

    first = client.post(
        "/admin/api/wallet/credit",
        json={"email": "credit@example.com", "amount": "5", "reason": "gift", "idempotency_key": "gift-1"},
        headers=ADMIN_HEADERS,
    )
    assert first.status_code == 200, first.text
    assert first.json()["balance_after"] == "5.00"
    second = client.post(
        "/admin/api/wallet/credit",
        json={"email": "credit@example.com", "amount": "5", "reason": "gift", "idempotency_key": "gift-1"},
        headers=ADMIN_HEADERS,
    )
    assert second.status_code == 200, second.text
    assert second.json()["idempotent"] is True
    assert second.json()["balance_after"] == "5.00"

    ledger = client.get(
        "/admin/api/wallet/ledger?email=credit@example.com",
        headers=ADMIN_HEADERS,
    )
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["total"] == 1
    assert ledger.json()["data"][0]["user_id"] == user_id
    assert ledger.json()["data"][0]["kind"] == "grant"


def test_hmac_credit_requires_secret_and_valid_signature(client, monkeypatch):
    _insert_user(client, "hmac@example.com", "0.00")
    body = {"email": "hmac@example.com", "amount": "2.50", "reason": "ops"}
    missing = client.post("/internal/wallet/credit", json=body)
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "invalid_wallet_signature"

    monkeypatch.setattr(settings, "wallet_credit_secret", "wallet-secret")
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    idempotency = "hmac-1"
    signature = sign_credit_request(
        timestamp=timestamp,
        nonce=nonce,
        user="hmac@example.com",
        amount="2.50",
        idempotency=idempotency,
        secret="wallet-secret",
    )
    ok = client.post(
        "/internal/wallet/credit",
        json=body,
        headers={
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Idempotency-Key": idempotency,
            "X-Signature": signature,
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["balance_after"] == "2.50"

    replay = client.post(
        "/internal/wallet/credit",
        json=body,
        headers={
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Idempotency-Key": idempotency,
            "X-Signature": signature,
        },
    )
    assert replay.status_code == 401


def test_key_credit_rejects_negative_amount(client):
    created = client.post("/admin/api/keys", json={"name": "neg"}, headers=ADMIN_HEADERS)
    assert created.status_code == 200, created.text
    denied = client.post(
        f"/admin/api/keys/{created.json()['id']}/credit",
        json={"amount": "-1.00", "reason": "no"},
        headers=ADMIN_HEADERS,
    )
    assert denied.status_code == 422, denied.text
    assert denied.json()["error"]["code"] == "invalid_credit_amount"
    wallet = client.post(
        "/admin/api/wallet/credit",
        json={"email": "nobody@example.com", "amount": "-2.50"},
        headers=ADMIN_HEADERS,
    )
    assert wallet.status_code == 422, wallet.text
    assert wallet.json()["error"]["code"] == "invalid_credit_amount"


def test_charge_usage_idempotent_and_non_negative(client):
    user_id = _insert_user(client, "charge@example.com", "0.10")
    other_id = _insert_user(client, "race@example.com", "0.10")
    quote = {
        "official_usd": Decimal("0.6667"),
        "multiplier": Decimal("0.12"),
        "sell_usd": Decimal("0.0800"),
        "unpriced": False,
    }
    first = client.portal.call(charge_usage, user_id, "req-same", quote)
    second = client.portal.call(charge_usage, user_id, "req-same", quote)
    assert first["charged"] is True
    assert second["idempotent"] is True
    assert first["balance_after"] == second["balance_after"] == "0.02"

    async def race() -> list[dict]:
        return await asyncio.gather(
            charge_usage(other_id, "req-a", quote),
            charge_usage(other_id, "req-b", quote),
        )

    results = client.portal.call(race)
    charged = [item for item in results if item.get("charged")]
    failed = [item for item in results if not item.get("charged")]
    assert len(charged) == 1
    assert len(failed) == 1
    assert failed[0]["reason"] == "insufficient_usd_credit"
    wallet = client.portal.call(
        gateway_store.one, "SELECT usd_credit FROM api_keys WHERE id=?", (other_id,)
    )
    assert usd_text(wallet["usd_credit"]) == "0.02"
    assert money2(wallet["usd_credit"]) >= 0


def test_public_pricing_omits_official_rates(client):
    client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS)
    response = client.get("/portal/api/pricing")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["models"]
    sample = payload["models"][0]
    assert sample["multiplier"]
    assert isinstance(sample["official"], dict) and sample["official"]
    assert isinstance(sample["sell"], dict) and sample["sell"]
    forbidden = {"multiplier_override", "price_multiplier", "sell_override_input", "sell_override_output"}
    assert forbidden.isdisjoint(sample)
    assert sample["model"]
    assert sample["provider"]
    assert sample["modality"] in {"text", "image", "video"}
    if sample["modality"] == "text":
        assert "input_usd_per_1m" in sample
    if sample["modality"] == "image":
        assert "usd_per_image" in sample
    if sample["modality"] == "video":
        assert "usd_per_second" in sample


def test_generation_endpoint_classifier():
    from app.billing import is_generation_endpoint

    assert is_generation_endpoint("/v1/responses")
    assert is_generation_endpoint("/v1/chat/completions")
    assert is_generation_endpoint("/v1/messages")
    assert is_generation_endpoint("/v1beta/models/gemini-3.8-flash:generateContent")
    assert is_generation_endpoint("/v1/v1beta/models/gemini-3.8-flash:streamGenerateContent")
    assert is_generation_endpoint("/v1/images/generations")
    assert is_generation_endpoint("/v1/images/edits")
    assert is_generation_endpoint("/v1/videos")
    assert is_generation_endpoint("/v1/videos/generations")
    assert is_generation_endpoint("/v1/videos/edits")
    assert is_generation_endpoint("/v1/videos/abc/remix")
    assert is_generation_endpoint("/mcp/tools/generate_image")
    assert not is_generation_endpoint("/v1/models")
    assert not is_generation_endpoint("/v1/videos/vid_123")
    assert not is_generation_endpoint("/v1/videos/vid_123/content")


def test_enforced_billing_http_402_and_successful_debit(client):
    import httpx
    from conftest import auth_payload, import_pool
    from test_codex_gateway import key_headers, sse

    from app.http_client import set_http_transport

    imported = import_pool(client, auth_payload("acct-bill"))
    raw = imported["generated_api_key"]["key"]
    key_id = imported["generated_api_key"]["id"]
    client.portal.call(
        gateway_store.execute,
        "UPDATE api_keys SET usd_credit='0.00' WHERE id=?",
        (key_id,),
    )
    assert client.put(
        "/admin/api/billing/settings",
        json={"price_multiplier": "0.12", "enforced": True, "new_user_usd": "0.00"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    assert client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS).status_code == 200

    headers = key_headers(raw)
    text = client.post(
        "/v1/responses",
        json={"model": "gpt-6-astra", "input": "hi"},
        headers=headers,
    )
    assert text.status_code == 402, text.text
    assert text.json()["error"]["code"] == "insufficient_usd_credit"

    image = client.post(
        "/v1/images/generations",
        json={"prompt": "cat", "model": "grok-imagine-image-2.0"},
        headers=headers,
    )
    assert image.status_code == 402, image.text
    assert image.json()["error"]["code"] == "insufficient_usd_credit"

    video = client.post(
        "/v1/videos",
        json={"prompt": "cat walks", "model": "grok-imagine-video-1.5"},
        headers=headers,
    )
    assert video.status_code == 402, video.text
    assert video.json()["error"]["code"] == "insufficient_usd_credit"

    models = client.get("/v1/models", headers=headers)
    assert models.status_code == 200, models.text

    ops = client.post("/admin/api/keys", json={"name": "ops"}, headers=ADMIN_HEADERS)
    assert ops.status_code == 200, ops.text
    ops_key = ops.json()["key"]
    client.portal.call(
        gateway_store.execute, "UPDATE api_keys SET usd_credit='0.00' WHERE id=?", (ops.json()["id"],)
    )
    blocked = client.post("/v1/responses", json={"model": "gpt-6-astra", "input": "ops"}, headers=key_headers(ops_key))
    assert blocked.status_code == 402
    credited = client.post(
        f"/admin/api/keys/{ops.json()['id']}/credit",
        json={"amount": "10.00", "reason": "ops"},
        headers=ADMIN_HEADERS,
    )
    assert credited.status_code == 200, credited.text

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/images/generations"):
            return httpx.Response(200, json={"data": [{"b64_json": "Zm9v"}]})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                "resp-bill",
                usage={"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1_000_000},
            ),
        )

    set_http_transport(httpx.MockTransport(handler))
    ops_call = client.post(
        "/v1/responses",
        json={"model": "gpt-6-astra", "input": "ops"},
        headers=key_headers(ops_key),
    )
    assert ops_call.status_code != 402, ops_call.text

    still_blocked = client.post(
        "/v1/responses",
        json={"model": "gpt-6-astra", "input": "still blocked"},
        headers=headers,
    )
    assert still_blocked.status_code == 402, still_blocked.text

    client.portal.call(
        gateway_store.execute,
        "UPDATE api_keys SET usd_credit='10.00' WHERE id=?",
        (key_id,),
    )
    assert client.put(
        "/admin/api/billing/settings",
        json={"price_multiplier": "0.12", "enforced": True, "new_user_usd": "0.00"},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    charged = client.post(
        "/v1/responses",
        json={"model": "gpt-6-astra", "input": "pay me"},
        headers=headers,
    )
    assert charged.status_code == 200, charged.text
    wallet = client.portal.call(
        gateway_store.one, "SELECT usd_credit FROM api_keys WHERE id=?", (key_id,)
    )
    assert money2(wallet["usd_credit"]) < Decimal("10.00")
    ledger = client.portal.call(
        gateway_store.one,
        "SELECT amount_usd FROM wallet_ledger WHERE key_id=? AND kind='usage' ORDER BY created_at DESC",
        (key_id,),
    )
    assert ledger is not None
    assert money2(ledger["amount_usd"]) < 0


def test_image_quote_is_per_image_and_ignores_sell_override():
    quoted = quote_usage(
        "img",
        images=3,
        settings_row={"price_multiplier": "0.50"},
        price_row={
            "modality": "image",
            "status": "active",
            "official_usd_per_image": "0.04",
            "sell_override_image": "9",
        },
    )
    assert quoted["official_usd"] == Decimal("0.1200")
    assert quoted["sell_usd"] == Decimal("0.0600")
    assert quoted["multiplier"] == Decimal("0.5000")


def test_manual_price_survives_sync_and_unpriced_models_stay_off_the_client_list(client):
    from conftest import auth_payload, import_pool

    import_pool(client, auth_payload("acct-price"))
    assert client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS).status_code == 200
    saved = client.put(
        "/admin/api/billing/prices/custom-image",
        json={"provider": "grok", "modality": "image", "official_usd_per_image": "0.09", "multiplier_override": "0.50"},
        headers=ADMIN_HEADERS,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["price_source"] == "manual"
    assert saved.json()["priced"] is True
    assert saved.json()["sell"]["usd_per_image"] in {"0.0450", "0.045"}
    client.post("/admin/api/billing/prices/sync", headers=ADMIN_HEADERS)
    rows = client.get("/admin/api/billing/prices", headers=ADMIN_HEADERS).json()["data"]
    custom = next(item for item in rows if item["model"] == "custom-image")
    assert custom["official_usd_per_image"] in {"0.0900", "0.09"}
    added = client.post(
        "/admin/api/models",
        json={"provider": "codex", "model_id": "manual-unpriced", "model_type": "text"},
        headers=ADMIN_HEADERS,
    )
    assert added.status_code == 200, added.text
    catalog = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    marked = next(item for item in catalog if item["id"] == "manual-unpriced")
    assert marked["priced"] is False
    listed = client.get("/v1/models", headers={"Authorization": "Bearer " + client.get("/admin/api/keys", headers=ADMIN_HEADERS).json()["data"][0]["key"]})
    assert listed.status_code == 200, listed.text
    assert "manual-unpriced" not in {item["id"] for item in listed.json()["data"]}
    assert "gpt-6-astra" in {item["id"] for item in listed.json()["data"]}
    assert listed.json()["data"][0]["pricing"]["multiplier"]
