"""Portal HTTP client. Never logs tokens, keys, codes, or request bodies."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from minking_desktop.paths import public_v1_url

logger = logging.getLogger("minking_desktop")

RequestFn = Callable[[str, str, dict[str, Any] | None, dict[str, str] | None, float], tuple[int, dict[str, Any] | list[Any] | str]]
BytesFn = Callable[[str, str, dict[str, Any] | None, dict[str, str] | None, float], tuple[int, bytes]]
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

DESKTOP_API_MISSING = "现网还没有桌面登录接口。请先在网页注册/登录，或等服务更新后再用客户端。"
PORTAL_API_MISSING = "服务端尚未更新该接口，请稍后再试。"
_ACCOUNT_FEATURE_PATHS = {
    "/portal/api/calls",
    "/portal/api/wallet",
    "/portal/api/wallet/ledger",
    "/portal/api/wallet/redeem",
}


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = "client_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": self.message, "status": self.status, "code": self.code}


def direct_opener() -> urllib.request.OpenerDirector:
    """Skip the system proxy. urllib does not honor ProxyOverride and adds ~9s per call.

    Frozen macOS builds also need the bundled certifi CA. Python's default
    context looks for a path that PyInstaller does not keep.
    """
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    try:
        import certifi
        import ssl

        context = ssl.create_default_context(cafile=certifi.where())
        handlers.append(urllib.request.HTTPSHandler(context=context))
    except Exception:
        pass
    return urllib.request.build_opener(*handlers)


_OPENER = direct_opener()


def _open(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _parse_body(raw: bytes) -> dict[str, Any] | list[Any] | str:
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return text
    return loaded


def default_request(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: float,
) -> tuple[int, dict[str, Any] | list[Any] | str]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method.upper())
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _open(request, timeout) as response:
            raw = response.read()
            return int(response.status), _parse_body(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        return int(exc.code), _parse_body(raw)
    except urllib.error.URLError as exc:
        raise ApiError("无法连接 MinKing 服务，请检查网络或接口地址", code="network_error") from exc


def default_request_bytes(
    method: str,
    url: str,
    payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
    timeout: float,
) -> tuple[int, bytes]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method.upper())
    request.add_header("Accept", "application/zip, application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with _open(request, timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read() if exc.fp else b""
        return int(exc.code), raw
    except urllib.error.URLError as exc:
        raise ApiError("无法连接 MinKing 服务，请检查网络或接口地址", code="network_error") from exc


def _error_from(status: int, payload: dict[str, Any] | list[Any] | str) -> ApiError:
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or "请求失败")
            code = str(err.get("code") or "request_failed")
            return ApiError(message, status=status, code=code)
        if isinstance(payload.get("message"), str):
            return ApiError(str(payload["message"]), status=status, code=str(payload.get("code") or "request_failed"))
    return ApiError("请求失败", status=status, code="request_failed")


class PortalClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        request: RequestFn | None = None,
        request_bytes: BytesFn | None = None,
        timeout: float = 30,
    ) -> None:
        self.base_url = public_v1_url(base_url)
        self.token = token
        self._request = request or default_request
        self._request_bytes = request_bytes or default_request_bytes
        self.timeout = timeout

    def _headers(self, *, auth: bool) -> dict[str, str]:
        headers = {"User-Agent": "MinKingDesktop/0.1"}
        if auth:
            if not self.token:
                raise ApiError("尚未登录", status=401, code="portal_auth_required")
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        auth: bool = False,
    ) -> dict[str, Any] | list[Any] | str:
        url = self.base_url.rstrip("/") + path
        logger.info("portal_request method=%s path=%s", method.upper(), path.split("?", 1)[0])
        started = time.perf_counter()
        status, body = self._request(method, url, payload, self._headers(auth=auth), self.timeout)
        route = path.split("?", 1)[0]
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info("portal_response method=%s path=%s status=%s duration_ms=%s", method.upper(), route, status, elapsed_ms)
        if status >= 400:
            err = _error_from(status, body)
            if status == 404 and err.code in {"not_found", "request_failed"}:
                if "/desktop/" in route:
                    raise ApiError(DESKTOP_API_MISSING, status=status, code="desktop_api_missing")
                if route in _ACCOUNT_FEATURE_PATHS:
                    raise ApiError(PORTAL_API_MISSING, status=status, code="portal_api_missing")
            raise err
        return body

    def _object(self, body: dict[str, Any] | list[Any] | str, *, message: str, code: str) -> dict[str, Any]:
        if isinstance(body, dict):
            return body
        if isinstance(body, list):
            return {"data": body}
        raise ApiError(message, code=code)

    def _paged(self, path: str, *, page: int, page_size: int, message: str, code: str) -> dict[str, Any]:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(int(page_size or 20), 100))
        query = urllib.parse.urlencode({"page": safe_page, "page_size": safe_size})
        body = self.call("GET", f"{path}?{query}", auth=True)
        payload = self._object(body, message=message, code=code)
        payload.setdefault("page", safe_page)
        payload.setdefault("page_size", safe_size)
        return payload

    def captcha(self) -> dict[str, str]:
        body = self.call("GET", "/portal/api/captcha")
        if not isinstance(body, dict) or "id" not in body or "image" not in body:
            raise ApiError("验证码响应无效", code="invalid_captcha_response")
        return {"id": str(body["id"]), "image": str(body["image"])}

    def send_code(self, *, name: str, email: str, captcha_id: str, captcha: str) -> dict[str, str]:
        body = self.call(
            "POST",
            "/portal/api/desktop/auth/send-code",
            {"name": name, "email": email, "captcha_id": captcha_id, "captcha": captcha},
        )
        if not isinstance(body, dict) or "challenge_id" not in body:
            raise ApiError("发送验证码失败", code="send_code_failed")
        return {"challenge_id": str(body["challenge_id"]), "expires_in": str(body.get("expires_in") or "600")}

    def verify(self, *, challenge_id: str, email: str, code: str, device_id: str) -> dict[str, Any]:
        fingerprint = (device_id or "").strip().lower()
        if len(fingerprint) != 64:
            raise ApiError("无法读取这台电脑的标识", code="device_id_required")
        body = self.call(
            "POST",
            "/portal/api/desktop/auth/verify",
            {"challenge_id": challenge_id, "email": email, "code": code, "device_id": fingerprint},
        )
        if not isinstance(body, dict) or not body.get("token"):
            raise ApiError("验证失败", code="invalid_email_code")
        self.token = str(body["token"])
        return {
            "name": str(body.get("name") or ""),
            "email": str(body.get("email") or email),
            "token": self.token,
            "csrf_token": str(body.get("csrf_token") or ""),
            "expires_in": body.get("expires_in"),
            "token_type": str(body.get("token_type") or "Bearer"),
            "reward_notice": str(body.get("reward_notice") or ""),
        }

    def bootstrap(self) -> dict[str, Any]:
        body = self.call("GET", "/portal/api/desktop/bootstrap", auth=True)
        if not isinstance(body, dict):
            raise ApiError("无法加载工作台", code="bootstrap_failed")
        return body

    def dashboard(self) -> dict[str, Any]:
        body = self.call("GET", "/portal/api/dashboard", auth=True)
        if not isinstance(body, dict):
            raise ApiError("无法加载用量", code="dashboard_failed")
        return body

    def calls(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._paged("/portal/api/calls", page=page, page_size=page_size, message="无法加载调用明细", code="calls_failed")

    def wallet(self) -> dict[str, Any]:
        body = self.call("GET", "/portal/api/wallet", auth=True)
        return self._object(body, message="无法加载钱包", code="wallet_failed")

    def wallet_ledger(self, *, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        return self._paged(
            "/portal/api/wallet/ledger",
            page=page,
            page_size=page_size,
            message="无法加载资金流水",
            code="ledger_failed",
        )

    def redeem(self, code: str) -> dict[str, Any]:
        trimmed = (code or "").strip()
        if not trimmed:
            raise ApiError("请输入卡密", code="redeem_code_required")
        body = self.call("POST", "/portal/api/wallet/redeem", {"code": trimmed}, auth=True)
        return self._object(body, message="兑换失败", code="redeem_failed")

    def reveal_key(self) -> dict[str, str]:
        body = self.call("POST", "/portal/api/desktop/key", auth=True)
        if not isinstance(body, dict) or not body.get("key"):
            raise ApiError("无法读取密钥", code="key_not_recoverable")
        return {"key": str(body["key"]), "prefix": str(body.get("prefix") or "")}

    def list_skills(self) -> dict[str, Any]:
        body = self.call("GET", "/portal/api/desktop/skills", auth=True)
        payload = self._object(body, message="无法加载技能列表", code="skills_failed")
        skills = payload.get("skills")
        if not isinstance(skills, list):
            raise ApiError("技能列表无效", code="skills_failed")
        return {"skills": [item for item in skills if isinstance(item, dict) and item.get("name")]}

    def download_skill(self, name: str) -> bytes:
        if not _SKILL_NAME.fullmatch(name or ""):
            raise ApiError("未知技能", code="invalid_skill")
        path = f"/portal/api/desktop/skills/{name}"
        url = self.base_url.rstrip("/") + path
        logger.info("portal_request method=GET path=%s", path)
        started = time.perf_counter()
        status, raw = self._request_bytes("GET", url, None, self._headers(auth=True), self.timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        logger.info("portal_response method=GET path=%s status=%s duration_ms=%s", path, status, elapsed_ms)
        if status >= 400:
            raise _error_from(status, _parse_body(raw))
        if not raw:
            raise ApiError("技能包为空", code="invalid_skill_zip")
        return raw

    def fetch_model_catalog(self, api_key: str) -> list[dict[str, Any]]:
        # No client_version: the Codex picker deliberately contains text models only.
        status, body = self._request("GET", self.base_url.rstrip("/") + "/models", None,
            {"Authorization": f"Bearer {api_key}", "User-Agent": "MinKingDesktop/0.2", "Accept": "application/json"}, self.timeout)
        if status >= 400:
            raise ApiError("云端模型目录加载失败，请稍后重试", status=status, code="model_catalog_failed")
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ApiError("云端模型目录格式无效", code="invalid_model_catalog")
        from app.providers.registry import is_image_model, is_video_model
        models = []
        seen = set()
        for item in body["data"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in seen:
                continue
            slug = item["id"]
            seen.add(slug)
            kind = item.get("type")
            if kind not in {"text", "image", "video"}:
                kind = "video" if is_video_model(slug) else "image" if is_image_model(slug) else "text"
            models.append({**item, "official_id": slug, "type": kind, "source": "cloud_api", "availability": "cloud_listed"})
        return models

    def fetch_codex_catalog(self, api_key: str) -> dict[str, Any] | None:
        url = self.base_url.rstrip("/") + "/models?client_version=bundle"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "MinKingDesktop/0.1",
            "Accept": "application/json",
        }
        logger.info("portal_request method=GET path=/models")
        status, body = self._request("GET", url, None, headers, self.timeout)
        logger.info("portal_response method=GET path=/models status=%s", status)
        if status >= 400 or not isinstance(body, dict):
            return None
        models = body.get("models")
        if isinstance(models, list) and any(isinstance(item, dict) and item.get("slug") and item.get("display_name") for item in models):
            return {"models": [item for item in models if isinstance(item, dict) and item.get("slug")]}
        return None

    def logout(self) -> None:
        if not self.token:
            return
        try:
            self.call("POST", "/portal/api/desktop/logout", auth=True)
        except ApiError:
            logger.info("portal_logout_remote_failed")
        self.token = None
