from __future__ import annotations

import json
import sys
from pathlib import Path

from minking_desktop.api import ApiError
from minking_desktop.app import (
    WINDOW_HEIGHT,
    WINDOW_MIN_SIZE,
    WINDOW_WIDTH,
    DesktopApp,
    JsBridge,
    missing_webview_message,
)
from minking_desktop.harness import detect_harnesses
from minking_desktop.secrets import SecretStore


class FakeClient:
    def __init__(self) -> None:
        self.token = "desktop-token"
        self.base_url = "https://portal.example/v1"

    def bootstrap(self):
        return {
            "user": {"name": "Ada", "email": "ada@example.com", "usd_credit": "3"},
            "key": {"prefix": "sk-ts-abcdef", "status": "active"},
            "public_base_url": "https://portal.example/v1",
            "models": ["gpt-6-astra"],
            "catalog": {
                "models": [
                    {
                        "slug": "gpt-6-astra",
                        "display_name": "GPT-6 Astra",
                        "context_window": 1000000,
                        "experimental_supported_tools": ["image_generation"],
                    }
                ]
            },
            "harnesses": [],
        }

    def reveal_key(self):
        return {"key": "sk-ts-secret-key", "prefix": "sk-ts-abcdef"}

    def fetch_model_catalog(self, api_key: str):
        assert api_key == "sk-ts-secret-key"
        return [{"id": "gpt-6-astra", "official_id": "gpt-6-astra", "type": "text"}]

    def fetch_codex_catalog(self, api_key: str):
        assert api_key == "sk-ts-secret-key"
        return None

    def logout(self):
        self.token = None


def test_apply_and_restore_without_network(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "chatgpt-official"}}), encoding="utf-8")
    app = DesktopApp(home=home, appdata=tmp_path / "MinKing")
    app.client = FakeClient()
    store = SecretStore(
        appdata=tmp_path / "MinKing",
        protect=lambda data: b"ENC" + data,
        unprotect=lambda data: data[3:],
    )
    store.save_token("desktop-token")
    app.store = store
    result = app.apply_cloud("codex")
    assert result["ok"] is True
    assert "配置已写入" in result["message"]
    assert "会话文件没有移动" in result["message"]
    live = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert live["OPENAI_API_KEY"] == "sk-ts-secret-key"
    written = json.loads((home / ".codex" / "codex-models.json").read_text(encoding="utf-8"))
    assert written["models"][0]["display_name"] == "GPT-6 Astra"
    assert written["models"][0]["context_window"] == 1000000
    config = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert "%userprofile%" not in config.lower()
    assert f'model_catalog_json = "{(home / ".codex" / "codex-models.json").as_posix()}"' in config
    restored = app.restore_official("codex")
    assert restored["ok"] is True
    back = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert back == {"tokens": {"access_token": "chatgpt-official"}}
    detected = detect_harnesses(home=home, public_base="https://portal.example/v1")
    assert next(item for item in detected if item["id"] == "codex")["mode"] == "official"
    log = (tmp_path / "MinKing" / "desktop.log")
    if log.is_file():
        text = log.read_text(encoding="utf-8")
        assert "sk-ts-secret-key" not in text
        assert "chatgpt-official" not in text


def test_apply_fetches_catalog_when_bootstrap_is_slug_only(tmp_path):
    class SlugClient(FakeClient):
        def bootstrap(self):
            payload = super().bootstrap()
            payload.pop("catalog", None)
            return payload

        def fetch_codex_catalog(self, api_key: str):
            assert api_key == "sk-ts-secret-key"
            return {
                "models": [
                    {
                        "slug": "gpt-6-astra",
                        "display_name": "GPT-6 Astra",
                        "context_window": 1000000,
                        "experimental_supported_tools": ["image_generation"],
                    }
                ]
            }

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "chatgpt-official"}}), encoding="utf-8")
    app = DesktopApp(home=home, appdata=tmp_path / "MinKing")
    app.client = SlugClient()
    store = SecretStore(
        appdata=tmp_path / "MinKing",
        protect=lambda data: b"ENC" + data,
        unprotect=lambda data: data[3:],
    )
    store.save_token("desktop-token")
    app.store = store
    result = app.apply_cloud("codex")
    assert result["ok"] is True
    written = json.loads((home / ".codex" / "codex-models.json").read_text(encoding="utf-8"))
    assert written["models"][0]["display_name"] == "GPT-6 Astra"


def test_window_size_constants():
    assert WINDOW_WIDTH == 1180
    assert WINDOW_HEIGHT == 760
    assert WINDOW_MIN_SIZE == (960, 640)


def test_missing_webview_message_source_vs_frozen(monkeypatch):
    err = ImportError("DLL load failed while importing _ssl")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    source = missing_webview_message(err)
    assert "MinKingAI.exe" in source
    assert "requirements.txt" in source
    assert "DLL load failed while importing _ssl" in source
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = missing_webview_message(err)
    assert "MinKingAI.exe" in frozen
    assert "desktop.log" in frozen
    assert "requirements.txt" not in frozen
    assert "DLL load failed while importing _ssl" in frozen


def test_missing_webview_message_on_macos(monkeypatch):
    monkeypatch.setattr("minking_desktop.app._is_macos", lambda: True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen = missing_webview_message(ImportError("webview"))
    assert "MinKingAI.app" in frozen
    assert "Application Support/MinKing/desktop.log" in frozen
    assert "MinKingAI.exe" not in frozen
    assert "%APPDATA%" not in frozen


def test_pack_smoke_accepts_tls_and_binds():
    from minking_desktop.app import pack_smoke

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Opener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://ceshi.007ka.cn/")
            assert timeout == 20
            return Response()

    assert pack_smoke(opener=Opener()) == 0


def test_close_should_quit_without_tray(tmp_path):
    app = DesktopApp(home=tmp_path / "home", appdata=tmp_path / "MinKing")
    assert app.close_should_quit() is True
    app.tray = object()
    assert app.close_should_quit() is False
    app._quitting = True
    assert app.close_should_quit() is True


def test_instance_quit_message_calls_quit(tmp_path, monkeypatch):
    app = DesktopApp(home=tmp_path / "home", appdata=tmp_path / "MinKing")
    called = []
    monkeypatch.setattr(app, "quit", lambda: called.append("quit"))
    app.handle_instance_message({"cmd": "quit"})
    assert called == ["quit"]


def _wired_app(tmp_path, client=None):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text(json.dumps({"tokens": {"access_token": "chatgpt-official"}}), encoding="utf-8")
    app = DesktopApp(home=home, appdata=tmp_path / "MinKing")
    app.client = client or FakeClient()
    store = SecretStore(
        appdata=tmp_path / "MinKing",
        protect=lambda data: b"ENC" + data,
        unprotect=lambda data: data[3:],
    )
    store.save_token("desktop-token")
    app.store = store
    return app, home


def test_unified_workspace_preserves_cloud_settings_and_local_lifecycle(tmp_path):
    app, _ = _wired_app(tmp_path)
    app.store.save_settings({"email": "ada@example.com", "guide_done": True})
    class Local:
        stopped = False
        def state(self):
            return {"service_running": not self.stopped}
        def control(self, action, port=None):
            self.stopped = action == "stop"
            return {"ok": True}
    local = Local()
    app._local_bridge = local
    bridge = JsBridge(app)
    assert bridge.set_workspace("local")["ok"]
    assert bridge.workspace_state()["workspace"] == "local"
    assert app.store.load_settings()["guide_done"] is True
    bridge.set_workspace("cloud")
    assert bridge.local_state()["service_running"]
    app.logout()
    assert bridge.local_state()["service_running"]
    app.quit()
    assert local.stopped


def test_unified_cloud_call_uses_cloud_key_and_rejects_other_paths(tmp_path, monkeypatch):
    app, _ = _wired_app(tmp_path)
    bridge = JsBridge(app)
    seen = []
    def request(method, url, payload, headers, timeout):
        seen.append((method, url, headers))
        assert isinstance(payload["input"], list)
        assert payload["reasoning"]["context"] == "all_turns"
        assert payload["stream"] is True
        return 200, {"output": []}
    monkeypatch.setattr("minking_desktop.api.default_request", request)
    assert bridge.cloud_models_state()["accounts"][0]["models"][0]["id"] == "gpt-6-astra"
    assert bridge.cloud_model_request("/v1/responses", {"model": "gpt-6-astra", "input": "test"})["ok"]
    assert seen[0][1] == app.public_base.rstrip("/") + "/responses"
    assert seen[0][2]["Authorization"] == "Bearer sk-ts-secret-key"
    assert app._local_bridge is None
    assert not bridge.cloud_model_request("/v1/videos/../../admin", None, "GET")["ok"]
    assert not bridge.cloud_model_request("/v1/videos/..", None, "GET")["ok"]
    app.logout()
    assert not bridge.cloud_model_request("/v1/responses", {})["ok"]
    assert len(seen) == 1


def test_default_exe_enters_existing_unified_app(monkeypatch):
    from minking_desktop import app as module
    monkeypatch.setattr(module, "configure_logging", lambda: None)
    monkeypatch.setattr(module, "_run", lambda argv: 37)
    assert module.run([]) == 37


def test_cloud_error_preserves_reason_without_credentials(tmp_path, monkeypatch):
    app, _ = _wired_app(tmp_path)
    def request(*args):
        return 400, {"error": {"message": "Input must be a list; sk-ts-secret-key desktop-token Bearer private"}}
    monkeypatch.setattr("minking_desktop.api.default_request", request)
    result = JsBridge(app).cloud_model_request("/v1/responses", {"model": "gpt-5.6-sol", "input": "test"})
    assert not result["ok"] and result["status"] == 400
    assert "Input must be a list" in result["error"]
    assert "sk-ts-secret-key" not in result["error"]
    assert "desktop-token" not in result["error"]
    assert "private" not in result["error"]


def test_local_tool_apply_and_restore_are_independent_of_cloud_login(tmp_path):
    app, home = _wired_app(tmp_path)
    app.client.token = None
    class Local:
        def state(self):
            return {"service_running": True, "base_url": "http://127.0.0.1:18787/v1", "api_key": "local-test",
                    "accounts": [{"id": "grok", "models": [{"id": "grok/official-text", "type": "text"}]}],
                    "config": {"enabled_providers": ["grok"]}}
    app._local_bridge = Local()
    bridge = JsBridge(app)
    original = (home / '.codex/auth.json').read_bytes()
    preview = bridge.local_tools('preview', 'codex')
    assert preview['base_url'] == 'http://127.0.0.1:18787/v1'
    assert preview['models'][0]['id'] == 'grok/official-text'
    assert not bridge.local_tools('preview_restore', 'codex')['ok']
    backup = bridge.local_tools('backup', 'codex')
    assert backup['ok'] and backup['created']
    assert Path(backup['backup_path']).is_relative_to(app.appdata / 'local-profiles')
    assert (home / '.codex/auth.json').read_bytes() == original
    applied = bridge.local_tools('apply', 'codex', ['grok/official-text'])
    assert applied["ok"] is True
    assert applied["session_sync"] is True
    assert "配置已写入" in applied["message"]
    assert bridge.local_tools('preview_restore', 'codex')['ok']
    env = (home / '.codex/.env').read_text()
    assert 'OPENAI_BASE_URL=http://127.0.0.1:18787/v1' in env
    assert (home / '.codex/skills/minking-media/SKILL.md').is_file()
    config = (home / '.codex/config.toml').read_text()
    assert '127.0.0.1:18787/v1' in config and 'grok/official-text' in config
    assert "model_context_window = 50000000" in config
    written = json.loads((home / ".codex" / "codex-models.json").read_text(encoding="utf-8"))
    entry = written["models"][0]
    assert entry["slug"] == "grok/official-text"
    assert entry["context_window"] == 50_000_000
    assert entry["supported_reasoning_levels"]
    assert entry["shell_type"] == "shell_command"
    assert bridge.local_tools('restore', 'codex')["ok"]
    assert (home / '.codex/auth.json').read_bytes() == original
    assert not (app.appdata / 'profiles').exists()
    assert bridge.local_tools('apply', 'claude_code', ['grok/official-text'])["ok"]
    settings = json.loads((home / '.claude/settings.json').read_text())
    assert settings['env']['ANTHROPIC_DEFAULT_SONNET_MODEL'] == 'grok/official-text'
    assert settings['env']['ANTHROPIC_BASE_URL'] == 'http://127.0.0.1:18787'


def test_preview_and_backup_do_not_write_until_apply(tmp_path):
    app, home = _wired_app(tmp_path)
    original = (home / ".codex" / "auth.json").read_text(encoding="utf-8")
    preview = app.preview_apply("codex")
    assert preview["ok"] is True
    assert any(item["name"] == "auth.json" for item in preview["files"])
    assert app.preview_restore("codex")["ok"] is False
    backup = app.backup_apply("codex")
    assert backup["ok"] is True
    assert backup["created"] is True
    assert Path(backup["backup_path"]).is_dir()
    assert json.loads((Path(backup["backup_path"]) / "auth.json").read_text(encoding="utf-8")) == {
        "tokens": {"access_token": "chatgpt-official"}
    }
    assert (home / ".codex" / "auth.json").read_text(encoding="utf-8") == original
    assert "OPENAI_API_KEY" not in original
    restored_preview = app.preview_restore("codex")
    assert restored_preview["ok"] is True
    assert restored_preview["versions"][0]["id"] != "official"
    written = app.apply_cloud("codex")
    assert written["ok"] is True
    live = json.loads((home / ".codex" / "auth.json").read_text(encoding="utf-8"))
    assert live["OPENAI_API_KEY"] == "sk-ts-secret-key"
    restore_preview = app.preview_restore("codex")
    assert restore_preview["ok"] is True
    assert live["OPENAI_API_KEY"] == "sk-ts-secret-key"


def test_export_backup_without_window_returns_hint(tmp_path):
    app, _home = _wired_app(tmp_path)
    backup = app.backup_apply("codex")
    result = app.export_backup(backup["backup_path"])
    assert result["ok"] is False
    assert "手动" in (result.get("hint") or "")
    assert backup["backup_path"] in (result.get("path") or result.get("hint") or "")


def test_js_bridge_account_methods(tmp_path):
    app, _home = _wired_app(tmp_path)
    bridge = JsBridge(app)
    for name in (
        "dashboard",
        "calls",
        "wallet",
        "wallet_ledger",
        "redeem",
        "preview_apply",
        "backup_apply",
        "preview_restore",
        "open_backup_folder",
        "export_backup",
        "complete_guide",
    ):
        assert callable(getattr(bridge, name))
    assert not hasattr(bridge, "window")


class AccountClient(FakeClient):
    def dashboard(self):
        return {
            "user": {"usd_credit": "3.00"},
            "metrics": {
                "calls": 4,
                "successes": 3,
                "success_rate": 75,
                "failure_rate": 25,
                "input_tokens": 9,
                "output_tokens": 3,
                "cached_tokens": 1,
            },
            "day": {"calls": 1, "success_rate": 100, "failure_rate": 0, "input_tokens": 2, "output_tokens": 1, "cached_tokens": 0},
            "week": {"calls": 3, "success_rate": 66.7, "failure_rate": 33.3, "input_tokens": 6, "output_tokens": 2, "cached_tokens": 1},
            "month": {"calls": 4, "success_rate": 75, "failure_rate": 25, "input_tokens": 9, "output_tokens": 3, "cached_tokens": 1},
        }

    def calls(self, page=1, page_size=20):
        return {
            "data": [
                {
                    "started_at": "2026-09-20T00:00:00Z",
                    "model": "gpt-6-astra",
                    "status": "success",
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "cached_tokens": 0,
                    "total_tokens": 3,
                }
            ],
            "page": page,
            "page_size": page_size,
            "total": 1,
        }

    def wallet(self):
        return {"usd_credit": "3.00"}

    def wallet_ledger(self, page=1, page_size=20):
        return {
            "data": [{"created_at": "2026-09-20", "type": "grant", "amount": "3.00", "balance": "3.00"}],
            "page": page,
            "page_size": page_size,
            "total": 1,
        }

    def redeem(self, code):
        assert code == "CARD-1"
        return {"usd_credit": "13.00"}


def test_dashboard_calls_wallet_parse_mock_json(tmp_path):
    app, _home = _wired_app(tmp_path, AccountClient())
    dash = app.dashboard()
    assert dash["ok"] is True
    assert dash["user"]["usd_credit"] == "3.00"
    assert dash["metrics"]["calls"] == 4
    assert dash["day"]["input_tokens"] == 2
    assert dash["week"]["cached_tokens"] == 1
    calls = app.calls(1, 20)
    assert calls["ok"] is True
    assert calls["data"][0]["model"] == "gpt-6-astra"
    wallet = app.wallet()
    assert wallet["usd_credit"] == "3.00"
    ledger = app.wallet_ledger(1, 20)
    assert ledger["data"][0]["amount"] == "3.00"
    redeemed = app.redeem("CARD-1")
    assert redeemed["usd_credit"] == "13.00"


class MissingAccountClient(FakeClient):
    def dashboard(self):
        raise ApiError("服务端尚未更新该接口，请稍后再试。", status=404, code="portal_api_missing")

    def calls(self, page=1, page_size=20):
        raise ApiError("服务端尚未更新该接口，请稍后再试。", status=404, code="portal_api_missing")

    def wallet(self):
        raise ApiError("服务端尚未更新该接口，请稍后再试。", status=404, code="portal_api_missing")

    def wallet_ledger(self, page=1, page_size=20):
        raise ApiError("服务端尚未更新该接口，请稍后再试。", status=404, code="portal_api_missing")

    def redeem(self, code):
        raise ApiError("服务端尚未更新该接口，请稍后再试。", status=404, code="portal_api_missing")


def test_missing_account_apis_do_not_crash(tmp_path):
    app, _home = _wired_app(tmp_path, MissingAccountClient())
    for result in (app.dashboard(), app.calls(), app.wallet(), app.wallet_ledger(), app.redeem("CARD-1")):
        assert result["ok"] is False
        assert result["status"] == 404
        assert "尚未更新" in result["error"]


def test_preview_includes_model_choices(tmp_path):
    app, _home = _wired_app(tmp_path)
    preview = app.preview_apply("codex")
    assert preview["ok"] is True
    assert preview["uses_models"] is True
    assert preview["models"][0]["slug"] == "gpt-6-astra"
    assert preview["models"][0]["display_name"] == "GPT-6 Astra"
    claude = app.preview_apply("claude_code")
    assert claude["uses_models"] is False


def test_apply_cloud_filters_selected_models(tmp_path):
    class Multi(FakeClient):
        def bootstrap(self):
            payload = super().bootstrap()
            payload["models"] = ["gpt-6-astra", "grok-4.6"]
            payload["catalog"]["models"].append(
                {
                    "slug": "grok-4.6",
                    "display_name": "Grok 4.6",
                    "context_window": 272000,
                }
            )
            return payload

    app, home = _wired_app(tmp_path, Multi())
    empty = app.apply_cloud("codex", [])
    assert empty["ok"] is False
    assert "模型" in empty["error"]
    result = app.apply_cloud("codex", ["grok-4.6"])
    assert result["ok"] is True
    written = json.loads((home / ".codex" / "codex-models.json").read_text(encoding="utf-8"))
    assert [item["slug"] for item in written["models"]] == ["grok-4.6"]
    assert written["models"][0]["display_name"] == "Grok 4.6"


def test_apply_uses_server_skill_zip(tmp_path):
    import io
    import zipfile

    from minking_desktop.app import _extract_skill_zip
    from minking_desktop.api import ApiError as DesktopApiError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "# from-server\n")
        archive.writestr("scripts/minking-media.ps1", "echo server")
    blob = buffer.getvalue()

    class SkillClient(FakeClient):
        def list_skills(self):
            return {"skills": [{"name": "minking-media", "sha256": "abc123", "files": ["SKILL.md"]}]}

        def download_skill(self, name):
            assert name == "minking-media"
            return blob

    app, home = _wired_app(tmp_path, SkillClient())
    result = app.apply_cloud("codex")
    assert result["ok"] is True
    skill = home / ".codex" / "skills" / "minking-media" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == "# from-server\n"
    cached = tmp_path / "MinKing" / "skills" / "minking-media"
    assert (cached / ".sha256").read_text(encoding="utf-8").strip() == "abc123"

    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../evil.md", "nope")
    try:
        _extract_skill_zip(evil.getvalue(), tmp_path / "dest")
        raise AssertionError("expected invalid zip")
    except DesktopApiError as exc:
        assert exc.code == "invalid_skill_zip"


def test_complete_guide_persists(tmp_path):
    app, _home = _wired_app(tmp_path)
    assert app.state()["settings"]["guide_done"] is False
    done = app.complete_guide()
    assert done["ok"] is True
    assert app.state()["settings"]["guide_done"] is True
    bridge = JsBridge(app)
    assert callable(bridge.complete_guide)
    assert callable(bridge.apply_cloud)


def test_local_codex_probe_uses_the_current_client_version(monkeypatch):
    import asyncio

    from app.config import settings
    import minking_desktop.official as official

    monkeypatch.setattr(official, "_refreshed_codex_version", True)
    monkeypatch.setattr(settings, "codex_client_version", "0.156.0")
    assert asyncio.run(official.codex_models_probe_path("chatgpt")) == "/models?client_version=0.156.0"
    assert asyncio.run(official.codex_models_probe_path("apikey")) == "/models"
