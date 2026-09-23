from __future__ import annotations

import json

from app.config import settings
from app.store.gateway import gateway_store, iso_now


def test_desktop_login_bootstrap_and_key(monkeypatch):
    from app import portal
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent: list[tuple[str, str]] = []
    answers: list[str] = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))

    with TestClient(app, base_url="https://portal.example") as client:
        client.portal.call(
            gateway_store.execute,
            "INSERT INTO accounts(account_id,credential_path,status,created_at,updated_at,provider) VALUES(?,?,'active',?,?,'codex')",
            ("acct-desktop", "unused", iso_now(), iso_now()),
        )
        cap = client.get("/portal/api/captcha").json()
        mailed = client.post(
            "/portal/api/desktop/auth/send-code",
            json={
                "name": "Ada",
                "email": "ada@example.com",
                "captcha_id": cap["id"],
                "captcha": answers[-1],
            },
        )
        assert mailed.status_code == 200, mailed.text
        verified = client.post(
            "/portal/api/desktop/auth/verify",
            json={
                "challenge_id": mailed.json()["challenge_id"],
                "email": "ada@example.com",
                "code": sent[-1][1],
                "device_id": "ab" * 32,
            },
        )
        assert verified.status_code == 200, verified.text
        body = verified.json()
        assert body["email"] == "ada@example.com"
        assert body["token_type"] == "Bearer"
        assert "sk-" not in body["token"]
        token = body["token"]
        headers = {"Authorization": f"Bearer {token}"}
        bootstrap = client.get("/portal/api/desktop/bootstrap", headers=headers)
        assert bootstrap.status_code == 200, bootstrap.text
        payload = bootstrap.json()
        assert payload["user"]["email"] == "ada@example.com"
        assert payload["public_base_url"] == "https://portal.example/v1"
        assert payload["models"]
        catalog_models = payload["catalog"]["models"]
        assert catalog_models[0]["slug"] in payload["models"]
        assert catalog_models[0]["display_name"]
        assert catalog_models[0]["context_window"]
        assert catalog_models[0]["pricing"]["multiplier"]
        assert catalog_models[0]["pricing"]["official"]
        assert catalog_models[0]["pricing"]["sell"]
        assert set(payload["key"]) == {"prefix", "status"}
        assert payload["key"]["prefix"].startswith("sk-ts-")
        assert len(payload["key"]["prefix"]) == 12
        ids = {item["id"] for item in payload["harnesses"]}
        assert {"codex", "workbuddy", "claude_code", "zcode"} <= ids
        assert "trae" not in ids
        assert "qcode" not in ids
        assert "antigravity" not in ids
        claude = next(item for item in payload["harnesses"] if item["id"] == "claude_code")
        assert claude["one_click"] is True
        assert claude["cloud"]["base_url"] == "https://portal.example"
        flags = {item["id"]: item["one_click"] for item in payload["harnesses"]}
        assert flags["zcode"] is True
        assert payload["desktop"]["protocol"] == "minking"
        assert payload["desktop"]["import_url"].startswith("minking://import?")
        assert "ada%40example.com" in payload["desktop"]["import_url"]
        assert "sk-" not in payload["desktop"]["import_url"]
        assert "sk-ts-" not in json.dumps(payload["harnesses"])
        revealed = client.post("/portal/api/desktop/key", headers=headers)
        assert revealed.status_code == 200, revealed.text
        key = revealed.json()["key"]
        assert key.startswith("sk-ts-")
        assert key not in payload["desktop"]["import_url"]
        listed = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert listed.status_code == 200
        cookie_bootstrap = client.get("/portal/api/desktop/bootstrap")
        assert cookie_bootstrap.status_code == 401
        web_code = client.post(
            "/portal/api/auth/send-code",
            json={
                "name": "Ada",
                "email": "ada@example.com",
                "captcha_id": cap["id"],
                "captcha": answers[-1],
            },
        )
        assert web_code.status_code == 403


def test_desktop_skills_list_and_zip(monkeypatch):
    import io
    import zipfile

    from app import portal
    from app.client_skills_pack import list_client_skills
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent: list[tuple[str, str]] = []
    answers: list[str] = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))

    packed = list_client_skills()
    assert packed
    media = next(item for item in packed if item["name"] == "minking-media")
    assert "SKILL.md" in media["files"]
    assert media["sha256"]

    with TestClient(app, base_url="https://portal.example") as client:
        cap = client.get("/portal/api/captcha").json()
        mailed = client.post(
            "/portal/api/desktop/auth/send-code",
            json={
                "name": "Ada",
                "email": "skills@example.com",
                "captcha_id": cap["id"],
                "captcha": answers[-1],
            },
        )
        assert mailed.status_code == 200, mailed.text
        verified = client.post(
            "/portal/api/desktop/auth/verify",
            json={
                "challenge_id": mailed.json()["challenge_id"],
                "email": "skills@example.com",
                "code": sent[-1][1],
                "device_id": "cd" * 32,
            },
        )
        assert verified.status_code == 200, verified.text
        headers = {"Authorization": f"Bearer {verified.json()['token']}"}
        listed = client.get("/portal/api/desktop/skills", headers=headers)
        assert listed.status_code == 200, listed.text
        names = {item["name"] for item in listed.json()["skills"]}
        assert "minking-media" in names
        zipped = client.get("/portal/api/desktop/skills/minking-media", headers=headers)
        assert zipped.status_code == 200, zipped.text
        assert "zip" in zipped.headers.get("content-type", "")
        assert zipped.headers.get("x-skill-sha256") == media["sha256"]
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
            assert "SKILL.md" in archive.namelist()
            assert any(name.startswith("scripts/") for name in archive.namelist())
        missing = client.get("/portal/api/desktop/skills/not-a-skill", headers=headers)
        assert missing.status_code == 404
        cookie_list = client.get("/portal/api/desktop/skills")
        assert cookie_list.status_code == 401


def _desktop_register(client, sent, answers, *, name, email, device_id):
    cap = client.get("/portal/api/captcha").json()
    mailed = client.post(
        "/portal/api/desktop/auth/send-code",
        json={"name": name, "email": email, "captcha_id": cap["id"], "captcha": answers[-1]},
    )
    assert mailed.status_code == 200, mailed.text
    return client.post(
        "/portal/api/desktop/auth/verify",
        json={
            "challenge_id": mailed.json()["challenge_id"],
            "email": email,
            "code": sent[-1][1],
            "device_id": device_id,
        },
    )


def test_desktop_signup_reward_is_once_per_computer(monkeypatch):
    from app import portal
    from app.main import app
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example/v1")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent: list[tuple[str, str]] = []
    answers: list[str] = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    same = "a" * 64
    other = "b" * 64

    with TestClient(app, base_url="https://portal.example") as client:
        client.portal.call(gateway_store.execute, "UPDATE billing_settings SET new_user_usd='8.00' WHERE id=1")
        cap = client.get("/portal/api/captcha").json()
        mailed = client.post(
            "/portal/api/desktop/auth/send-code",
            json={"name": "Ada", "email": "ada-device@example.com", "captcha_id": cap["id"], "captcha": answers[-1]},
        )
        assert mailed.status_code == 200, mailed.text
        missing = client.post(
            "/portal/api/desktop/auth/verify",
            json={
                "challenge_id": mailed.json()["challenge_id"],
                "email": "ada-device@example.com",
                "code": sent[-1][1],
            },
        )
        assert missing.status_code == 422, missing.text
        assert missing.json()["error"]["code"] == "device_id_required"
        first = client.post(
            "/portal/api/desktop/auth/verify",
            json={
                "challenge_id": mailed.json()["challenge_id"],
                "email": "ada-device@example.com",
                "code": sent[-1][1],
                "device_id": same,
            },
        )
        assert first.status_code == 200, first.text
        assert first.json()["reward_notice"] == ""
        wallet = client.get("/portal/api/wallet", headers={"Authorization": f"Bearer {first.json()['token']}"})
        assert wallet.status_code == 200, wallet.text
        assert wallet.json()["usd_credit"] == "8.00"
        tools = client.get("/portal/api/harnesses", headers={"Authorization": f"Bearer {first.json()['token']}"})
        assert tools.status_code == 200, tools.text
        assert "codex" in {item["id"] for item in tools.json()["data"]}
        assert "%USERPROFILE%" not in tools.text

        second = _desktop_register(client, sent, answers, name="Bea", email="bea-device@example.com", device_id=same)
        assert second.status_code == 200, second.text
        assert "已经领取过新人注册奖励" in second.json()["reward_notice"]
        second_wallet = client.get("/portal/api/wallet", headers={"Authorization": f"Bearer {second.json()['token']}"})
        assert second_wallet.json()["usd_credit"] == "0.00"

        third = _desktop_register(client, sent, answers, name="Cara", email="cara-device@example.com", device_id=other)
        assert third.status_code == 200, third.text
        assert third.json()["reward_notice"] == ""
        third_wallet = client.get("/portal/api/wallet", headers={"Authorization": f"Bearer {third.json()['token']}"})
        assert third_wallet.json()["usd_credit"] == "8.00"
