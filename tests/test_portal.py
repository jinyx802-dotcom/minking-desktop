from __future__ import annotations

import io
import json
import sqlite3
import tomllib
import zipfile
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import settings
from app.providers import registry
from app.store.gateway import gateway_store, iso_now
from conftest import ADMIN_PASSWORD


def test_email_login_one_key_zip_and_isolation(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent = []
    answers = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    origin = {"Origin": "https://portal.example"}
    with TestClient(app, base_url="https://portal.example") as client:
        assert client.get("/portal").status_code == 200
        assert client.get("/portal/api/auth/session").status_code == 401
        admin = client.post("/admin/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}, headers=origin).json()
        admin_headers = {**origin, "X-CSRF-Token": admin["csrf_token"]}
        key = client.post("/admin/api/keys", json={"name": "other"}, headers=admin_headers).json()
        assert client.post(f"/admin/api/keys/{key['id']}/bundle", headers=admin_headers).status_code == 503
        # Healthy upstream account for the generated model catalog.
        client.portal.call(gateway_store.execute,
            "INSERT INTO accounts(account_id,credential_path,status,created_at,updated_at,provider) VALUES(?,?,'active',?,?,'codex')",
            ("acct", "unused", iso_now(), iso_now()))

        def sign_in(name, email):
            cap = client.get("/portal/api/captcha").json()
            body = {"name": name, "email": email, "captcha_id": cap["id"], "captcha": answers[-1]}
            mailed = client.post("/portal/api/auth/send-code", json=body, headers=origin)
            assert mailed.status_code == 200, mailed.text
            assert client.post("/portal/api/auth/send-code", json=body, headers=origin).status_code == 422
            challenge = mailed.json()["challenge_id"]
            verified = client.post("/portal/api/auth/verify", json={"challenge_id":challenge,"email":email,"code":sent[-1][1]}, headers=origin)
            assert verified.status_code == 200, verified.text
            assert client.post("/portal/api/auth/verify", json={"challenge_id":challenge,"email":email,"code":sent[-1][1]}, headers=origin).status_code == 401
            return verified.json()["csrf_token"]

        csrf = sign_in("Alice", "alice@example.com")
        dashboard = client.get("/portal/api/dashboard").json()
        assert dashboard["metrics"]["calls"] == 0
        assert dashboard["metrics"]["success_rate"] is None
        assert dashboard["user"]["usd_credit"] == "0.00"
        assert key["key"] not in json.dumps(dashboard)
        assert dashboard["desktop"]["protocol"] == "minking"
        assert dashboard["desktop"]["import_url"].startswith("minking://import?")
        assert "alice%40example.com" in dashboard["desktop"]["import_url"]
        assert key["key"] not in dashboard["desktop"]["import_url"]
        assert "sk-" not in dashboard["desktop"]["import_url"]
        page = client.get("/portal")
        assert "在 MinKing 客户端中打开" in page.text
        assert "minking://" in page.text
        assert "登录" in page.text
        assert "注册" in page.text
        assert "调用明细" in page.text
        assert "资金流水" in page.text
        assert "卡密兑换" in page.text
        assert "C:/Users/你的用户名/.codex/codex-models.json" in page.text
        assert "不识别 %USERPROFILE%" in page.text
        bundle = client.post("/portal/api/bundle", headers={**origin,"X-CSRF-Token":csrf})
        assert bundle.status_code == 200, bundle.text if bundle.status_code != 200 else ""
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
            assert set(archive.namelist()) == {"config.toml",".env","codex-models.json","auth.json"}
            config = tomllib.loads(archive.read("config.toml").decode())
            provider = config["model_providers"]["minkingapi"]
            assert provider["base_url"] == "https://portal.example/v1"
            assert provider["requires_openai_auth"] is False
            assert provider["env_key"] == "OPENAI_API_KEY"
            assert provider["supports_websockets"] is False
            assert provider["name"] == "MinKing API Composite"
            assert config["model_provider"] == "minkingapi"
            assert config["disable_response_storage"] is True
            assert config["review_model"] == config["model"]
            assert "%userprofile%" not in archive.read("config.toml").decode().lower()
            assert config["model_catalog_json"].startswith("C:/Users/")
            assert config["model_catalog_json"].endswith("/.codex/codex-models.json")
            catalog_models = json.loads(archive.read("codex-models.json"))["models"]
            assert config["model"] in {m["slug"] for m in catalog_models}
            first = catalog_models[0]
            assert first["display_name"]
            assert first["context_window"]
            assert "supported_reasoning_levels" in first
            assert "experimental_supported_tools" in first
            env = dict(
                line.split("=", 1)
                for line in archive.read(".env").decode().splitlines()
                if line and "=" in line
            )
            first_key = env["OPENAI_API_KEY"]
            assert env["OPENAI_BASE_URL"] == "https://portal.example/v1"
            assert "MINKING_API_KEY" not in env
            assert json.loads(archive.read("auth.json")) == {"OPENAI_API_KEY": first_key}
            assert first_key != key["key"]
            assert first_key
        listed = client.get("/admin/api/keys", headers=admin_headers).json()["data"]
        alice = next(item for item in listed if item["name"] == "Alice")
        assert alice["email"] == "alice@example.com"
        other = next(item for item in listed if item["id"] == key["id"])
        assert other["email"] in (None, "")
        assert client.get("/portal/api/dashboard").json()["metrics"]["downloads"] == 1
        assert client.post("/portal/api/key/rotate", headers={**origin,"X-CSRF-Token":csrf}).status_code == 200
        assert client.get("/v1/models", headers={"Authorization":f"Bearer {first_key}"}).status_code == 401
        assert client.post("/portal/api/auth/logout", headers={**origin,"X-CSRF-Token":csrf}).status_code == 200
        client.portal.call(gateway_store.execute, "UPDATE portal_codes SET created_at=? WHERE email=?", ("2000-01-01T00:00:00Z", "alice@example.com"))
        csrf2 = sign_in("Alice", "alice@example.com")
        assert client.get("/portal/api/dashboard").json()["metrics"]["downloads"] == 1
        assert client.post("/portal/api/bundle", headers={**origin,"X-CSRF-Token":csrf2}).status_code == 200
        client.portal.call(gateway_store.execute,
            "INSERT INTO call_records(request_id,started_at,key_id,key_name,endpoint,model,status,total_tokens) VALUES(?,?,?,?,?,?,?,?)",
            ("other-call", iso_now(), key["id"], "other", "/v1/responses", "gpt-6-astra", "success", 100))
        assert client.get("/portal/api/dashboard").json()["metrics"]["calls"] == 0
        csrf3 = sign_in("Bob", "bob@example.com")
        assert client.get("/portal/api/dashboard").json()["metrics"]["downloads"] == 0
        assert client.get("/portal/api/dashboard").json()["metrics"]["calls"] == 0
        assert client.post("/portal/api/bundle", headers={**origin,"X-CSRF-Token":csrf2}).status_code == 403
        assert client.post("/portal/api/bundle", headers={**origin,"X-CSRF-Token":csrf3}).status_code == 200
        dashboard = client.get("/portal/api/dashboard").json()
        assert dashboard["balance"] == "0.00"
        assert dashboard["today_calls"] == 0
        assert dashboard["calls_7d"] == 0
        assert dashboard["calls_30d"] == 0
        assert dashboard["metrics"]["failure_rate"] is None
        assert "input_tokens" in dashboard["metrics"]
        assert "output_tokens" in dashboard["metrics"]
        assert "cached_tokens" in dashboard["metrics"]
        alice_key = next(item for item in client.get("/admin/api/keys", headers=admin_headers).json()["data"] if item["name"] == "Alice")
        bob_key = next(item for item in client.get("/admin/api/keys", headers=admin_headers).json()["data"] if item["name"] == "Bob")
        client.portal.call(
            gateway_store.execute,
            "INSERT INTO call_records(request_id,started_at,key_id,key_name,endpoint,model,status,total_tokens) VALUES(?,?,?,?,?,?,?,?)",
            ("alice-call", iso_now(), alice_key["id"], "Alice", "/v1/responses", "gpt-6-astra", "success", 9),
        )
        client.portal.call(
            gateway_store.execute,
            "INSERT INTO call_records(request_id,started_at,key_id,key_name,endpoint,model,status,total_tokens) VALUES(?,?,?,?,?,?,?,?)",
            ("bob-secret-call", iso_now(), bob_key["id"], "Bob", "/v1/responses", "gpt-6-astra", "success", 7),
        )
        bob_calls = {item["request_id"] for item in client.get("/portal/api/calls").json()["data"]}
        assert "bob-secret-call" in bob_calls
        assert "alice-call" not in bob_calls
        assert "other-call" not in bob_calls
        client.portal.call(gateway_store.execute, "UPDATE portal_codes SET created_at=? WHERE email=?", ("2000-01-01T00:00:00Z", "alice@example.com"))
        sign_in("Alice", "alice@example.com")
        alice_calls = {item["request_id"] for item in client.get("/portal/api/calls").json()["data"]}
        assert "alice-call" in alice_calls
        assert "bob-secret-call" not in alice_calls
        assert "other-call" not in alice_calls
        pricing = client.get("/portal/api/pricing").json()
        assert pricing["models"]
        sample = pricing["models"][0]
        assert sample["multiplier"]
        assert sample["official"]
        assert sample["sell"]
        assert "price_multiplier" not in sample


def test_registered_adapter_drives_route_api(client, monkeypatch):
    from conftest import ADMIN_HEADERS
    dummy = SimpleNamespace(id="future", display_name="Future", capabilities={"chat"}, ready=True)
    monkeypatch.setitem(registry._ADAPTERS, "future", dummy)
    providers = client.get("/admin/api/providers").json()["data"]
    assert any(item["id"] == "future" for item in providers)
    key = client.post("/admin/api/keys", json={"name":"future-user"}, headers=ADMIN_HEADERS).json()
    assert client.put(f"/admin/api/keys/{key['id']}/route", json={"provider":"future","preferred_account_id":None}, headers=ADMIN_HEADERS).status_code == 200
    routes = client.get("/admin/api/keys").json()["data"]
    assert any(route["provider"] == "future" for route in next(item for item in routes if item["id"] == key["id"])["routes"])


async def test_sqlite_migrates_existing_api_keys(tmp_path):
    from app.store.gateway import GatewayStore
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE api_keys(id TEXT PRIMARY KEY,name TEXT NOT NULL,key_prefix TEXT NOT NULL,fingerprint TEXT UNIQUE NOT NULL,key_ciphertext TEXT,fast_enabled INTEGER,status TEXT,last_used_at TEXT,created_at TEXT)")
    store = GatewayStore()
    await store.start(path)
    columns = await store.all("PRAGMA table_info(api_keys)")
    assert "owner_user_id" in {item["name"] for item in columns}
    assert await store.one("SELECT name FROM sqlite_master WHERE type='table' AND name='portal_users'")
    await store.stop()
    await store.start(path)
    assert await store.one("SELECT name FROM sqlite_master WHERE type='index' AND name='uq_api_keys_owner'")
    await store.stop()


def test_admin_portal_routes_and_bundle_v1_normalization(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://ceshi.007ka.cn/maliang/v1")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: b"png")

    with TestClient(app, base_url="https://ceshi.007ka.cn") as client:
        # Check /admin/portal page
        resp = client.get("/admin/portal")
        assert resp.status_code == 200
        assert "portal.js" in resp.text
        assert "MinKing AI" in resp.text
        # Check /admin/portal/api/captcha
        cap = client.get("/admin/portal/api/captcha")
        assert cap.status_code == 200
        assert "image" in cap.json()
        # Check static asset under /admin/static
        static_resp = client.get("/admin/static/app.js")
        assert static_resp.status_code == 200
        page = client.get("/v1/portal")
        assert page.status_code == 200
        assert 'content="/maliang/v1"' in page.text
        assert "/maliang/v1/static/portal.js" in page.text
        assert client.get("/v1/portal/api/captcha").status_code == 200
        assert client.get("/v1/static/portal.js").status_code == 200
        assert client.get("/v1/static/portal.css").status_code == 200


def test_v1_portal_keeps_v1_prefix_when_public_url_omits_it(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://ceshi.007ka.cn/maliang")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: b"png")

    with TestClient(app, base_url="https://ceshi.007ka.cn") as client:
        page = client.get("/v1/portal")
        assert page.status_code == 200
        assert 'content="/maliang/v1"' in page.text
        assert "/maliang/v1/static/portal.css" in page.text
        assert "/maliang/v1/static/portal.js" in page.text
        assert 'href="/maliang/static/portal.css' not in page.text


def test_v1_send_code_accepts_https_origin_behind_http_proxy(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://ceshi.007ka.cn/maliang/v1")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    answers = []
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: None)

    with TestClient(app, base_url="http://127.0.0.1") as client:
        cap = client.get("/v1/portal/api/captcha").json()
        body = {"name": "Ada", "email": "ada@example.com", "captcha_id": cap["id"], "captcha": answers[-1]}
        denied = client.post(
            "/v1/portal/api/auth/send-code",
            json=body,
            headers={"Origin": "https://evil.example", "X-Forwarded-Proto": "http"},
        )
        assert denied.status_code == 403
        mailed = client.post(
            "/v1/portal/api/auth/send-code",
            json=body,
            headers={"Origin": "https://ceshi.007ka.cn", "X-Forwarded-Proto": "http"},
        )
        assert mailed.status_code == 200, mailed.text


def test_json_ready_converts_decimal():
    from decimal import Decimal

    from app.portal import json_ready

    payload = json_ready({"calls": Decimal("12"), "rate": Decimal("1.5"), "rows": [{"n": Decimal("0")}]})
    assert payload == {"calls": 12, "rate": 1.5, "rows": [{"n": 0}]}
    json.dumps(payload)


def test_login_without_name_existing_user_and_unknown_email(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent = []
    answers = []
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    origin = {"Origin": "https://portal.example"}
    with TestClient(app, base_url="https://portal.example") as client:
        cap = client.get("/portal/api/captcha").json()
        unknown = client.post(
            "/portal/api/auth/send-code",
            json={"name": "", "email": "new@example.com", "captcha_id": cap["id"], "captcha": answers[-1]},
            headers=origin,
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "email_not_registered"
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
        client.post("/portal/api/auth/logout", headers={**origin, "X-CSRF-Token": verified.json()["csrf_token"]})
        client.portal.call(gateway_store.execute, "UPDATE portal_codes SET created_at=? WHERE email=?", ("2000-01-01T00:00:00Z", "ada@example.com"))
        cap = client.get("/portal/api/captcha").json()
        login = client.post(
            "/portal/api/auth/send-code",
            json={"name": "", "email": "ada@example.com", "captcha_id": cap["id"], "captcha": answers[-1]},
            headers=origin,
        )
        assert login.status_code == 200, login.text


def test_captcha_is_case_insensitive(monkeypatch):
    from app import portal
    from app.main import app

    monkeypatch.setattr(settings, "portal_enabled", True)
    monkeypatch.setattr(settings, "gateway_public_base_url", "https://portal.example")
    monkeypatch.setattr(settings, "portal_smtp_host", "mail.example")
    monkeypatch.setattr(settings, "portal_smtp_from", "login@example.com")
    sent = []
    answers = []
    chars = iter("Ab12C")
    monkeypatch.setattr(portal.secrets, "choice", lambda _alphabet: next(chars))
    monkeypatch.setattr(portal, "_send_mail", lambda address, code: sent.append((address, code)))
    monkeypatch.setattr(portal, "_captcha_png", lambda answer: (answers.append(answer) or b"png"))
    origin = {"Origin": "https://portal.example"}
    with TestClient(app, base_url="https://portal.example") as client:
        cap = client.get("/portal/api/captcha").json()
        assert answers[-1] == "Ab12C"
        mailed = client.post(
            "/portal/api/auth/send-code",
            json={
                "name": "Ada",
                "email": "case@example.com",
                "captcha_id": cap["id"],
                "captcha": "  ab12c  ",
            },
            headers=origin,
        )
        assert mailed.status_code == 200, mailed.text
