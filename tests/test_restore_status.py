from __future__ import annotations

import json

from conftest import ADMIN_HEADERS, auth_payload, import_pool
from test_providers import grok_cli_payload

from app.store.gateway import gateway_store


def _import_grok(client, *payloads: dict) -> None:
    files = [
        ("files[]", (f"auth-{index}.json", json.dumps(payload), "application/json"))
        for index, payload in enumerate(payloads)
    ]
    response = client.post(
        "/admin/api/accounts/import",
        files=files,
        data={"provider": "grok"},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 200, response.text


def _cool(client, account_id: str, model: str) -> None:
    client.portal.call(
        gateway_store.execute,
        """INSERT INTO account_model_health(account_id,model,failures,window_started_at,cooldown_until)
           VALUES(?,?,?,?,?)""",
        (account_id, model, 10, "2026-09-22T00:00:00Z", "2099-01-01T00:00:00Z"),
    )


def test_invalid_account_can_be_marked_active(client):
    import_pool(client, auth_payload("acct-restore"))
    client.portal.call(
        gateway_store.execute,
        """UPDATE accounts
           SET status='invalid', cooldown_until='2099-01-01T00:00:00Z', network_failures=4
           WHERE account_id=?""",
        ("acct-restore",),
    )
    _cool(client, "acct-restore", "gpt-6-astra")

    restored = client.patch(
        "/admin/api/accounts/acct-restore",
        json={"enabled": True},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200, restored.text
    account = client.portal.call(
        gateway_store.one,
        "SELECT status, cooldown_until, network_failures FROM accounts WHERE account_id=?",
        ("acct-restore",),
    )
    assert account["status"] == "active"
    assert account["cooldown_until"] is None
    assert int(account["network_failures"]) == 0
    health = client.portal.call(
        gateway_store.one,
        "SELECT failures, cooldown_until FROM account_model_health WHERE account_id=?",
        ("acct-restore",),
    )
    assert int(health["failures"]) == 0
    assert health["cooldown_until"] is None
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["status"] == "active"


def test_deleted_account_cannot_be_marked_active(client):
    import_pool(client, auth_payload("acct-gone"))
    assert client.delete("/admin/api/accounts/acct-gone", headers=ADMIN_HEADERS).status_code == 200
    denied = client.patch(
        "/admin/api/accounts/acct-gone",
        json={"enabled": True},
        headers=ADMIN_HEADERS,
    )
    assert denied.status_code == 404


def test_restore_model_revives_invalid_accounts_and_clears_cooldown(client):
    _import_grok(
        client,
        grok_cli_payload(user_id="user-live", email="live@example.com"),
        grok_cli_payload(user_id="user-off", email="off@example.com"),
    )
    assert client.patch(
        "/admin/api/accounts/grok:user-off",
        json={"enabled": False},
        headers=ADMIN_HEADERS,
    ).status_code == 200
    client.portal.call(
        gateway_store.execute,
        "UPDATE accounts SET status='invalid', network_failures=2 WHERE account_id=?",
        ("grok:user-live",),
    )
    _cool(client, "grok:user-live", "grok-4.6")

    before = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    grok = next(item for item in before if item["id"] == "grok-4.6")
    assert grok["available"] is False
    assert grok["cooled"] is True

    restored = client.post(
        "/admin/api/models/restore",
        json={"provider": "grok", "model_id": "grok-4.6"},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"ok": True, "available": True, "revived_accounts": 1}
    rows = {
        row["account_id"]: row["status"]
        for row in client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    }
    assert rows["grok:user-live"] == "active"
    assert rows["grok:user-off"] == "disabled"
    health = client.portal.call(
        gateway_store.one,
        "SELECT failures, cooldown_until FROM account_model_health WHERE model=?",
        ("grok-4.6",),
    )
    assert int(health["failures"]) == 0
    assert health["cooldown_until"] is None
    after = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    grok = next(item for item in after if item["id"] == "grok-4.6")
    assert grok["available"] is True
    assert grok["cooled"] is False


def test_restore_model_keeps_invalid_account_when_another_is_healthy(client):
    _import_grok(
        client,
        grok_cli_payload(user_id="user-healthy", email="healthy@example.com"),
        grok_cli_payload(user_id="user-bad", email="bad@example.com"),
    )
    client.portal.call(
        gateway_store.execute,
        "UPDATE accounts SET status='invalid' WHERE account_id=?",
        ("grok:user-bad",),
    )
    _cool(client, "grok:user-healthy", "grok-4.6")
    restored = client.post(
        "/admin/api/models/restore",
        json={"provider": "grok", "model_id": "grok-4.6"},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["revived_accounts"] == 0
    assert restored.json()["available"] is True
    rows = {
        row["account_id"]: row["status"]
        for row in client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    }
    assert rows["grok:user-bad"] == "invalid"
    assert rows["grok:user-healthy"] == "active"


def test_restore_model_revives_deleted_accounts_when_none_healthy(client):
    import_pool(client, auth_payload("acct-deleted-restore"))
    assert client.delete(
        "/admin/api/accounts/acct-deleted-restore", headers=ADMIN_HEADERS
    ).status_code == 200
    assert client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"] == []
    before = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    astra = next(item for item in before if item["id"] == "gpt-6-astra")
    assert astra["available"] is False
    restored = client.post(
        "/admin/api/models/restore",
        json={"provider": "codex", "model_id": "gpt-6-astra"},
        headers=ADMIN_HEADERS,
    )
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"ok": True, "available": True, "revived_accounts": 1}
    listed = client.get("/admin/api/accounts", headers=ADMIN_HEADERS).json()["data"]
    assert listed[0]["account_id"] == "acct-deleted-restore"
    assert listed[0]["status"] == "active"
    after = client.get("/admin/api/models", headers=ADMIN_HEADERS).json()["data"]
    astra = next(item for item in after if item["id"] == "gpt-6-astra")
    assert astra["available"] is True


def test_restore_unknown_model_is_not_found(client):
    missing = client.post(
        "/admin/api/models/restore",
        json={"provider": "grok", "model_id": "not-a-model"},
        headers=ADMIN_HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"
