"""Local official accounts. Never use the server account pool or configurable upstreams.

Only protocol helpers are shared with app.providers. Credentials remain in memory;
the original applications own login and refresh, and their files are read-only.
"""
from __future__ import annotations

import asyncio
import codecs
import datetime as dt
import json
import os
import re
import shutil
import tomllib
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import ssl

import certifi
import httpx
from fastapi import HTTPException

from app.providers import antigravity as ag
from app.providers import codex, codex_protocol as cp, workbuddy as wb, grok
from app.providers.image_protocol import _image_responses_payload, _image_results_from_sse
from app.providers.video_protocol import shape_grok_video_body
from minking_desktop.paths import localappdata_root, workbuddy_credential_files

_refreshed_codex_version = False


async def codex_models_probe_path(auth_mode: str) -> str:
    """ChatGPT /models requires the current stable Codex CLI version."""
    if auth_mode != "chatgpt":
        return "/models"
    global _refreshed_codex_version
    from app.config import settings

    if not _refreshed_codex_version:
        try:
            from app.providers.codex_version import refresh_codex_client_version

            await refresh_codex_client_version()
        except Exception:
            pass
        _refreshed_codex_version = True
    return f"/models?client_version={settings.codex_client_version}"


OFFICIAL = {
    "grok": "https://cli-chat-proxy.grok.com/v1",
    "grok_api": "https://api.x.ai/v1",
    "codex": "https://chatgpt.com/backend-api/codex",
    "openai": "https://api.openai.com/v1",
    "claude_code": "https://api.anthropic.com/v1",
    "workbuddy": "https://copilot.tencent.com",
    "workbuddy_global": "https://www.workbuddy.ai",
    "antigravity": "https://daily-cloudcode-pa.googleapis.com",
}
NAMES = {"codex": "Codex", "grok": "Grok", "workbuddy": "WorkBuddy", "claude_code": "Claude Code", "antigravity": "Antigravity"}
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


def read_object(path: Path) -> dict:
    try:
        if path.stat().st_size > 2_000_000:
            return {}
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, UnicodeError):
        return {}


def official_url(value: Any, root: str) -> bool:
    return not value or str(value).rstrip("/") in {root, root.removesuffix("/v1")}


def system_proxy() -> str | None:
    """Use an existing local HTTP CONNECT proxy, never an alternative API base."""
    value = urllib.request.getproxies().get("https")
    if value:
        value = value if "://" in value else "http://" + value
        parsed = urlsplit(value)
        if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
            return value
    return None


@dataclass
class Account:
    provider: str
    credential: Any = field(repr=False, default=None)
    detected: bool = False
    source: str = "none"
    status: str = "missing"
    detail: str = "未发现可读取的官方登录凭据"
    configured_model: str | None = None
    models: list[dict] = field(default_factory=list)
    verified_at: str | None = None

    def public(self) -> dict:
        return {"id": self.provider, "name": NAMES[self.provider], "detected": self.detected,
                "credential_source": self.source, "status": self.status, "detail": self.detail,
                "configured_model": self.configured_model, "models": self.models,
                "verified_at": self.verified_at, "quota": None}


class OfficialGateway:
    def __init__(self, home: Path, appdata: Path, *, transport=None, localappdata: Path | None = None):
        self.home, self.appdata = home, appdata
        self.localappdata = localappdata_root(home=home, localappdata=localappdata)
        # Use only an existing loopback CONNECT proxy; never inherit API base URLs.
        self.proxy = system_proxy() if transport is None else None
        client_kwargs = {
            "transport": transport,
            "trust_env": False,
            "follow_redirects": False,
            "proxy": self.proxy,
            "timeout": httpx.Timeout(180, connect=15),
        }
        if transport is None:
            client_kwargs["verify"] = ssl.create_default_context(cafile=certifi.where())
        self.client = httpx.AsyncClient(**client_kwargs)
        self.accounts: dict[str, Account] = {}
        self.lock = asyncio.Lock()
        # The server sanitizer has optional diagnostic logging of tool outputs.
        # Local mode never records request or response contents.
        grok.logger.disabled = True

    async def close(self):
        await self.client.aclose()

    @staticmethod
    def model(provider: str, name: str, source: str, *, verified=False, kind="text") -> dict | None:
        if not isinstance(name, str) or not MODEL_ID.fullmatch(name) or name.startswith("minking"):
            return None
        if kind == "text":
            kind = "video" if "video" in name.lower() else "image" if "image" in name.lower() else "text"
        return {"id": f"{provider}/{name}", "official_id": name, "provider": provider,
                "source": source, "availability": "listed" if verified else "unverified",
                "type": kind, "capabilities": [kind]}

    def media_catalog(self, account):
        adapters = {"grok": grok.GrokAdapter, "workbuddy": wb.WorkBuddyAdapter,
                    "codex": codex.CodexAdapter, "antigravity": ag.AntigravityAdapter}
        factory = adapters.get(account.provider)
        if not factory:
            return
        known = {m["id"] for m in account.models}
        for info in factory().catalog():
            if info.type not in {"image", "video"} or getattr(info, "alias_of", None):
                continue
            name = info.id.removeprefix(account.provider + "/")
            model = self.model(account.provider, name, "adapter_catalog", kind=info.type)
            if model and model["id"] not in known:
                account.models.append(model)
                known.add(model["id"])

    async def sync(self):
        await self.scan()
        async def verify(account):
            if not account.credential or account.status == "expired":
                return
            self.media_catalog(account)
            try:
                await self.probe(account.provider)
            except HTTPException:
                pass  # Each account retains its own failure, without blocking other platforms.
        await asyncio.gather(*(verify(a) for a in self.accounts.values()))
        return self.public()

    def _found(self, account: Account, credential: Any, source: str):
        account.credential, account.source = credential, source
        account.detected, account.status = True, "detected"
        account.detail = "检测到凭据，尚未连接官方验证"
        expiry = getattr(credential, "expires_at", None)
        if isinstance(credential, dict):
            raw = credential.get("expiresAt")
            if isinstance(raw, (int, float)):
                try:
                    expiry = dt.datetime.fromtimestamp(raw / 1000 if raw > 10**11 else raw, dt.UTC)
                except (ValueError, OverflowError, OSError):
                    pass
        if expiry and expiry < dt.datetime.now(dt.UTC):
            account.status, account.detail = "expired", "凭据已过期，请在官方客户端重新登录或刷新后扫描"

    async def scan(self) -> list[dict]:
        async with self.lock:
            self.accounts = await asyncio.to_thread(self._scan)
        return self.public()

    def _scan(self) -> dict[str, Account]:
        accounts = {p: Account(p) for p in NAMES}
        a = accounts["grok"]
        root = self.home / ".grok"
        a.detected = root.exists() or bool(shutil.which("grok"))
        try:
            path = root / "auth.json"
            entries = grok.parse_auth_payload(read_object(path), source="local", path=path)
            self._found(a, grok.load_credentials(entries[0].payload, path=path), "official_file")
        except (HTTPException, ValueError, IndexError):
            pass
        config = read_toml(root / "config.toml")
        if config.get("model_provider", "xai") in {"xai", "grok"}:
            a.configured_model = config.get("model")
            if a.configured_model and (m := self.model(a.provider, a.configured_model, "local_config")):
                a.models = [m]
        a = accounts["codex"]
        root = self.home / ".codex"
        config = read_toml(root / "config.toml")
        provider = config.get("model_provider", "openai")
        providers = config.get("model_providers") or {}
        provider_config = providers.get(provider, {}) if isinstance(providers, dict) and isinstance(provider, str) else {}
        provider_config = provider_config if isinstance(provider_config, dict) else {}
        is_official = provider == "openai" and official_url(provider_config.get("base_url"), OFFICIAL["openai"])
        a.detected = root.exists() or bool(shutil.which("codex"))
        a.configured_model = config.get("model") if is_official else None
        paths = [(root / "auth.json", "official_file", is_official),
                 (self.appdata / "local-profiles/codex/official/auth.json", "official_snapshot", True),
                 (self.appdata / "profiles/codex/official/auth.json", "official_snapshot", True)]
        paths.extend((path, "official_history", True) for path in self._codex_history_auth_paths())
        for path, source, allow_key in paths:
            raw = read_object(path)
            if source in {"official_snapshot", "official_history"} and not raw.get("tokens"):
                snapshot_config = read_toml(path.parent / "config.toml")
                snapshot_provider = snapshot_config.get("model_provider", "openai")
                snapshot_providers = snapshot_config.get("model_providers") or {}
                snapshot_entry = snapshot_providers.get(snapshot_provider, {}) if isinstance(snapshot_providers, dict) else {}
                allow_key = snapshot_provider == "openai" and official_url(snapshot_entry.get("base_url"), OFFICIAL["openai"])
            if isinstance(raw.get("OPENAI_API_KEY"), str) and raw["OPENAI_API_KEY"].startswith(("mk-local-", "sk-ts-")):
                allow_key = False
            if not raw or (not raw.get("tokens") and not allow_key):
                continue
            try:
                credential = codex.parse_auth_payload(raw, source="local", path=path)
            except (HTTPException, ValueError):
                continue
            self._found(a, credential, source)
            break
        if a.configured_model:
            model = self.model(a.provider, a.configured_model, "local_config")
            a.models = [model] if model else []

        a = accounts["workbuddy"]
        files = workbuddy_credential_files(home=self.home, localappdata=self.localappdata)
        path = next((item for item in files if item.is_file()), files[0])
        a.detected = any(item.exists() for item in files) or (self.home / ".workbuddy").exists() or bool(shutil.which("workbuddy"))
        try:
            self._found(a, wb.load_credentials(read_object(path), path=path), "official_file")
            # Server catalog is only a candidate list, never proof of account entitlement.
            a.models = [m for name in wb._MODELS if (m := self.model(a.provider, name, "adapter_catalog"))]
        except ValueError:
            pass

        a = accounts["claude_code"]
        root = self.home / ".claude"
        config = read_object(root / "settings.json")
        a.detected = root.exists() or bool(shutil.which("claude"))
        env = config.get("env") if isinstance(config.get("env"), dict) else {}
        is_official = official_url(env.get("ANTHROPIC_BASE_URL"), OFFICIAL["claude_code"])
        raw = read_object(root / ".credentials.json").get("claudeAiOauth")
        if isinstance(raw, dict) and isinstance(raw.get("accessToken"), str) and raw["accessToken"]:
            self._found(a, {**raw, "auth_mode": "oauth"}, "official_file")
        elif is_official and env.get("ANTHROPIC_API_KEY"):
            self._found(a, {"accessToken": env["ANTHROPIC_API_KEY"], "auth_mode": "api"}, "official_config")
        a.configured_model = config.get("model") if is_official else None
        if a.configured_model and (m := self.model(a.provider, a.configured_model, "local_config")):
            a.models = [m]

        a = accounts["antigravity"]
        roots = [self.home / ".gemini/antigravity", self.home / ".gemini/antigravity-ide",
                 self.appdata.parent / "Antigravity", self.localappdata / "Antigravity"]
        a.detected = any(p.exists() for p in roots) or bool(shutil.which("antigravity"))
        # The server's credential-manager decoder and bounded JSON scanner are reused.
        candidates = [(raw, "windows_credential_manager") for raw in ag.windows_credential_payloads()]
        for root in roots:
            for path in ag._candidate_json_files(root)[:500]:
                candidates.append((read_object(path), "official_file"))
        for raw, source in candidates:
            try:
                credential = ag.load_credentials(raw, path=Path("unused-read-only"))
            except (HTTPException, ValueError, TypeError):
                continue
            self._found(a, credential, source)
            break
        return accounts

    def _codex_history_auth_paths(self) -> list[Path]:
        """Newest backup first. Live MinKing keys are skipped by the caller."""
        found: list[Path] = []
        for root_name in ("local-profiles", "profiles"):
            backups = self.appdata / root_name / "codex" / "backups"
            if not backups.is_dir():
                continue
            directories = sorted(
                (item for item in backups.iterdir() if item.is_dir()),
                key=lambda item: item.name,
                reverse=True,
            )
            for directory in directories:
                path = directory / "auth.json"
                if path.is_file():
                    found.append(path)
        return found

    def public(self) -> list[dict]:
        return [a.public() for a in self.accounts.values()]

    def headers(self, a: Account) -> dict:
        c = a.credential
        if a.provider == "grok":
            return grok.build_headers(c)
        if a.provider == "codex":
            return codex.build_headers(c)
        if a.provider == "workbuddy":
            return wb.chat_headers(c)
        if a.provider == "antigravity":
            return ag.build_headers(c)
        return {"anthropic-version": "2023-06-01", "Content-Type": "application/json",
                **({"Authorization": f"Bearer {c['accessToken']}", "anthropic-beta": "oauth-2025-04-20"}
                   if c["auth_mode"] == "oauth" else {"x-api-key": c["accessToken"]})}

    def base(self, a: Account) -> str:
        if a.provider == "grok" and a.credential.auth_mode == "api_key":
            return OFFICIAL["grok_api"]
        if a.provider == "codex" and a.credential.auth_mode == "api":
            return OFFICIAL["openai"]
        if a.provider == "workbuddy" and a.credential.realm == "global":
            return OFFICIAL["workbuddy_global"]
        return OFFICIAL[a.provider]

    def require(self, provider: str) -> Account:
        a = self.accounts.get(provider)
        if not a or a.credential is None:
            raise HTTPException(409, "未找到官方凭据，请先在官方客户端登录并重新扫描")
        if a.status == "expired":
            raise HTTPException(401, "官方凭据已过期，请在官方客户端刷新登录后重新扫描")
        return a

    async def request(self, a: Account, method: str, suffix: str, *, body=None, stream=False, timeout=180, codex_lite=True):
        headers = self.headers(a)
        if suffix.startswith(("/images/", "/v1/images/", "/videos")):
            headers["Accept"] = "application/json"
        if codex_lite and a.provider == "codex" and a.credential.auth_mode == "chatgpt" and isinstance(body, dict) and cp.responses_lite_enabled(str(body.get("model", ""))):
            headers[cp.RESPONSES_LITE_HEADER] = "true"
        request = self.client.build_request(method, self.base(a) + suffix, headers=headers,
                                            json=body, timeout=httpx.Timeout(timeout, connect=20))
        try:
            response = await self.client.send(request, stream=stream)
        except httpx.TimeoutException:
            raise HTTPException(504, "连接官方服务超时") from None
        except httpx.HTTPError:
            raise HTTPException(502, "无法连接官方服务") from None
        if not 200 <= response.status_code < 300:
            status = response.status_code
            if isinstance(body, dict) and status in {400, 403, 404}:
                for model in a.models:
                    if model["official_id"] == body.get("model"):
                        model["availability"] = "unavailable"
                        model["detail"] = f"官方接口拒绝调用（HTTP {status}）"
            await response.aclose()
            if status in (401, 403):
                a.status, a.detail = "rejected", "官方拒绝授权，请检查账号权限或重新登录"
            raise HTTPException(status if status in (400, 401, 403, 404, 405, 429) else 502,
                                f"官方服务返回 HTTP {status}；未切换模型或上游")
        return response

    async def probe(self, provider: str) -> dict:
        a = self.require(provider)
        try:
            if provider == "workbuddy":
                response = await self.request(a, "POST", "/v2/billing/meter/get-user-resource",
                    body={"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3], "OnlyValidPeriod": True}, timeout=20)
            elif provider == "antigravity":
                response = await self.request(a, "POST", ag.FETCH_AVAILABLE_MODELS_PATH, body={}, timeout=20)
            else:
                suffix = await codex_models_probe_path(a.credential.auth_mode if provider == "codex" else "")
                response = await self.request(a, "GET", suffix, timeout=20)
            data = response.json()
            if not isinstance(data, dict) or data.get("error") or (provider == "workbuddy" and data.get("code") not in (None, 0, "0")):
                raise HTTPException(502, "官方状态接口未返回有效结果")
            if provider != "workbuddy":
                rows = data.get("models", data.get("data", []))
                if isinstance(rows, dict):
                    rows = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in rows.items()]
                if not isinstance(rows, list):
                    raise HTTPException(502, "官方模型目录格式无法识别")
                a.models = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    found = self.model(provider, row.get("id") or row.get("slug"), "official_api", verified=True)
                    if not found:
                        continue
                    if provider == "codex":
                        from app.providers.codex_catalog import official_catalog_overlay
                        overlay = official_catalog_overlay(row)
                        if overlay:
                            found["upstream"] = overlay
                    a.models.append(found)
            a.status, a.detail = "verified", "官方状态接口验证成功；模型实际调用权限以调用结果为准"
            self.media_catalog(a)
            a.verified_at = dt.datetime.now(dt.UTC).isoformat()
        except (ValueError, TypeError):
            raise HTTPException(502, "官方状态接口返回格式无法识别") from None
        except HTTPException as exc:
            if a.status != "rejected":
                a.status, a.detail = "unverified", str(exc.detail)
            raise
        return a.public()

    def resolve(self, full_model: Any) -> tuple[Account, str]:
        if not isinstance(full_model, str) or "/" not in full_model:
            raise HTTPException(422, "model 必须包含平台前缀，例如 codex/官方模型ID")
        provider, model = full_model.split("/", 1)
        if provider not in NAMES or not MODEL_ID.fullmatch(model) or model.startswith("minking"):
            raise HTTPException(422, "请选择官方模型 ID，不支持云端别名")
        return self.require(provider), model

    async def generate(self, body: dict, endpoint: str) -> tuple[AsyncIterator[bytes], Account, str]:
        a, model = self.resolve(body.get("model"))
        payload = {**body, "model": model}
        if endpoint == "messages":
            if a.provider != "claude_code":
                from app.providers.anthropic_protocol import anthropic_to_responses
                translated = anthropic_to_responses(body, default_model=body["model"])
                translated["model"] = body["model"]
                return await self.generate(translated, "responses")
            response = await self.request(a, "POST", "/messages", body=payload, stream=True)
            return self._raw(response), a, "native"
        if endpoint == "images/generations":
            if a.provider in {"codex", "antigravity"} and not (a.provider == "codex" and a.credential.auth_mode == "api"):
                return self._generate_image(a, payload), a, "native"
            if a.provider == "grok":
                suffix = "/images/generations"
                payload = grok.shape_grok_image_body("generations", payload)
            elif a.provider == "workbuddy":
                suffix = "/v1/images/generations"
                payload.setdefault("n", 1)
                payload.setdefault("response_format", "b64_json")
            elif a.provider == "codex" and a.credential.auth_mode == "api":
                suffix = "/images/generations"
            else:
                raise HTTPException(501, "该官方登录方式尚未接入图片生成；不会转发到其他平台")
            response = await self.request(a, "POST", suffix, body=payload, stream=True)
            return self._raw(response), a, "native"
        if endpoint == "videos":
            if a.provider != "grok":
                raise HTTPException(501, "当前只有 Grok 适配器接入官方视频生成")
            payload = shape_grok_video_body(payload)
            payload["model"] = model
            response = await self.request(a, "POST", "/videos/generations", body=payload, stream=True)
            return self._raw(response), a, "native"
        if payload.get("previous_response_id") or payload.get("conversation"):
            raise HTTPException(422, "本地网关不保存会话，请传入完整对话历史")
        if endpoint == "chat/completions":
            instructions, items = cp.chat_messages_to_input(payload.get("messages", []))
            envelope = {**payload, "instructions": instructions, "input": items}
        else:
            envelope = dict(payload)
        if not isinstance(envelope.get("input"), (str, list)):
            raise HTTPException(422, "请提供 input 或 messages")
        if a.provider == "claude_code":
            from minking_desktop.claude_protocol import messages_payload, as_chat_chunks
            response = await self.request(a, "POST", "/messages", body=messages_payload(envelope, model), stream=True)
            return wb.stream_chat_as_responses(as_chat_chunks(self._raw(response)), model=model), a, "responses"
        if a.provider == "grok":
            if endpoint == "responses":
                mapped = grok.sanitize_grok_responses(payload)
                wire = mapped.payload
                wire["model"] = model
                wire["stream"] = True
                response = await self.request(a, "POST", "/responses", body=wire, stream=True)
                return self._grok_responses(response, mapped.rewrite), a, "responses"
            wire = grok.sanitize_grok_chat_payload(payload)
            wire["model"] = model
            response = await self.request(a, "POST", "/" + endpoint, body=wire, stream=True)
            return self._raw(response), a, "native"
        if a.provider == "codex":
            if a.credential.auth_mode == "api":
                response = await self.request(a, "POST", "/" + endpoint, body=payload, stream=True)
                return self._raw(response), a, "native"
            envelope, _ = cp.to_responses_payload(payload, default_model=model)
            response = await self.request(a, "POST", "/responses", body=envelope, stream=True)
            return self._raw(response), a, "responses"
        if a.provider == "workbuddy":
            try:
                chat = payload if endpoint == "chat/completions" else wb.responses_to_chat(envelope, model=model)
                chat = wb.prepare_chat_payload(chat, realm=a.credential.realm)
            except ValueError:
                raise HTTPException(422, "无法转换请求，请检查消息与工具格式") from None
            response = await self.request(a, "POST", "/v2/chat/completions", body=chat, stream=True)
            return wb.stream_chat_as_responses(self._checked_chat(response), model=model), a, "responses"
        project = a.credential.project_id
        if not project:
            response = await self.request(a, "POST", ag.LOAD_CODE_ASSIST_PATH,
                                          body={"metadata": ag.LOAD_CODE_ASSIST_METADATA}, timeout=20)
            project = ag.extract_project_id(response.json())
        if not project:
            raise HTTPException(409, "官方账号缺少项目授权，请先在 Antigravity 完成初始化")
        cloud = ag.responses_to_cloudcode(envelope, project=project)
        cloud["model"] = model  # Preserve actual official ID; never use server aliases.
        name_map = cloud.pop("_ts_claude_names", None)
        response = await self.request(a, "POST", ag.STREAM_PATH, body=cloud, stream=True)
        return self._antigravity(response, model, name_map), a, "responses"

    async def _generate_image(self, account, payload):
        if account.provider == "antigravity":
            project = account.credential.project_id
            if not project:
                loaded = await self.request(account, "POST", ag.LOAD_CODE_ASSIST_PATH,
                    body={"metadata": ag.LOAD_CODE_ASSIST_METADATA}, timeout=20)
                project = ag.extract_project_id(loaded.json())
            if not project:
                raise HTTPException(409, "请先在 Antigravity 完成项目授权")
            body = ag.build_image_cloudcode_envelope(project=project, prompt=payload.get("prompt", ""),
                images=[], mask=None, model=payload["model"])
            response = await self.request(account, "POST", ag.STREAM_PATH, body=body, timeout=200)
            result = {"data": [{"b64_json": ag.extract_image_b64_from_cloudcode(response.content)}]}
        else:
            from minking_desktop.cloud_response import collect_response
            try:
                direct = await self.request(account, "POST", "/images/generations", body=payload, timeout=200)
                yield json.dumps(direct.json()).encode("utf-8")
                return
            except HTTPException as exc:
                if exc.status_code not in {400, 404, 405}:
                    raise
            body, _ = _image_responses_payload("generation", json_body=payload, files=None)
            response = await self.request(account, "POST", "/responses", body=body, timeout=200, codex_lite=False)
            status, completed = collect_response(response.text)
            if status != 200:
                raise HTTPException(502, "官方图片生成未完成")
            blocks, remainder = cp.iter_sse_blocks(response.text)
            if remainder.strip():
                blocks.append(remainder)
            images = _image_results_from_sse([cp.parse_sse_event(block) for block in blocks], completed)
            if not images:
                raise HTTPException(502, "官方响应没有图片；该账号或宿主模型可能不支持图片工具")
            result = {"data": images}
        yield json.dumps(result).encode("utf-8")

    async def _grok_responses(self, response, rewrite):
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, state = "", {}
        try:
            async for chunk in response.aiter_bytes():
                buffer += decoder.decode(chunk)
                blocks, buffer = cp.iter_sse_blocks(buffer)
                for block in blocks:
                    for output in grok.rewrite_grok_codex_sse_block(block, rewrite, state):
                        yield (output + "\n\n").encode("utf-8")
            buffer += decoder.decode(b"", final=True)
            if buffer.strip():
                for output in grok.rewrite_grok_codex_sse_block(buffer, rewrite, state):
                    yield (output + "\n\n").encode("utf-8")
        finally:
            await response.aclose()

    async def _raw(self, response: httpx.Response):
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()

    async def _checked_chat(self, response):
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, terminal = "", False
        def inspect(block):
            nonlocal terminal
            _, data = cp.parse_sse_event(block)
            if data == "[DONE]":
                terminal = True
            elif data:
                event = json.loads(data)
                if event.get("error"):
                    raise HTTPException(502, "官方聊天流返回错误")
                if any(c.get("finish_reason") is not None for c in event.get("choices", [])):
                    terminal = True
        try:
            async for chunk in response.aiter_bytes():
                buffer += decoder.decode(chunk)
                blocks, buffer = cp.iter_sse_blocks(buffer)
                for block in blocks:
                    inspect(block)
                yield chunk
            buffer += decoder.decode(b"", final=True)
            if buffer.strip():
                inspect(buffer)
                yield b"\n\n"
            if not terminal:
                raise HTTPException(502, "官方聊天流未正常结束")
        finally:
            await response.aclose()

    async def _antigravity(self, response, model, name_map):
        # Server converter assembles tool calls and thought signatures from the full stream.
        try:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > 64 * 1024 * 1024:
                    raise HTTPException(502, "官方响应超过本地大小限制")
            raw = bytes(raw)
            yield ag.cloudcode_bytes_to_codex_sse(raw, model=model, name_map=name_map)
        finally:
            await response.aclose()
