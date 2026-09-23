"""Tray companion window. Not a chat client."""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from minking_desktop import __version__
from minking_desktop.api import ApiError, PortalClient
from minking_desktop.clipboard import copy_text
from minking_desktop.harness import (
    ApplyError,
    apply_harness,
    backup_before_sync,
    detect_harnesses,
    local_codex_catalog_entry,
    packed_codex_catalog,
    planned_sync_files,
    codex_restore_compare,
    list_restore_versions,
    restore_all,
    restore_available,
    restore_harness,
    sync_codex_sessions,
)
from minking_desktop.paths import DEFAULT_PUBLIC_V1, appdata_root, profile_root, public_v1_url, user_home
from minking_desktop.protocol import InstanceLock, launch_command, parse_deep_link, register_protocol
from minking_desktop.recipes import recipe_by_id
from minking_desktop.secrets import SecretError, SecretStore
from minking_desktop.windowing import (
    WEBVIEW2_DOWNLOAD,
    find_window_hwnd,
    handoff_existing_instance,
    load_pythonnet,
    native_hwnd,
    prepare_windows_webview,
    restore_window,
    wake_webview,
    show_error_dialog,
    webview2_installed,
)

logger = logging.getLogger("minking_desktop")


def _ui_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "minking_desktop" / "ui"
    return Path(__file__).resolve().parent / "ui"


UI_DIR = _ui_dir()
WINDOW_TITLE = "MinKing AI"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760
WINDOW_MIN_SIZE = (960, 640)
WINDOW_BACKGROUND = "#F7F8F5"
_MODEL_PICKER_HARNESSES = {"codex", "grok", "workbuddy", "zcode"}
_MAX_SKILL_BYTES = 5 * 1024 * 1024


def _as_slug_list(value: Any) -> list[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                return [text]
            value = loaded
        else:
            return [text]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return None


def _extract_skill_zip(blob: bytes, dest: Path) -> None:
    tmp = dest.parent / (dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    root = tmp.resolve()
    total = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        for info in archive.infolist():
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ApiError("技能包无效", code="invalid_skill_zip")
            target = (tmp / rel).resolve()
            if root not in target.parents and target != root:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ApiError("技能包无效", code="invalid_skill_zip")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if total > _MAX_SKILL_BYTES:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ApiError("技能包过大", code="invalid_skill_zip")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as out:
                out.write(source.read())
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(tmp, dest)
    shutil.rmtree(tmp, ignore_errors=True)


def _model_choices_from(bootstrap: dict[str, Any]) -> list[dict[str, str]]:
    slugs = [item for item in bootstrap.get("models") or [] if isinstance(item, str) and item]
    catalog = bootstrap.get("catalog") if isinstance(bootstrap.get("catalog"), dict) else None
    packed = packed_codex_catalog(catalog, slugs)
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in packed.get("models") or []:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        slug = str(item["slug"])
        if slug in seen:
            continue
        seen.add(slug)
        choice = {"slug": slug, "display_name": str(item.get("display_name") or slug)}
        if isinstance(item.get("pricing"), dict):
            choice["pricing"] = item["pricing"]
        choices.append(choice)
    for slug in slugs:
        if slug not in seen:
            choices.append({"slug": slug, "display_name": slug})
    return choices


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _client_bundle_name() -> str:
    return "MinKingAI.app" if _is_macos() else "MinKingAI.exe"


def _desktop_log_hint() -> str:
    if _is_macos():
        return "~/Library/Application Support/MinKing/desktop.log"
    return r"%APPDATA%\MinKing\desktop.log"


def missing_webview_message(exc: BaseException | None = None) -> str:
    detail = f"{type(exc).__name__}: {exc}" if exc else "ImportError: webview"
    bundle = _client_bundle_name()
    if getattr(sys, "frozen", False):
        return (
            "客户端组件缺失，窗口无法打开。\n"
            f"{detail}\n\n"
            f"请重新下载并直接运行 {bundle}。\n"
            f"详情已写入 {_desktop_log_hint()}"
        )
    return (
        "缺少 pywebview，无法打开窗口。\n"
        f"请直接运行打包好的 {bundle}；开发调试才需要安装 "
        "clients/minking-desktop/requirements.txt。\n"
        f"{detail}"
    )


def startup_failure_message() -> str:
    return f"客户端启动失败。请查看 {_desktop_log_hint()}，或重新下载 {_client_bundle_name()}。"


def webview_start_failure_message(exc: BaseException) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if _is_macos():
        return (
            "无法打开窗口。\n"
            f"{detail}\n\n"
            "请重新打开 MinKingAI.app。若从浏览器下载，请在访达里右键打开。"
        )
    return (
        "无法打开窗口。\n"
        f"{detail}\n\n"
        "请确认已安装 Microsoft Edge WebView2 Runtime 后重试。\n"
        f"{WEBVIEW2_DOWNLOAD}"
    )


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = record.getMessage()
        if "sk-ts-" in text or "Bearer " in text or "OPENAI_API_KEY" in text or "ANTHROPIC_API_KEY" in text:
            record.msg = "log_line_redacted"
            record.args = ()
        return True


class JsBridge:
    """pywebview js_api surface. Do not expose window/native COM objects here."""

    def __init__(self, app: DesktopApp) -> None:
        self._app = app

    def state(self) -> dict[str, Any]:
        return self._app.state()

    def open_local_gateway(self) -> dict[str, Any]:
        return self.set_workspace("local")

    def workspace_state(self) -> dict[str, Any]:
        return {"workspace": self._app.store.load_settings().get("workspace", "cloud")}

    def set_workspace(self, workspace: str) -> dict[str, Any]:
        if workspace not in {"cloud", "local"}:
            return {"ok": False, "error": "未知来源"}
        with self._app._lock:
            settings = self._app.store.load_settings()
            settings["workspace"] = workspace
            self._app.store.save_settings(settings)
        return {"ok": True}

    def local_state(self) -> dict[str, Any]:
        return self._app.local_bridge().state()

    def local_request(self, path: str, data=None, method="GET") -> dict[str, Any]:
        return self._app.local_bridge().request(path, data, method)

    def local_control(self, action: str, port=None) -> dict[str, Any]:
        return self._app.local_bridge().control(action, port)

    def local_copy(self, value: str) -> dict[str, Any]:
        return self._app.local_bridge().copy(value)

    def local_tools(self, action="list", harness_id="", selected_models=None, version="") -> dict[str, Any]:
        state = self.local_state()
        base = state["base_url"]
        root = self._app.appdata / "local-profiles"
        home = self._app.home
        try:
            if action == "list":
                items = detect_harnesses(home=home, public_base=base)
                for item in items:
                    item["has_snapshot"] = restore_available(item["id"], profile_root=root)
                return {"ok": True, "items": items}
            recipe = recipe_by_id(harness_id, public_base=base)
            if action == "preview_restore":
                versions = list_restore_versions(harness_id, profile_root=root)
                if not versions:
                    return {"ok": False, "error": "还没有可回退的配置版本"}
                payload = {"ok": True, "display_name": recipe["display_name"],
                        "files": planned_sync_files(recipe, home=home), "base_url": base,
                        "snapshot_dir": versions[0]["path"], "versions": versions}
                if harness_id == "codex":
                    payload["auth_compare"] = codex_restore_compare(home=home, profile_root=root)
                return payload
            if action == "preview":
                snap = root / harness_id / "official"
                return {"ok": True, "display_name": recipe["display_name"],
                        "files": planned_sync_files(recipe, home=home), "base_url": base,
                        "snapshot_dir": str(snap), "models": [m for a in state.get("accounts", [])
                        if a["id"] in state["config"]["enabled_providers"] for m in a["models"] if m["type"] == "text"]}
            if action == "backup":
                dest = backup_before_sync(recipe, home=home, profile_root=root)
                return {"ok": True, "created": dest is not None, "backup_path": str(dest) if dest else "",
                        "backup_dir": str(root / harness_id / "backups")}
            if action == "restore":
                return restore_harness(
                    recipe, home=home, profile_root=root, version=str(version or "") or None
                )
            if action == "sync_sessions":
                if harness_id != "codex":
                    return {"ok": False, "error": "只有 Codex 支持一键同步会话"}
                return sync_codex_sessions(home=home, profile_root=root)
            if action != "apply" or not state["service_running"]:
                return {"ok": False, "error": "请先启动本地 HTTP 服务"}
            available = {m["id"] for a in state.get("accounts", []) for m in a["models"]
                         if a["id"] in state["config"]["enabled_providers"] and m["type"] == "text"}
            models = [m for m in _as_slug_list(selected_models) or [] if m in available]
            if not models:
                return {"ok": False, "error": "请先同步并选择至少一个文本模型"}
            backup_before_sync(recipe, home=home, profile_root=root)
            catalog = None
            if harness_id == "codex":
                upstream_by_id = {}
                for account in state.get("accounts") or []:
                    for item in account.get("models") or []:
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            upstream = item.get("upstream")
                            upstream_by_id[item["id"]] = upstream if isinstance(upstream, dict) else None
                catalog = {
                    "models": [
                        local_codex_catalog_entry(slug, upstream_by_id.get(slug), priority=index + 1)
                        for index, slug in enumerate(models)
                    ]
                }
            return apply_harness(harness_id, home=home, profile_root=root, api_key=state["api_key"],
                                 public_base=base, models=models, catalog=catalog)
        except ApplyError as exc:
            return {"ok": False, "error": str(exc)}
        except (KeyError, OSError, ValueError):
            return {"ok": False, "error": "接入操作失败，请检查工具配置与备份"}

    def cloud_models_state(self) -> dict[str, Any]:
        if not self._app.client.token:
            raise ApiError("请先登录云端账号")
        catalog = self._app.client.fetch_model_catalog(self._app._api_key())
        return {"accounts": [{"id": "cloud", "name": "MinKing 云端", "models": catalog}],
            "config": {"enabled_providers": ["cloud"], "default_model": ""},
            "base_url": self._app.public_base, "service_running": True, "version": __version__}

    def cloud_model_request(self, path: str, data=None, method="POST") -> dict[str, Any]:
        import re
        from minking_desktop.api import default_request
        allowed = {"/v1/responses", "/v1/chat/completions", "/v1/messages", "/v1/images/generations", "/v1/videos"}
        video_get = (method == "GET" and isinstance(path, str) and path.startswith("/v1/videos/")
                     and re.fullmatch(r"[A-Za-z0-9_-]+", path[len("/v1/videos/"):]) is not None)
        if not ((method == "POST" and path in allowed) or video_get):
            return {"ok": False, "error": "接口路径无效"}
        if not self._app.client.token:
            return {"ok": False, "error": "请先登录云端账号"}
        try:
            if path == "/v1/responses" and isinstance(data, dict):
                from app.providers.codex_protocol import responses_lite_enabled, to_responses_payload
                model = str(data.get("model") or "")
                if responses_lite_enabled(model):
                    data, _ = to_responses_payload(data, default_model=model)
                else:
                    data = dict(data)
                    if isinstance(data.get("input"), str):
                        data["input"] = [{"role": "user", "content": [
                            {"type": "input_text", "text": data["input"]}]}]
                data["stream"] = True
            key = self._app._api_key()
            status, result = default_request(method, self._app.public_base.rstrip("/") + path[3:], data,
                {"Authorization": "Bearer " + key}, 200)
            if status < 400 and path == "/v1/responses":
                from minking_desktop.cloud_response import collect_response
                status, result = collect_response(result)
            if status >= 400:
                error = result.get("error") if isinstance(result, dict) else None
                detail = error.get("message") if isinstance(error, dict) else None
                detail = detail if isinstance(detail, str) else "请查看调用明细"
                # Error details are shown in memory only; strip known credentials before returning.
                for secret in (key, self._app.client.token):
                    if secret:
                        detail = detail.replace(secret, "[已隐藏]")
                detail = re.sub(r"(?i)Bearer\s+\S+|\bsk-[A-Za-z0-9_-]+", "[已隐藏]", detail)
                return {"ok": False, "status": status,
                        "error": f"云端调用失败（HTTP {status}）：{detail[:800]}"}
            if not isinstance(result, dict):
                return {"ok": False, "error": "云端返回了非 JSON 响应"}
            return {"ok": True, "data": result}
        except (ApiError, OSError, ValueError):
            return {"ok": False, "error": "云端连接失败或请求超时"}

    def copy_model_text(self, value: str) -> dict[str, Any]:
        if not isinstance(value, str) or len(value) > 1_000_000:
            return {"ok": False, "error": "复制内容无效"}
        try:
            copy_text(value)
            return {"ok": True}
        except OSError:
            return {"ok": False, "error": "复制失败"}

    def get_captcha(self) -> dict[str, Any]:
        return self._app.get_captcha()

    def send_code(self, name: str, email: str, captcha_id: str, captcha: str) -> dict[str, Any]:
        return self._app.send_code(name, email, captcha_id, captcha)

    def verify(self, challenge_id: str, email: str, code: str) -> dict[str, Any]:
        return self._app.verify(challenge_id, email, code)

    def save_base_url(self, base_url: str) -> dict[str, Any]:
        return self._app.save_base_url(base_url)

    def apply_cloud(self, harness_id: str, selected_models: Any = None) -> dict[str, Any]:
        return self._app.apply_cloud(harness_id, selected_models)

    def sync_codex_sessions(self) -> dict[str, Any]:
        return self._app.sync_codex_sessions()

    def complete_guide(self) -> dict[str, Any]:
        return self._app.complete_guide()

    def restore_official(self, harness_id: str, version: str = "") -> dict[str, Any]:
        return self._app.restore_official(harness_id, version)

    def restore_all_official(self) -> dict[str, Any]:
        return self._app.restore_all_official()

    def preview_apply(self, harness_id: str) -> dict[str, Any]:
        return self._app.preview_apply(harness_id)

    def backup_apply(self, harness_id: str) -> dict[str, Any]:
        return self._app.backup_apply(harness_id)

    def preview_restore(self, harness_id: str) -> dict[str, Any]:
        return self._app.preview_restore(harness_id)

    def open_backup_folder(self, path: str = "") -> dict[str, Any]:
        return self._app.open_backup_folder(path)

    def export_backup(self, path: str = "") -> dict[str, Any]:
        return self._app.export_backup(path)

    def dashboard(self) -> dict[str, Any]:
        return self._app.dashboard()

    def calls(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._app.calls(page, page_size)

    def wallet(self) -> dict[str, Any]:
        return self._app.wallet()

    def wallet_ledger(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._app.wallet_ledger(page, page_size)

    def redeem(self, code: str) -> dict[str, Any]:
        return self._app.redeem(code)

    def reveal_key(self) -> dict[str, Any]:
        return self._app.reveal_key()

    def copy_value(self, kind: str, harness_id: str = "") -> dict[str, Any]:
        return self._app.copy_value(kind, harness_id)

    def logout(self) -> dict[str, Any]:
        return self._app.logout()


def configure_logging(appdata: Path | None = None) -> None:
    root = appdata_root(appdata=appdata)
    root.mkdir(parents=True, exist_ok=True)
    log_path = root / "desktop.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactFilter())
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False


class DesktopApp:
    def __init__(self, *, home: Path | None = None, appdata: Path | None = None) -> None:
        self.home = user_home(home)
        self.store = SecretStore(appdata=appdata)
        self.appdata = self.store.appdata
        self._lock = threading.RLock()
        self._local_lock = threading.RLock()
        self._local_bridge = None
        self._cached_key: str | None = None
        self._quitting = False
        self._hid_once = False
        self._instance_lock: InstanceLock | None = None
        self.window = None
        self.tray = None
        settings = self.store.load_settings()
        self.public_base = public_v1_url(str(settings.get("public_base_url") or DEFAULT_PUBLIC_V1))
        self.client = PortalClient(self.public_base, token=self.store.load_token())
        self._deep_link: dict[str, str] | None = None
        self._bootstrap_cache: tuple[float, dict[str, Any]] | None = None

    def local_bridge(self):
        with self._local_lock:
            if self._local_bridge is None:
                from minking_desktop.local_desktop import DesktopBridge, LocalService
                service = LocalService(home=self.home, appdata=self.appdata)
                self._local_bridge = DesktopBridge(service)
                try:
                    service.start()
                except (RuntimeError, OSError):
                    pass  # The embedded service panel allows changing an occupied port.
            return self._local_bridge

    def stop_local(self) -> None:
        with self._local_lock:
            if self._local_bridge is not None:
                self._local_bridge.control("stop")

    def _settings_public(self) -> dict[str, Any]:
        data = self.store.load_settings()
        return {
            "public_base_url": self.public_base,
            "email": data.get("email") or "",
            "name": data.get("name") or "",
            "version": __version__,
            "guide_done": bool(data.get("guide_done")),
        }

    def state(self) -> dict[str, Any]:
        with self._lock:
            logged_in = bool(self.client.token)
            payload: dict[str, Any] = {
                "ok": True,
                "logged_in": logged_in,
                "settings": self._settings_public(),
            }
            if not logged_in:
                return payload
            try:
                bootstrap = self._bootstrap()
            except ApiError as exc:
                if exc.status in {401, 403}:
                    self._forget_session()
                    return {"ok": True, "logged_in": False, "settings": self._settings_public(), "error": "登录已过期，请重新验证邮箱"}
                return {**payload, "error": exc.message}
            user = bootstrap.get("user") if isinstance(bootstrap.get("user"), dict) else {}
            key = bootstrap.get("key") if isinstance(bootstrap.get("key"), dict) else {}
            models = [item for item in bootstrap.get("models") or [] if isinstance(item, str)]
            public_base = public_v1_url(str(bootstrap.get("public_base_url") or self.public_base))
            harnesses = detect_harnesses(home=self.home, public_base=public_base)
            for item in harnesses:
                item["has_snapshot"] = restore_available(item["id"], profile_root=profile_root(appdata=self.appdata))
            self.public_base = public_base
            payload.update(
                {
                    "user": {
                        "name": user.get("name") or "",
                        "email": user.get("email") or "",
                        "usd_credit": str(user.get("usd_credit") or "0"),
                    },
                    "key": {"prefix": key.get("prefix") or "", "status": key.get("status") or ""},
                    "public_base_url": public_base,
                    "models": models,
                    "model_choices": _model_choices_from(bootstrap),
                    "harnesses": harnesses,
                    "deep_link": self._deep_link,
                }
            )
            self._deep_link = None
            return payload

    def get_captcha(self) -> dict[str, Any]:
        try:
            return {"ok": True, **self.client.captcha()}
        except ApiError as exc:
            return exc.as_dict()

    def send_code(self, name: str, email: str, captcha_id: str, captcha: str) -> dict[str, Any]:
        try:
            result = self.client.send_code(name=name.strip(), email=email.strip(), captcha_id=captcha_id, captcha=captcha.strip())
            return {"ok": True, **result}
        except ApiError as exc:
            return exc.as_dict()

    def verify(self, challenge_id: str, email: str, code: str) -> dict[str, Any]:
        try:
            from minking_desktop.device_id import DeviceIdError, device_fingerprint

            try:
                fingerprint = device_fingerprint()
            except DeviceIdError as exc:
                return {"ok": False, "error": str(exc), "code": "device_id_required"}
            result = self.client.verify(
                challenge_id=challenge_id,
                email=email.strip(),
                code=code.strip(),
                device_id=fingerprint,
            )
            self.store.save_token(result["token"])
        except ApiError as exc:
            return exc.as_dict()
        except SecretError:
            return {"ok": False, "error": "无法保存登录凭据"}
        settings = self.store.load_settings()
        settings["email"] = result["email"]
        settings["name"] = result["name"]
        settings["public_base_url"] = self.public_base
        self.store.save_settings(settings)
        self._cached_key = None
        self._bootstrap_cache = None
        return {
            "ok": True,
            "name": result["name"],
            "email": result["email"],
            "reward_notice": result.get("reward_notice") or "",
        }

    def save_base_url(self, base_url: str) -> dict[str, Any]:
        self.public_base = public_v1_url(base_url)
        settings = self.store.load_settings()
        settings["public_base_url"] = self.public_base
        self.store.save_settings(settings)
        token = self.client.token
        self.client = PortalClient(self.public_base, token=token)
        self._bootstrap_cache = None
        return {"ok": True, "public_base_url": self.public_base}

    def _bootstrap(self) -> dict[str, Any]:
        now = time.monotonic()
        cached = self._bootstrap_cache
        if cached and now - cached[0] < 30:
            return cached[1]
        payload = self.client.bootstrap()
        self._bootstrap_cache = (now, payload)
        return payload

    def apply_cloud(self, harness_id: str, selected_models: Any = None) -> dict[str, Any]:
        try:
            key = self._api_key()
            bootstrap = self._bootstrap()
            models = [item for item in bootstrap.get("models") or [] if isinstance(item, str)]
            catalog = bootstrap.get("catalog") if isinstance(bootstrap.get("catalog"), dict) else None
            packed = packed_codex_catalog(catalog, models)
            if not any(isinstance(item, dict) and item.get("display_name") for item in packed.get("models") or []):
                fetched = self.client.fetch_codex_catalog(key)
                if fetched:
                    catalog = fetched
                    models = [
                        str(item["slug"])
                        for item in fetched.get("models") or []
                        if isinstance(item, dict) and item.get("slug")
                    ]
            wanted = _as_slug_list(selected_models)
            if wanted is not None:
                available = set(models)
                models = [item for item in wanted if item in available]
                if not models:
                    return {"ok": False, "error": "请至少选择一个模型"}
                if isinstance(catalog, dict) and isinstance(catalog.get("models"), list):
                    selected = set(models)
                    catalog = {
                        "models": [
                            item
                            for item in catalog["models"]
                            if isinstance(item, dict) and str(item.get("slug")) in selected
                        ]
                    }
            public_base = public_v1_url(str(bootstrap.get("public_base_url") or self.public_base))
            skill_source = self._refresh_skills()
            result = apply_harness(
                harness_id,
                home=self.home,
                profile_root=profile_root(appdata=self.appdata),
                api_key=key,
                public_base=public_base,
                models=models,
                catalog=catalog,
                skill_source=skill_source,
            )
            if not result.get("ok"):
                return result
            logger.info("applied_cloud harness=%s", harness_id)
            payload = {"ok": True, "id": harness_id, "mode": "cloud"}
            message = result.get("message")
            if isinstance(message, str) and message:
                payload["message"] = message
            return payload
        except ApiError as exc:
            return exc.as_dict()
        except Exception as exc:
            logger.exception("apply_failed harness=%s", harness_id)
            return {"ok": False, "error": str(exc) or "写入失败"}

    def sync_codex_sessions(self) -> dict[str, Any]:
        try:
            return sync_codex_sessions(
                home=self.home,
                profile_root=profile_root(appdata=self.appdata),
            )
        except ApplyError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            logger.exception("codex_session_sync_failed")
            return {"ok": False, "error": "同步会话失败"}

    def complete_guide(self) -> dict[str, Any]:
        settings = self.store.load_settings()
        settings["guide_done"] = True
        self.store.save_settings(settings)
        return {"ok": True, "guide_done": True}

    def _refresh_skills(self) -> Path | None:
        list_fn = getattr(self.client, "list_skills", None)
        download_fn = getattr(self.client, "download_skill", None)
        if not callable(list_fn) or not callable(download_fn):
            return None
        try:
            payload = list_fn()
            skills = payload.get("skills") if isinstance(payload, dict) else None
            if not isinstance(skills, list):
                return None
            root = self.appdata / "skills"
            root.mkdir(parents=True, exist_ok=True)
            for item in skills:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                sha = str(item.get("sha256") or "")
                if not name or not sha:
                    continue
                dest = root / name
                marker = dest / ".sha256"
                if dest.is_dir() and marker.is_file() and marker.read_text(encoding="utf-8").strip() == sha:
                    continue
                blob = download_fn(name)
                _extract_skill_zip(blob, dest)
                (dest / ".sha256").write_text(sha + "\n", encoding="utf-8")
            if any(child.is_dir() and (child / "SKILL.md").is_file() for child in root.iterdir()):
                return root
            return None
        except ApiError:
            logger.info("skill_refresh_skipped")
            return None
        except Exception:
            logger.exception("skill_refresh_failed")
            return None

    def restore_official(self, harness_id: str, version: str = "") -> dict[str, Any]:
        try:
            recipe = recipe_by_id(harness_id, public_base=self.public_base)
            result = restore_harness(
                recipe,
                home=self.home,
                profile_root=profile_root(appdata=self.appdata),
                version=version or None,
            )
            logger.info("restored_official harness=%s ok=%s", harness_id, result.get("ok"))
            return result
        except Exception as exc:
            logger.exception("restore_failed harness=%s", harness_id)
            return {"ok": False, "error": str(exc) or "恢复失败"}

    def restore_all_official(self) -> dict[str, Any]:
        results = restore_all(home=self.home, profile_root=profile_root(appdata=self.appdata), public_base=self.public_base)
        return {"ok": True, "results": results}

    def preview_apply(self, harness_id: str) -> dict[str, Any]:
        recipe = self._recipe(harness_id)
        if recipe.get("error"):
            return recipe
        if not recipe.get("one_click"):
            return {"ok": False, "error": "该工具不支持一键写入，请复制接口地址和密钥。"}
        models: list[dict[str, str]] = []
        try:
            bootstrap = self._bootstrap()
            models = _model_choices_from(bootstrap)
        except ApiError:
            models = []
        return {
            "ok": True,
            "id": harness_id,
            "display_name": recipe["display_name"],
            "action": "apply",
            "files": planned_sync_files(recipe, home=self.home),
            "models": models,
            "uses_models": harness_id in _MODEL_PICKER_HARNESSES,
            "base_url": self.public_base,
        }

    def backup_apply(self, harness_id: str) -> dict[str, Any]:
        recipe = self._recipe(harness_id)
        if recipe.get("error"):
            return recipe
        if not recipe.get("one_click"):
            return {"ok": False, "error": "该工具不支持一键写入，请复制接口地址和密钥。"}
        dest = backup_before_sync(recipe, home=self.home, profile_root=profile_root(appdata=self.appdata))
        backup_root = profile_root(appdata=self.appdata) / harness_id / "backups"
        logger.info("backup_before_apply harness=%s created=%s", harness_id, bool(dest))
        return {
            "ok": True,
            "id": harness_id,
            "backup_path": str(dest) if dest else "",
            "backup_dir": str(backup_root),
            "created": dest is not None,
            "files": planned_sync_files(recipe, home=self.home),
            "message": None if dest else "当前没有可备份的配置文件，写入时会创建新文件。",
        }

    def preview_restore(self, harness_id: str) -> dict[str, Any]:
        recipe = self._recipe(harness_id)
        if recipe.get("error"):
            return recipe
        if not recipe.get("one_click"):
            return {"ok": False, "error": "该工具不支持一键恢复。"}
        versions = list_restore_versions(harness_id, profile_root=profile_root(appdata=self.appdata))
        if not versions:
            return {"ok": False, "error": "还没有可回退的配置版本。首次接入时会自动保存当前配置。"}
        payload = {
            "ok": True,
            "id": harness_id,
            "display_name": recipe["display_name"],
            "action": "restore",
            "files": planned_sync_files(recipe, home=self.home),
            "snapshot_dir": versions[0]["path"],
            "versions": versions,
        }
        if harness_id == "codex":
            payload["auth_compare"] = codex_restore_compare(
                home=self.home, profile_root=profile_root(appdata=self.appdata)
            )
        return payload

    def open_backup_folder(self, path: str = "") -> dict[str, Any]:
        raw = (path or "").strip()
        target = Path(raw) if raw else profile_root(appdata=self.appdata)
        try:
            if target.is_file():
                target = target.parent
            target.mkdir(parents=True, exist_ok=True)
            _open_path(target)
            return {"ok": True, "path": str(target)}
        except OSError:
            return {"ok": False, "error": "无法打开备份目录", "path": str(target), "hint": f"请手动打开：{target}"}

    def export_backup(self, path: str = "") -> dict[str, Any]:
        src = Path((path or "").strip())
        if not src.exists():
            return {"ok": False, "error": "备份目录不存在", "path": str(src), "hint": "请先生成备份，或手动复制路径中的文件夹"}
        if src.is_file():
            src = src.parent
        picked = self._pick_folder(str(src.parent))
        if not picked:
            return {
                "ok": False,
                "cancelled": True,
                "path": str(src),
                "hint": f"请手动复制备份目录到安全位置：{src}",
            }
        dest = Path(picked) / src.name
        try:
            if dest.exists():
                dest = Path(picked) / f"{src.name}-copy"
            shutil.copytree(src, dest, dirs_exist_ok=True)
            return {"ok": True, "path": str(dest)}
        except OSError:
            return {"ok": False, "error": "导出备份失败", "path": str(src), "hint": f"请手动复制备份目录：{src}"}

    def dashboard(self) -> dict[str, Any]:
        return self._portal("dashboard")

    def calls(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._portal("calls", page=page, page_size=page_size)

    def wallet(self) -> dict[str, Any]:
        return self._portal("wallet")

    def wallet_ledger(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._portal("wallet_ledger", page=page, page_size=page_size)

    def redeem(self, code: str) -> dict[str, Any]:
        return self._portal("redeem", code)

    def _recipe(self, harness_id: str) -> dict[str, Any]:
        try:
            return recipe_by_id(harness_id, public_base=self.public_base)
        except KeyError:
            return {"ok": False, "error": "未知工具"}

    def _portal(self, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            payload = getattr(self.client, method)(*args, **kwargs)
        except ApiError as exc:
            return exc.as_dict()
        except Exception:
            logger.exception("portal_%s_failed", method)
            return {"ok": False, "error": "请求失败"}
        if isinstance(payload, dict):
            return {"ok": True, **payload}
        return {"ok": True, "data": payload}

    def _pick_folder(self, directory: str) -> str | None:
        window = self.window
        if window is None:
            return None
        try:
            import webview

            chosen = window.create_file_dialog(webview.FileDialog.FOLDER, directory=directory)
        except Exception:
            logger.info("folder_picker_failed")
            return None
        if not chosen:
            return None
        first = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
        text = str(first or "").strip()
        return text or None

    def reveal_key(self) -> dict[str, Any]:
        try:
            key = self._api_key()
            prefix = key[:12] if key.startswith("sk-ts-") else (key[:8] + "…")
            return {"ok": True, "key": key, "prefix": prefix}
        except ApiError as exc:
            return exc.as_dict()

    def copy_value(self, kind: str, harness_id: str = "") -> dict[str, Any]:
        try:
            if kind == "base_url":
                text = public_v1_url(self.public_base)
            elif kind == "key":
                text = self._api_key()
            elif kind == "root_url":
                text = public_v1_url(self.public_base)
                text = text[:-3] if text.endswith("/v1") else text
            else:
                return {"ok": False, "error": "未知复制类型"}
            copy_text(text)
            logger.info("copied kind=%s harness=%s", kind, harness_id or "-")
            return {"ok": True, "kind": kind}
        except ApiError as exc:
            return exc.as_dict()
        except OSError:
            return {"ok": False, "error": "无法写入剪贴板"}

    def logout(self) -> dict[str, Any]:
        try:
            self.client.logout()
        except ApiError:
            pass
        self._forget_session()
        return {"ok": True, "logged_in": False}

    def _forget_session(self) -> None:
        self._cached_key = None
        self._bootstrap_cache = None
        self.client.token = None
        self.store.clear_token()
        settings = self.store.load_settings()
        settings.pop("email", None)
        settings.pop("name", None)
        self.store.save_settings(settings)

    def _api_key(self) -> str:
        if self._cached_key:
            return self._cached_key
        revealed = self.client.reveal_key()
        self._cached_key = revealed["key"]
        return self._cached_key

    def handle_instance_message(self, message: dict) -> None:
        cmd = str(message.get("cmd") or "")
        if cmd == "quit":
            self.quit()
            return
        self.handle_deep_link(str(message.get("url") or "minking://open"))

    def handle_deep_link(self, url: str | None) -> None:
        parsed = parse_deep_link(url)
        if parsed is None:
            return
        self._deep_link = parsed
        base = parsed.get("base") or ""
        if base:
            self.save_base_url(base)
        self.show_window()

    def show_window(self) -> None:
        window = self.window
        hwnd = native_hwnd(window) or find_window_hwnd(WINDOW_TITLE)
        if hwnd:
            restore_window(hwnd, os.getpid())
            if self._instance_lock is not None:
                self._instance_lock.update_hwnd(hwnd)
        if window is None:
            return
        try:
            window.show()
            window.restore()
            wake_webview(window)
        except Exception:
            logger.exception("show_window_failed")

    def hide_window(self) -> bool:
        if self._quitting:
            return True
        if self.window is not None:
            try:
                self.window.hide()
            except Exception:
                return True
            return False
        return True

    def _hide_to_tray(self) -> None:
        if self._quitting:
            return
        self.hide_window()
        tray = self.tray
        if tray is None or self._hid_once:
            return
        self._hid_once = True
        notify = getattr(tray, "notify", None)
        if not callable(notify):
            return
        try:
            notify(f"窗口已隐藏到托盘。再次打开 {_client_bundle_name()} 或点击托盘图标即可回来。")
        except TypeError:
            try:
                notify("MinKing AI", "窗口已隐藏到托盘。再次打开客户端即可回来。")
            except Exception:
                pass
        except Exception:
            pass

    def close_should_quit(self) -> bool:
        return self._quitting or self.tray is None

    def quit(self) -> None:
        logger.info("quit")
        self._quitting = True
        self.stop_local()
        tray = self.tray
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                pass
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass


def _open_path(path: Path) -> None:
    target = str(path)
    if os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
        return
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.run([opener, target], check=False)


def _load_icon():
    from PIL import Image

    path = UI_DIR / "brand-mark.png"
    try:
        with Image.open(path) as image:
            return image.copy()
    except Exception:
        return Image.new("RGBA", (64, 64), (107, 140, 255, 255))


def _start_tray(app: DesktopApp) -> None:
    if os.environ.get("MINKING_NO_TRAY") == "1":
        logger.info("tray_disabled")
        return
    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError:
        logger.info("pystray_missing")
        return
    except Exception:
        logger.exception("pystray_import_failed")
        return

    def show(icon, item) -> None:  # noqa: ARG001
        app.show_window()

    def restore(_icon, _item) -> None:
        app.restore_all_official()
        app.show_window()
        try:
            if app.window is not None:
                app.window.evaluate_js("render()")
        except Exception:
            pass

    def quit_app(icon, _item) -> None:
        icon.stop()
        app.quit()

    try:
        app.tray = pystray.Icon(
            "MinKingAI",
            icon=_load_icon(),
            title="MinKing AI",
            menu=Menu(
                MenuItem("打开主窗口", show, default=True),
                MenuItem("回退全部工具配置", restore),
                MenuItem("退出", quit_app),
            ),
        )
    except Exception:
        logger.exception("pystray_icon_failed")
        app.tray = None
        return
    runner = getattr(app.tray, "run_detached", None)
    if callable(runner):
        runner()
        return
    app.tray.run()


def pack_smoke(*, opener=None, url: str = "https://ceshi.007ka.cn/") -> int:
    """Confirm the bundled CA can open the public site and a loopback port can bind."""
    import socket
    import urllib.error
    import urllib.request

    from minking_desktop.api import direct_opener

    request = urllib.request.Request(url, method="GET")
    client = opener or direct_opener()
    try:
        with client.open(request, timeout=20) as response:
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
    except Exception:
        logger.exception("pack_smoke_https_failed")
        return 1
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
    except OSError:
        logger.exception("pack_smoke_bind_failed")
        return 1
    if status <= 0 or port <= 0:
        return 1
    print(f"pack_smoke_ok status={status} port={port}")
    return 0


def run(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--pack-smoke" in args:
        configure_logging()
        return pack_smoke()
    if "--local-api" in args or "--local-web" in args:
        from minking_desktop.local_api import run as run_local
        return run_local([arg for arg in args if arg not in {"--local-api", "--local-web"}])
    configure_logging()
    try:
        return _run(argv)
    except Exception:
        logger.exception("desktop_fatal")
        show_error_dialog(WINDOW_TITLE, startup_failure_message())
        return 1


def _run(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    app = DesktopApp()
    incoming = next((item for item in args if item.lower().startswith("minking:")), None)
    payload = {"cmd": "import", "url": incoming} if incoming else {"cmd": "show"}
    lock = InstanceLock(appdata=app.appdata, on_message=app.handle_instance_message)
    app._instance_lock = lock
    if handoff_existing_instance(title=WINDOW_TITLE, lock=lock, payload=payload):
        logger.info("handed_off_to_existing")
        return 0
    lock.serve()
    try:
        register_protocol(launch_command())
    except OSError:
        logger.info("protocol_register_failed")

    if os.name == "nt":
        import ctypes

        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)
        except OSError:
            pass
        prepare_windows_webview()
        if not webview2_installed():
            show_error_dialog(
                WINDOW_TITLE,
                "本机缺少 Microsoft Edge WebView2 Runtime，窗口无法打开。\n\n"
                f"请先安装：{WEBVIEW2_DOWNLOAD}\n安装后重新打开 MinKingAI.exe。",
            )
            lock.close()
            return 1
        try:
            runtime = load_pythonnet()
            logger.info("pythonnet_loaded runtime=%s", runtime)
        except Exception:
            logger.exception("pythonnet_load_failed")
    try:
        import webview
    except Exception as exc:
        logger.exception("webview_import_failed")
        message = missing_webview_message(exc)
        try:
            sys.stderr.write(message + "\n")
        except Exception:
            pass
        show_error_dialog(WINDOW_TITLE, message)
        lock.close()
        return 1

    index = UI_DIR / "index.html"
    if not index.is_file():
        show_error_dialog(WINDOW_TITLE, f"缺少界面文件，无法打开窗口。\n{index}")
        lock.close()
        return 1

    window = webview.create_window(
        WINDOW_TITLE,
        str(index),
        js_api=JsBridge(app),
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN_SIZE,
        resizable=True,
        background_color=WINDOW_BACKGROUND,
    )
    app.window = window

    def on_shown() -> None:
        hwnd = native_hwnd(window) or find_window_hwnd(WINDOW_TITLE)
        if hwnd:
            lock.update_hwnd(hwnd)
            logger.info("window_shown hwnd=%s", hwnd)

    def on_closing() -> bool:
        if app.close_should_quit():
            logger.info("window_closing quit")
            return True
        logger.info("window_closing hide_to_tray")
        threading.Timer(0.05, app._hide_to_tray).start()
        return False

    window.events.shown += on_shown
    window.events.closing += on_closing
    if incoming:
        app.handle_deep_link(incoming)

    threading.Thread(target=_start_tray, args=(app,), daemon=True, name="minking-tray").start()
    logger.info("webview_start")
    storage = str(app.appdata / "webview")
    try:
        Path(storage).mkdir(parents=True, exist_ok=True)
        webview.start(
            gui="edgechromium" if os.name == "nt" else None,
            debug=False,
            private_mode=False,
            storage_path=storage,
        )
    except Exception as exc:
        logger.exception("webview_start_failed")
        show_error_dialog(WINDOW_TITLE, webview_start_failure_message(exc))
        lock.close()
        return 1
    logger.info("webview_exit")
    app.stop_local()
    lock.close()
    return 0
