from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from app.config import settings
from app.http_client import new_client
from app.providers.oauth_login import parse_oauth_callback
from app.providers.base import (
    CAP_CHAT,
    CAP_IMAGE,
    CAP_IMAGE_EDIT,
    CAP_RESPONSES,
    CAP_STREAM,
    ImportedAccount,
    ModelInfo,
)

DEFAULT_HOST = "https://daily-cloudcode-pa.googleapis.com"
USER_AGENT = (
    "antigravity/ide/2.12.2 (Windows NT 10.0; Win64; x64) "
    "aidev_client; auth_method=oauth"
)
DEFAULT_MODEL = "gemini-3.8-flash"
IMAGE_MODEL = "gemini-3.1-flash-image"
PUBLIC_MODELS = (
    ("gemini-3.8-flash", True),
    ("gemini-3.7-flash", False),
    ("gemini-3.1-pro", False),
    ("claude-sonnet-4-6", False),
    ("claude-opus-4-6-thinking", False),
)
ALIAS_MODELS = (
    ("gemini-3-flash", DEFAULT_MODEL),
    ("gemini-3.5-flash", DEFAULT_MODEL),
    ("gemini-3.6-flash", DEFAULT_MODEL),
    ("claude-opus-4-6", "claude-opus-4-6-thinking"),
    ("claude-sonnet-4-6-thinking", "claude-sonnet-4-6"),
)
ALIAS_TO_PUBLIC = {alias: target for alias, target in ALIAS_MODELS}
WIRE_MODELS = {
    "gemini-3.8-flash": "gemini-3.8-flash-tiered",
    "gemini-3.7-flash": "gemini-3.7-flash-tiered",
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
    "gemini-3.1-flash-image": "gemini-3.1-flash-image",
}
WIRE_MODELS_HIGH_EFFORT = {
    "gemini-3.1-pro": "gemini-pro-agent",
}
_TEXT_CAPS = (CAP_RESPONSES, CAP_CHAT, CAP_STREAM)
_IMAGE_CAPS = (CAP_IMAGE, CAP_IMAGE_EDIT)
_ACCOUNT_CAPS = (CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT)
TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
OAUTH_CALLBACK_PORT = 51121
OAUTH_BIND_HOSTS = ("127.0.0.1", "::1")
OAUTH_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
    "openid",
)
WINDOWS_CREDENTIAL_TARGET = "gemini:antigravity"
STREAM_PATH = "/v1internal:streamGenerateContent"
LOAD_CODE_ASSIST_PATH = "/v1internal:loadCodeAssist"
FETCH_AVAILABLE_MODELS_PATH = "/v1internal:fetchAvailableModels"
LOAD_CODE_ASSIST_METADATA = {
    "ideType": "ANTIGRAVITY",
    "platform": "PLATFORM_UNSPECIFIED",
    "pluginType": "GEMINI",
}
MAX_SYSTEM_INSTRUCTION_CHARS = 250_000
HUGE_SYSTEM_INSTRUCTION_CHARS = 80_000
_REFRESH_WINDOW = dt.timedelta(minutes=5)
_HOSTED_TOOL_TYPES = frozenset(
    {"image_generation", "web_search", "computer_use", "computer_use_preview"}
)
_FREEFORM_TOOL_TYPES = frozenset({"custom", "freeform", "custom_tool"})
_NAMED_TYPE_TOOLS = frozenset(
    {"apply_patch", "shell", "local_shell", "shell_command", "unified_exec", "exec"}
)
_PATCH_TOOL_NAMES = frozenset({"apply_patch", "ApplyPatch"})
_CALL_INPUT_TYPES = frozenset({"function_call", "custom_tool_call", "apply_patch_call"})
_OUTPUT_INPUT_TYPES = frozenset(
    {"function_call_output", "custom_tool_call_output", "apply_patch_call_output"}
)
# Gemini 3 requires thought_signature on functionCall parts when replaying tool history.
# Codex clients often strip unknown fields, so keep a process-local call_id cache too.
_THOUGHT_SIGNATURE_CACHE_MAX = 4096
_thought_signature_lock = threading.Lock()
_thought_signature_by_call_id: OrderedDict[str, str] = OrderedDict()
_SKIP_INPUT_TYPES = frozenset(
    {
        "image_generation_call",
        "web_search_call",
        "file_search_call",
        "computer_call",
        "computer_call_output",
        "computer_use_call",
        "additional_tools",
        "reasoning",
        "item_reference",
        "tool_search",
        "tool_search_call",
        "tool_search_output",
    }
)
_CLAUDE_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
_APPLY_PATCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": "Full apply_patch document including *** Begin Patch.",
        }
    },
    "required": ["input"],
}
_SCHEMA_TYPE_ALIASES = {
    "object": "object",
    "string": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
    "array": "array",
}
_THINKING_LEVELS = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "x-high": "high",
}
_CLAUDE_THINKING_BUDGETS = {
    "none": 1024,
    "minimal": 1024,
    "low": 1024,
    "medium": 1024,
    "high": 1024,
    "xhigh": 1024,
    "x-high": 1024,
}
_CLAUDE_MAX_OUTPUT_TOKENS = 64000
_IMAGE_BLOCK_TYPES = frozenset({"input_image", "output_image", "image_url"})
_TEXT_BLOCK_TYPES = frozenset({"input_text", "output_text", "text"})
_SKIP_DIR_NAMES = frozenset(
    {
        "brain",
        "conversations",
        "cache",
        "code cache",
        "gpucache",
        "chrome",
        "crashpad",
        "cacheddata",
        "logs",
        "network",
        "session storage",
        "local storage",
        "blob_storage",
        "dawngraphitecache",
        "dawnwebgpucache",
        "shared dictionary",
        "crashes",
        "knowledge",
        "scratch",
        "annotations",
        "bin",
        "builtin",
        "history",
    }
)


def local_oauth_roots() -> list[Path]:
    roots = [
        Path.home() / ".gemini" / "antigravity",
        Path.home() / ".gemini" / "antigravity-ide",
    ]
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        roots.append(Path(appdata) / "Antigravity")
    if localappdata:
        roots.append(Path(localappdata) / "Antigravity")
    return roots


_PUBLIC_TEXT_IDS = {model_id for model_id, _default in PUBLIC_MODELS}
_EFFORT_SUFFIXES = ("-extra-low", "-low", "-medium", "-high", "-tiered")


def public_slug_for_variant(model_id: str) -> str | None:
    """Fold gemini-3.8-flash-medium onto the public slug. Other families stay."""
    name = (model_id or "").strip()
    if not name:
        return None
    for suffix in _EFFORT_SUFFIXES:
        if not name.endswith(suffix):
            continue
        base = name[: -len(suffix)]
        target = ALIAS_TO_PUBLIC.get(base, base)
        if target in _PUBLIC_TEXT_IDS:
            return target
        return None
    return None


def public_model(slug: str) -> str:
    name = (slug or "").strip() or DEFAULT_MODEL
    folded = public_slug_for_variant(name)
    if folded:
        return folded
    return ALIAS_TO_PUBLIC.get(name, name)


def is_claude_model(slug: str) -> bool:
    return public_model(slug).startswith("claude-")


def wire_model(slug: str, *, high_effort: bool = False) -> str:
    name = public_model(slug)
    if high_effort and name in WIRE_MODELS_HIGH_EFFORT:
        return WIRE_MODELS_HIGH_EFFORT[name]
    return WIRE_MODELS.get(name, name)


def _field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name not in payload:
            continue
        value = payload[name]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _text(payload: dict[str, Any], *names: str) -> str | None:
    value = _field(payload, *names)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _expires_at_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return dt.datetime.fromtimestamp(seconds, tz=dt.UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return _expires_at_text(int(text))
        return text
    return None


def _b64url_json(segment: str) -> dict[str, Any] | None:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _email_from_id_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    if len(parts) < 2:
        return None
    payload = _b64url_json(parts[1])
    if not payload:
        return None
    email = payload.get("email")
    if isinstance(email, str) and email.strip() and "@" in email:
        return email.strip()
    return None


def _flatten_oauth_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    nested = row.get("token")
    if isinstance(nested, dict):
        for key, value in nested.items():
            out.setdefault(key, value)
    return out


def _canonical_payload(row: dict[str, Any]) -> dict[str, Any]:
    row = _flatten_oauth_row(row)
    id_token = _field(row, "id_token", "idToken")
    email = _text(row, "email") or _email_from_id_token(id_token)
    access = _text(row, "accessToken", "access_token")
    refresh = _text(row, "refreshToken", "refresh_token")
    if not email or not access or not refresh:
        return {}
    stored: dict[str, Any] = {
        "email": email,
        "accessToken": access,
        "refreshToken": refresh,
    }
    expires = _field(row, "expiresAt", "expires_at", "expiry")
    if expires is not None:
        stored["expiresAt"] = expires
    project_id = _text(row, "projectId", "project_id")
    if project_id:
        stored["projectId"] = project_id
    client_id = _text(row, "clientId", "client_id")
    if not client_id and isinstance(id_token, str):
        claims = _b64url_json(id_token.split(".")[1]) if id_token.count(".") >= 2 else None
        if isinstance(claims, dict):
            for key in ("azp", "aud"):
                value = claims.get(key)
                if isinstance(value, str) and value.endswith(".apps.googleusercontent.com"):
                    client_id = value
                    break
    if client_id:
        stored["clientId"] = client_id
    client_secret = _text(row, "clientSecret", "client_secret")
    if client_secret:
        stored["clientSecret"] = client_secret
    scopes = _field(row, "scopes")
    if isinstance(scopes, list) and scopes:
        stored["scopes"] = [str(item) for item in scopes if str(item).strip()]
    elif isinstance(scopes, str) and scopes.strip():
        stored["scopes"] = [item for item in scopes.split() if item]
    return stored


def _imported(stored: dict[str, Any], *, source_path: str | None) -> ImportedAccount:
    email = str(stored["email"])
    return ImportedAccount(
        account_id=f"antigravity:{email.lower()}",
        payload=stored,
        expires_at=_expires_at_text(stored.get("expiresAt")),
        label=email,
        auth_mode="oauth",
        capabilities=_ACCOUNT_CAPS,
        source_path=source_path,
    )


def parse_oauth_payload(
    payload: Any,
    *,
    filename: str,
    source_path: str | None = None,
) -> list[ImportedAccount]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
        rows = payload["accounts"]
    elif isinstance(payload, dict):
        rows = [payload]
    else:
        raise HTTPException(status_code=422, detail="Antigravity oauth JSON must be an object")
    imported: list[ImportedAccount] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        stored = _canonical_payload(row)
        if not stored:
            continue
        item = _imported(stored, source_path=source_path)
        if item.account_id in seen:
            continue
        seen.add(item.account_id)
        imported.append(item)
    if not imported:
        raise HTTPException(
            status_code=422,
            detail="Antigravity oauth JSON is missing email, accessToken, or refreshToken",
        )
    return imported


def oauth_client_id() -> str:
    return (os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID") or settings.antigravity_oauth_client_id or "").strip()


def oauth_client_secret() -> str:
    return (
        os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET") or settings.antigravity_oauth_client_secret or ""
    ).strip()


def oauth_redirect_uri(port: int | None = None) -> str:
    return f"http://localhost:{int(port or OAUTH_CALLBACK_PORT)}/oauth-callback"


def build_oauth_auth_url(state: str, *, redirect_uri: str | None = None) -> str:
    params = {
        "client_id": oauth_client_id(),
        "redirect_uri": redirect_uri or oauth_redirect_uri(),
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{OAUTH_AUTH_URL}?{urlencode(params)}"


async def _oauth_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    timeout = httpx.Timeout(30.0, connect=10.0, pool=settings.gateway_pool_timeout_seconds)

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        last: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                return await active_client.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json_body,
                    timeout=timeout,
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        assert last is not None
        raise last

    try:
        if client is None:
            async with new_client() as temporary_client:
                return await request(temporary_client)
        return await request(client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Antigravity login timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Google login endpoint") from exc


async def _email_from_userinfo(
    access_token: str, client: httpx.AsyncClient | None = None
) -> str | None:
    response = await _oauth_request(
        "GET",
        OAUTH_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        client=client,
    )
    if response.status_code >= 400:
        return None
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    email = body.get("email")
    if isinstance(email, str) and email.strip() and "@" in email:
        return email.strip()
    return None


async def _project_from_code_assist(
    access_token: str, client: httpx.AsyncClient | None = None
) -> str | None:
    try:
        response = await _oauth_request(
            "POST",
            load_code_assist_url(),
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json_body={"metadata": dict(LOAD_CODE_ASSIST_METADATA)},
            client=client,
        )
    except HTTPException:
        return None
    if response.status_code >= 400:
        return None
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    return extract_project_id(payload)


async def exchange_authorization_code(
    code: str,
    *,
    redirect_uri: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    token = (code or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="Google login did not return an authorization code")
    response = await _oauth_request(
        "POST",
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": token,
            "redirect_uri": redirect_uri,
            "client_id": oauth_client_id(),
            "client_secret": oauth_client_secret(),
        },
        client=client,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail="Google login could not exchange the authorization code")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Google login returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Google login returned invalid JSON")
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        raise HTTPException(status_code=422, detail="Google login did not return an access token")
    if not isinstance(refresh, str) or not refresh.strip():
        raise HTTPException(
            status_code=422,
            detail="Google login did not return a refresh token. Click Allow on the consent screen and retry.",
        )
    email = _email_from_id_token(body.get("id_token")) or await _email_from_userinfo(access, client)
    if not email:
        raise HTTPException(status_code=422, detail="Google login did not return an account email")
    stored: dict[str, Any] = {
        "email": email,
        "accessToken": access.strip(),
        "refreshToken": refresh.strip(),
        "clientId": oauth_client_id(),
        "scopes": list(OAUTH_SCOPES),
    }
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        stored["expiresAt"] = int(dt.datetime.now(dt.UTC).timestamp()) + int(expires_in)
    scope = body.get("scope")
    if isinstance(scope, str) and scope.strip():
        stored["scopes"] = [item for item in scope.split() if item]
    project_id = await _project_from_code_assist(access, client)
    if project_id:
        stored["projectId"] = project_id
    return stored


def _candidate_json_files(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    skip = {name.lower() for name in _SKIP_DIR_NAMES}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name.lower() not in skip]
            relative = Path(dirpath)
            try:
                depth = len(relative.relative_to(root).parts)
            except ValueError:
                depth = 0
            if depth > 4:
                dirnames[:] = []
                continue
            for name in filenames:
                if not name.lower().endswith(".json"):
                    continue
                files.append(Path(dirpath) / name)
    except OSError:
        return files
    return files


def windows_credential_payloads() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return []

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    cred = ctypes.POINTER(CREDENTIAL)()
    if not advapi32.CredReadW(WINDOWS_CREDENTIAL_TARGET, 1, 0, ctypes.byref(cred)):
        return []
    try:
        size = cred.contents.CredentialBlobSize
        blob = bytes(cred.contents.CredentialBlob[i] for i in range(size))
    finally:
        advapi32.CredFree(cred)
    text = None
    for encoding in ("utf-8", "utf-16-le"):
        try:
            candidate = blob.decode(encoding).strip("\x00").strip()
        except UnicodeDecodeError:
            continue
        if candidate.startswith("{") or candidate.startswith("["):
            text = candidate
            break
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    return []


def discover_local_accounts() -> list[ImportedAccount]:
    found: list[ImportedAccount] = []
    seen: set[str] = set()
    for payload in windows_credential_payloads():
        try:
            imported = parse_oauth_payload(
                payload,
                filename=WINDOWS_CREDENTIAL_TARGET,
                source_path=f"credman:{WINDOWS_CREDENTIAL_TARGET}",
            )
        except (HTTPException, ValueError):
            imported = []
        for item in imported:
            if item.account_id in seen:
                continue
            seen.add(item.account_id)
            found.append(item)
    for root in local_oauth_roots():
        for path in _candidate_json_files(root):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            if len(raw) > settings.gateway_max_auth_file_bytes:
                continue
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
                imported = parse_oauth_payload(
                    payload,
                    filename=path.name,
                    source_path=str(path),
                )
            except (UnicodeDecodeError, json.JSONDecodeError, HTTPException, ValueError):
                continue
            for item in imported:
                if item.account_id in seen:
                    continue
                seen.add(item.account_id)
                found.append(item)
    return found


@dataclass(frozen=True, slots=True)
class AntigravityCredentials:
    access_token: str
    refresh_token: str
    expires_at: dt.datetime | None
    email: str | None
    project_id: str | None
    client_id: str | None
    path: Path
    raw: dict[str, Any]
    host: str = DEFAULT_HOST


def generate_url(host: str | None = None) -> str:
    return f"{(host or DEFAULT_HOST).rstrip('/')}{STREAM_PATH}"


def load_code_assist_url(host: str | None = None) -> str:
    return f"{(host or DEFAULT_HOST).rstrip('/')}{LOAD_CODE_ASSIST_PATH}"


def fetch_available_models_url(host: str | None = None) -> str:
    return f"{(host or DEFAULT_HOST).rstrip('/')}{FETCH_AVAILABLE_MODELS_PATH}"


def _quota_family(model_id: str) -> str | None:
    name = (model_id or "").strip().lower()
    if not name or name.startswith(("tab_", "chat_")):
        return None
    if "flash-image" in name:
        return None
    if name.startswith("claude-"):
        return "Claude"
    if name.startswith("gpt-oss"):
        return "GPT-OSS"
    if name.startswith("gemini-"):
        return "Gemini"
    return None


def _quota_reset_unix(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())


def parse_quota_snapshot(catalog: Any) -> dict[str, Any] | None:
    """Map fetchAvailableModels quotaInfo into the admin remaining-% windows."""
    if not isinstance(catalog, dict):
        return None
    models = catalog.get("models")
    if not isinstance(models, dict) or not models:
        return None
    grouped: dict[tuple[str, str, int | None], dict[str, Any]] = {}
    for model_id, info in models.items():
        if not isinstance(model_id, str) or not isinstance(info, dict):
            continue
        family = _quota_family(model_id)
        if family is None:
            continue
        quota = info.get("quotaInfo")
        if not isinstance(quota, dict):
            continue
        fraction = quota.get("remainingFraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            continue
        remaining = round(max(0.0, min(100.0, float(fraction) * 100.0)), 1)
        resets_at = _quota_reset_unix(quota.get("resetTime"))
        key = (family, f"{remaining:.1f}", resets_at)
        grouped.setdefault(
            key,
            {
                "family": family,
                "remaining": remaining,
                "resets_at": resets_at,
            },
        )
    if not grouped:
        return None
    family_order = {"Gemini": 0, "Claude": 1, "GPT-OSS": 2}
    limits: list[dict[str, Any]] = []
    for item in sorted(
        grouped.values(),
        key=lambda row: (family_order.get(str(row["family"]), 9), str(row["family"])),
    ):
        remaining = float(item["remaining"])
        resets_at = item["resets_at"] if isinstance(item["resets_at"], int) else None
        family = str(item["family"])
        limits.append(
            {
                "limit_id": f"antigravity-{family.lower()}",
                "limit_name": family,
                "primary": {
                    "used_percent": max(0.0, 100.0 - remaining),
                    "remaining_percent": remaining,
                    "window_minutes": None,
                    "window_label": family,
                    "resets_at": resets_at,
                },
                "secondary": None,
            }
        )
    next_reset = min(
        (
            int(item["primary"]["resets_at"])
            for item in limits
            if isinstance(item["primary"].get("resets_at"), int)
        ),
        default=None,
    )
    return {
        "plan_type": "unknown",
        "limits": limits,
        "next_reset_at": next_reset,
        "reset_credits": {"available_count": 0, "credits": []},
        "quota_kind": "credits",
        "message": None,
    }


def extract_reasoning_effort(payload: dict[str, Any]) -> str:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip().lower()
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str) and effort.strip():
            return effort.strip().lower()
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort.strip():
        return effort.strip().lower()
    return ""


def thinking_level_for_effort(effort: str) -> str:
    if not effort:
        return "high"
    return _THINKING_LEVELS.get(effort, "high")


def thinking_budget_for_effort(effort: str) -> int:
    if not effort:
        return 1024
    budget = _CLAUDE_THINKING_BUDGETS.get(effort, 1024)
    return max(1024, min(budget, _CLAUDE_MAX_OUTPUT_TOKENS - 1))


def thinking_config_for(slug: str, effort: str) -> dict[str, Any]:
    if is_claude_model(slug):
        return {"thinkingBudget": thinking_budget_for_effort(effort)}
    return {"thinkingLevel": thinking_level_for_effort(effort)}


def is_high_effort(effort: str) -> bool:
    return effort in {"high", "xhigh", "x-high"}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        if block.get("thought") is True:
            continue
        block_type = str(block.get("type") or "input_text")
        if block_type not in _TEXT_BLOCK_TYPES:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("content")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _inline_image_part(block: dict[str, Any]) -> dict[str, Any] | None:
    image_url = block.get("image_url")
    url = None
    if isinstance(image_url, str):
        url = image_url
    elif isinstance(image_url, dict):
        nested = image_url.get("url")
        if isinstance(nested, str):
            url = nested
    if not isinstance(url, str):
        raw = block.get("url")
        if isinstance(raw, str):
            url = raw
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if url.startswith("data:"):
        header, separator, data = url.partition(",")
        if not separator or not data:
            return None
        mime = "image/png"
        if header.startswith("data:"):
            mime = header[5:].split(";", 1)[0].strip() or "image/png"
        return {"inlineData": {"mimeType": mime, "data": data}}
    if url.startswith("https://") or url.startswith("http://"):
        return {"fileData": {"fileUri": url}}
    return None


def build_image_cloudcode_envelope(
    *,
    project: str,
    prompt: str,
    images: list[str] | None = None,
    mask: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    text = (prompt or "").strip()
    if text:
        parts.append({"text": text})
    for url in images or []:
        if not isinstance(url, str) or not url.strip():
            continue
        image = _inline_image_part({"type": "input_image", "image_url": url.strip()})
        if image:
            parts.append(image)
    if isinstance(mask, str) and mask.strip():
        image = _inline_image_part({"type": "input_image", "image_url": mask.strip()})
        if image:
            parts.append(image)
    if not parts:
        raise HTTPException(
            status_code=400,
            detail="Antigravity image request is missing prompt or image",
        )
    slug = (model or IMAGE_MODEL).strip() or IMAGE_MODEL
    return {
        "project": project,
        "model": wire_model(slug),
        "requestType": "agent",
        "userAgent": "antigravity",
        "request": {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        },
    }


def _cloudcode_parts_from_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    if isinstance(content, dict):
        blocks = [content]
    elif isinstance(content, list):
        blocks = content
    else:
        return []
    parts: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, str):
            if block:
                parts.append({"text": block})
            continue
        if not isinstance(block, dict):
            continue
        if block.get("thought") is True:
            continue
        block_type = str(block.get("type") or "input_text")
        if block_type in _IMAGE_BLOCK_TYPES:
            image = _inline_image_part(block)
            if image:
                parts.append(image)
            continue
        if block_type not in _TEXT_BLOCK_TYPES and "text" not in block:
            continue
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("content")
        if isinstance(text, str) and text:
            parts.append({"text": text})
    return parts


def _iter_input_items(payload: dict[str, Any]) -> list[Any]:
    raw = payload.get("input")
    if isinstance(raw, str):
        return [{"type": "message", "role": "user", "content": raw}] if raw else []
    if isinstance(raw, list):
        return list(raw)
    return []


def _append_part(contents: list[dict[str, Any]], role: str, part: dict[str, Any]) -> None:
    if not part:
        return
    if contents and contents[-1].get("role") == role:
        parts = contents[-1].setdefault("parts", [])
        if isinstance(parts, list):
            parts.append(part)
            return
    contents.append({"role": role, "parts": [part]})


def _append_content(contents: list[dict[str, Any]], role: str, text: str) -> None:
    if not text:
        return
    _append_part(contents, role, {"text": text})


def _call_id_of(item: dict[str, Any]) -> str | None:
    for key in ("call_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _call_name_of(item: dict[str, Any]) -> str | None:
    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    item_type = str(item.get("type") or "")
    if item_type in {"apply_patch_call", "apply_patch_call_output"}:
        return "apply_patch"
    return None


def _index_function_names(items: list[Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in _CALL_INPUT_TYPES:
            continue
        call_id = _call_id_of(item)
        name = _call_name_of(item)
        if call_id and name:
            index[call_id] = name
    return index


def _coerce_schema_type(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "null":
            return None
        return _SCHEMA_TYPE_ALIASES.get(text.lower(), text)
    if isinstance(value, list):
        for item in value:
            coerced = _coerce_schema_type(item)
            if coerced:
                return coerced
    return None


def _resolve_schema_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return None
    path = [part for part in ref[2:].split("/") if part]
    if len(path) >= 2 and path[0] in {"definitions", "$defs"}:
        node = defs.get(path[1])
        return dict(node) if isinstance(node, dict) else None
    return None


def clean_json_schema(
    schema: Any,
    *,
    defs: dict[str, Any] | None = None,
    _depth: int = 0,
    _seen_refs: frozenset[str] | None = None,
    _seen_ids: frozenset[int] | None = None,
) -> Any:
    """Coerce JSON Schema so every `type` is a string Cloud Code accepts."""
    if _depth > 24:
        return {"type": "object"}
    if schema is True:
        return {"type": "object"}
    if schema is False or schema is None:
        return None
    if not isinstance(schema, dict):
        return None
    seen_ids = _seen_ids or frozenset()
    ident = id(schema)
    if ident in seen_ids:
        return {"type": "object"}
    seen_ids = seen_ids | {ident}
    local_defs = dict(defs or {})
    for key in ("$defs", "definitions"):
        extra = schema.get(key)
        if isinstance(extra, dict):
            local_defs.update(extra)
    ref = schema.get("$ref")
    working = dict(schema)
    seen_refs = _seen_refs or frozenset()
    if isinstance(ref, str) and ref:
        if ref in seen_refs:
            return {"type": "object"}
        seen_refs = seen_refs | {ref}
        resolved = _resolve_schema_ref(ref, local_defs)
        if isinstance(resolved, dict):
            merged = dict(resolved)
            for key, value in working.items():
                if key not in {"$ref", "$defs", "definitions"}:
                    merged.setdefault(key, value)
            working = merged
    out: dict[str, Any] = {}
    for key, value in working.items():
        if key in {"$ref", "$defs", "definitions", "$schema", "$id"}:
            continue
        out[key] = value
    coerced = _coerce_schema_type(out.get("type"))
    if coerced:
        out["type"] = coerced
    elif "type" in out:
        if isinstance(out.get("properties"), dict):
            out["type"] = "object"
        elif "items" in out:
            out["type"] = "array"
        else:
            out.pop("type", None)

    def _clean_child(node: Any) -> Any:
        return clean_json_schema(
            node,
            defs=local_defs,
            _depth=_depth + 1,
            _seen_refs=seen_refs,
            _seen_ids=seen_ids,
        )

    properties = out.get("properties")
    if isinstance(properties, dict):
        cleaned_props: dict[str, Any] = {}
        for name, prop in properties.items():
            cleaned = _clean_child(prop)
            if isinstance(cleaned, dict):
                cleaned_props[str(name)] = cleaned
        out["properties"] = cleaned_props
    if "items" in out:
        cleaned_items = _clean_child(out["items"])
        if isinstance(cleaned_items, dict):
            out["items"] = cleaned_items
        else:
            out.pop("items", None)
    for union_key in ("anyOf", "oneOf", "allOf"):
        options = out.get(union_key)
        if not isinstance(options, list):
            continue
        cleaned_opts = [item for item in (_clean_child(option) for option in options) if isinstance(item, dict)]
        if cleaned_opts:
            out[union_key] = cleaned_opts
        else:
            out.pop(union_key, None)
    extra = out.get("additionalProperties")
    if extra is True:
        out.pop("additionalProperties", None)
    elif isinstance(extra, dict):
        cleaned_extra = _clean_child(extra)
        if isinstance(cleaned_extra, dict):
            out["additionalProperties"] = cleaned_extra
        else:
            out.pop("additionalProperties", None)
    return out


def clean_claude_json_schema(schema: Any) -> dict[str, Any]:
    """Claude/Vertex rejects integer, null unions, tuples, and object schemas without required."""
    cleaned = clean_json_schema(schema)
    shaped = _shape_claude_schema(cleaned)
    if isinstance(shaped, dict):
        return shaped
    return {"type": "object", "properties": {}, "required": []}


def _is_nullish_claude_schema(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null":
        return True
    if schema.get("anyOf") or schema.get("enum") or schema.get("items"):
        return False
    if schema.get("type") == "object" and not schema.get("properties"):
        return not schema.get("description")
    return not schema.get("type") and not schema.get("properties")


def _merge_claude_branch(out: dict[str, Any], branch: dict[str, Any]) -> None:
    for key, value in branch.items():
        if key == "description" and out.get("description"):
            continue
        out[key] = value


def _merge_property_specs(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_enum = left.get("enum") if isinstance(left.get("enum"), list) else None
    right_enum = right.get("enum") if isinstance(right.get("enum"), list) else None
    if left_enum and right_enum:
        merged = dict(left)
        merged["enum"] = list(dict.fromkeys([*left_enum, *right_enum]))
        return merged
    if left.get("type") == "object" and right.get("type") == "object":
        return _merge_claude_objects([left, right])
    return left


def _merge_claude_objects(objects: list[dict[str, Any]]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required_sets: list[set[str]] = []
    additional_false = True
    description = None
    for obj in objects:
        if description is None and isinstance(obj.get("description"), str) and obj["description"].strip():
            description = obj["description"].strip()
        raw_props = obj.get("properties")
        if isinstance(raw_props, dict):
            for name, spec in raw_props.items():
                if not isinstance(spec, dict):
                    continue
                key = str(name)
                if key in props:
                    props[key] = _merge_property_specs(props[key], spec)
                else:
                    props[key] = spec
            if isinstance(obj.get("required"), list):
                required_sets.append({str(item) for item in obj["required"] if str(item) in raw_props})
        if obj.get("additionalProperties") is not False:
            additional_false = False
    out: dict[str, Any] = {"type": "object", "properties": props, "required": []}
    if required_sets:
        shared = set.intersection(*required_sets)
        out["required"] = [name for name in props if name in shared]
    if additional_false:
        out["additionalProperties"] = False
    if description:
        out["description"] = description
    return out


def _collapse_claude_union(options: list[dict[str, Any]]) -> dict[str, Any]:
    if not options:
        return {"type": "object", "properties": {}, "required": []}
    if len(options) == 1:
        return options[0]
    objects = [
        item
        for item in options
        if item.get("type") == "object" or isinstance(item.get("properties"), dict)
    ]
    if objects and len(objects) == len(options):
        return _merge_claude_objects(objects)
    return options[0]


def _shape_claude_schema(schema: Any) -> dict[str, Any] | None:
    if not isinstance(schema, dict):
        return None
    type_name = schema.get("type")
    if type_name == "null":
        return None
    out: dict[str, Any] = {}
    if type_name == "integer":
        type_name = "number"
    if isinstance(type_name, str) and type_name:
        out["type"] = type_name
    description = schema.get("description")
    if isinstance(description, str) and description.strip():
        out["description"] = description.strip()
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        out["enum"] = enum_values
    properties = schema.get("properties")
    if isinstance(properties, dict):
        out["type"] = "object"
        cleaned_props: dict[str, Any] = {}
        for name, prop in properties.items():
            shaped = _shape_claude_schema(prop)
            if isinstance(shaped, dict) and not _is_nullish_claude_schema(shaped):
                cleaned_props[str(name)] = shaped
        out["properties"] = cleaned_props
        required = schema.get("required")
        if isinstance(required, list):
            out["required"] = [str(item) for item in required if str(item) in cleaned_props]
        else:
            out["required"] = []
    items = schema.get("items")
    prefix = schema.get("prefixItems")
    if not isinstance(items, dict) and isinstance(prefix, list) and prefix:
        first = prefix[0]
        items = first if isinstance(first, dict) else {"type": "string"}
    if items is not None:
        out["type"] = out.get("type") or "array"
        shaped_items = _shape_claude_schema(items)
        out["items"] = shaped_items if isinstance(shaped_items, dict) else {"type": "string"}
    extra = schema.get("additionalProperties")
    if extra is False:
        out["additionalProperties"] = False
    for key in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        value = schema.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern:
        out["pattern"] = pattern
    if "properties" not in out:
        for union_key in ("anyOf", "oneOf"):
            options = schema.get(union_key)
            if not isinstance(options, list):
                continue
            shaped_any = [
                item
                for item in (_shape_claude_schema(option) for option in options)
                if isinstance(item, dict) and not _is_nullish_claude_schema(item)
            ]
            if len(shaped_any) == 1:
                _merge_claude_branch(out, shaped_any[0])
                break
            if len(shaped_any) > 1:
                _merge_claude_branch(out, _collapse_claude_union(shaped_any))
                break
    if "type" not in out and "anyOf" not in out:
        if not out.get("properties") and not out.get("enum") and not out.get("items"):
            return None
        out["type"] = "object"
        out.setdefault("properties", {})
        out.setdefault("required", [])
    if out.get("type") == "object":
        out.setdefault("properties", {})
        out.setdefault("required", [])
    if out.get("type") == "null":
        return None
    return out


def _parameters_empty(params: Any) -> bool:
    if not isinstance(params, dict):
        return True
    properties = params.get("properties")
    if isinstance(properties, dict) and properties:
        return False
    if params.get("anyOf") or params.get("oneOf") or params.get("allOf"):
        return False
    return True


def _claude_tool_name(name: str, used: set[str]) -> str:
    cleaned = _CLAUDE_TOOL_NAME_RE.sub("_", name).strip("_")
    if not cleaned:
        cleaned = "tool"
    if cleaned[0].isdigit():
        cleaned = f"tool_{cleaned}"
    cleaned = cleaned[:64]
    base = cleaned
    index = 2
    while cleaned in used:
        suffix = f"_{index}"
        cleaned = f"{base[: max(1, 64 - len(suffix))]}{suffix}"
        index += 1
    used.add(cleaned)
    return cleaned


def _rewrite_cloudcode_tool_names(contents: list[dict[str, Any]], orig_to_wire: dict[str, str]) -> None:
    if not orig_to_wire:
        return
    for content in contents:
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            for key in ("functionCall", "functionResponse"):
                blob = part.get(key)
                if isinstance(blob, dict):
                    original = blob.get("name")
                    if isinstance(original, str) and original in orig_to_wire:
                        blob["name"] = orig_to_wire[original]


def _tool_name(tool: dict[str, Any]) -> str | None:
    function = tool.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    name = tool.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    tool_type = str(tool.get("type") or "")
    if tool_type in _NAMED_TYPE_TOOLS:
        return tool_type
    return None


def _is_hosted_tool(tool: dict[str, Any], name: str | None) -> bool:
    tool_type = str(tool.get("type") or "")
    return tool_type in _HOSTED_TOOL_TYPES or name in _HOSTED_TOOL_TYPES


def _iter_raw_tools(payload: dict[str, Any]) -> list[Any]:
    tools: list[Any] = []
    raw = payload.get("tools")
    if isinstance(raw, list):
        tools.extend(raw)
    for item in _iter_input_items(payload):
        if not isinstance(item, dict) or str(item.get("type") or "") != "additional_tools":
            continue
        extra = item.get("tools")
        if isinstance(extra, list):
            tools.extend(extra)
        nested = item.get("additional_tools")
        if isinstance(nested, list):
            tools.extend(nested)
    return tools


def _expand_tools(raw_tools: list[Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    queue = list(raw_tools)
    while queue:
        tool = queue.pop(0)
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "") == "namespace":
            inner = tool.get("tools")
            if isinstance(inner, list):
                queue[0:0] = [child for child in inner if isinstance(child, dict)]
            continue
        expanded.append(tool)
    return expanded


def _map_function_declaration(tool: dict[str, Any], *, claude: bool = False) -> dict[str, Any] | None:
    item = dict(tool)
    tool_type = str(item.get("type") or "function")
    if tool_type in {"tool_search", "namespace"}:
        return None
    function = item.get("function")
    if isinstance(function, dict):
        for field in ("name", "description", "parameters", "input_schema", "inputSchema"):
            if field in function and field not in item:
                item[field] = function[field]
    name = _tool_name(item)
    if not name or _is_hosted_tool(item, name):
        return None
    description = item.get("description")
    parameters = item.get("parameters")
    if parameters is None:
        parameters = item.get("input_schema")
    if parameters is None:
        parameters = item.get("inputSchema")
    empty = _parameters_empty(parameters)
    if empty and (tool_type in _FREEFORM_TOOL_TYPES or name in _PATCH_TOOL_NAMES):
        parameters = dict(_APPLY_PATCH_PARAMETERS)
        if not (isinstance(description, str) and description.strip()):
            description = "Apply a file patch to the workspace."
    elif empty:
        parameters = {"type": "object", "properties": {}}
    try:
        cleaned = clean_json_schema(parameters)
        if claude:
            cleaned = clean_claude_json_schema(cleaned)
    except RecursionError:
        cleaned = {"type": "object", "properties": {}, "required": []}
    if not isinstance(cleaned, dict):
        cleaned = {"type": "object", "properties": {}, "required": []}
    if not isinstance(cleaned.get("type"), str):
        cleaned["type"] = "object"
    if claude and cleaned.get("type") != "object":
        cleaned = {"type": "object", "properties": {}, "required": []}
    if claude:
        cleaned.setdefault("properties", {})
        cleaned.setdefault("required", [])
    declaration: dict[str, Any] = {"name": name, "parameters": cleaned}
    if isinstance(description, str) and description.strip():
        declaration["description"] = description.strip()
    return declaration


def function_declarations(payload: dict[str, Any], *, claude: bool = False) -> list[dict[str, Any]]:
    declarations: list[dict[str, Any]] = []
    seen: set[str] = set()
    used_wire: set[str] = set()
    for tool in _expand_tools(_iter_raw_tools(payload)):
        mapped = _map_function_declaration(tool, claude=claude)
        if mapped is None:
            continue
        original = str(mapped["name"])
        if original in seen:
            continue
        seen.add(original)
        if claude:
            wire = _claude_tool_name(original, used_wire)
            mapped["name"] = wire
            if wire != original:
                mapped["_original_name"] = original
        declarations.append(mapped)
    return declarations


def _thought_signature_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (bytes, bytearray)) and value:
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        for key in ("thoughtSignature", "thought_signature", "bytesBase64Encoded", "data"):
            nested = value.get(key)
            if nested is value:
                continue
            found = _thought_signature_value(nested)
            if found:
                return found
        return None
    if isinstance(value, list) and value and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        return base64.b64encode(bytes(value)).decode("ascii")
    return None


def _extract_thought_signature(*sources: Any) -> str | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("thoughtSignature", "thought_signature"):
            found = _thought_signature_value(source.get(key))
            if found:
                return found
        nested = source.get("functionCall")
        if not isinstance(nested, dict):
            nested = source.get("function_call")
        if isinstance(nested, dict):
            for key in ("thoughtSignature", "thought_signature"):
                found = _thought_signature_value(nested.get(key))
                if found:
                    return found
    return None


def remember_thought_signature(call_id: str | None, signature: str | None) -> None:
    if not isinstance(call_id, str) or not call_id.strip():
        return
    if not isinstance(signature, str) or not signature.strip():
        return
    key = call_id.strip()
    value = signature.strip()
    with _thought_signature_lock:
        _thought_signature_by_call_id[key] = value
        _thought_signature_by_call_id.move_to_end(key)
        while len(_thought_signature_by_call_id) > _THOUGHT_SIGNATURE_CACHE_MAX:
            _thought_signature_by_call_id.popitem(last=False)


def lookup_thought_signature(call_id: str | None) -> str | None:
    if not isinstance(call_id, str) or not call_id.strip():
        return None
    key = call_id.strip()
    with _thought_signature_lock:
        value = _thought_signature_by_call_id.get(key)
        if value is None:
            return None
        _thought_signature_by_call_id.move_to_end(key)
        return value


def clear_thought_signature_cache() -> None:
    with _thought_signature_lock:
        _thought_signature_by_call_id.clear()


def _function_call_args(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get("args")
    if isinstance(raw, dict):
        return raw
    raw = item.get("arguments")
    if isinstance(raw, dict):
        return raw
    if raw is None:
        raw = item.get("input")
    if raw is None:
        raw = item.get("patch") if item.get("patch") is not None else item.get("diff")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return {}
        if stripped[0] in "{[":
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return {"input": raw}
    if raw is None:
        return {}
    return {"input": raw}


def _function_call_part(item: dict[str, Any]) -> dict[str, Any] | None:
    name = _call_name_of(item)
    if not name:
        return None
    call_id = _call_id_of(item)
    item_id = item.get("id")
    item_id = item_id.strip() if isinstance(item_id, str) and item_id.strip() else None
    signature = _extract_thought_signature(item)
    if signature is None:
        signature = lookup_thought_signature(call_id)
    if signature is None and item_id and item_id != call_id:
        signature = lookup_thought_signature(item_id)
    if signature is not None:
        if call_id:
            remember_thought_signature(call_id, signature)
        if item_id:
            remember_thought_signature(item_id, signature)
    call: dict[str, Any] = {"name": name, "args": _function_call_args(item)}
    if call_id:
        # Claude/Vertex maps functionCall.id -> tool_use.id and rejects a missing field.
        call["id"] = call_id
    part: dict[str, Any] = {"functionCall": call}
    if signature:
        # Cloud Code / Gemini REST expects the signature on the part (camelCase).
        part["thoughtSignature"] = signature
    return part


def _function_response_object(output: Any) -> dict[str, Any] | None:
    if output is None:
        return None
    if isinstance(output, dict):
        return output or None
    if isinstance(output, str):
        stripped = output.strip()
        if not stripped:
            return None
        if stripped[0] in "{[":
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and parsed:
                return parsed
        return {"content": output}
    if isinstance(output, list):
        parts: list[str] = []
        for block in output:
            if isinstance(block, str) and block:
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str):
                text = block.get("content")
            if isinstance(text, str) and text:
                parts.append(text)
        if not parts:
            return None
        return {"content": "\n".join(parts)}
    return {"content": str(output)}


def _is_image_block(block: dict[str, Any]) -> bool:
    block_type = str(block.get("type") or "")
    return block_type in _IMAGE_BLOCK_TYPES or "image_url" in block


def _function_response_part(item: dict[str, Any], names: dict[str, str]) -> dict[str, Any] | None:
    parts = _function_output_cloud_parts(item, names)
    for part in parts:
        if isinstance(part, dict) and "functionResponse" in part:
            return part
    return None


def _function_output_cloud_parts(item: dict[str, Any], names: dict[str, str]) -> list[dict[str, Any]]:
    call_id = _call_id_of(item)
    indexed = names.get(call_id) if call_id else None
    name = indexed or _call_name_of(item)
    if not name or (call_id and name == call_id and indexed is None):
        return []
    output = item.get("output")
    images: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    if isinstance(output, dict) and _is_image_block(output):
        image = _inline_image_part(output)
        if image:
            images.append(image)
        response = {"content": "ok"}
    elif isinstance(output, list):
        texts: list[str] = []
        for block in output:
            if isinstance(block, str) and block:
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if _is_image_block(block):
                image = _inline_image_part(block)
                if image:
                    images.append(image)
                continue
            text = block.get("text")
            if not isinstance(text, str):
                text = block.get("content")
            if isinstance(text, str) and text:
                texts.append(text)
        response = {"content": "\n".join(texts)} if texts else {"content": "ok"}
    else:
        response = _function_response_object(output)
        if not isinstance(response, dict) or not response:
            response = {"content": "ok"}
    function_response: dict[str, Any] = {"name": name, "response": response}
    if call_id:
        function_response["id"] = call_id
    parts: list[dict[str, Any]] = [{"functionResponse": function_response}]
    parts.extend(images)
    return parts


def _ensure_contents_end_with_user(contents: list[dict[str, Any]]) -> None:
    if not contents:
        return
    last = contents[-1]
    if last.get("role") != "model":
        return
    pending: list[tuple[str, str | None]] = []
    for part in last.get("parts") or []:
        if not isinstance(part, dict):
            continue
        call = part.get("functionCall")
        if isinstance(call, dict):
            name = call.get("name")
            if isinstance(name, str) and name.strip():
                call_id = call.get("id")
                pending.append(
                    (
                        name.strip(),
                        call_id.strip() if isinstance(call_id, str) and call_id.strip() else None,
                    )
                )
    if pending:
        for name, call_id in pending:
            response: dict[str, Any] = {"name": name, "response": {"content": "ok"}}
            if call_id:
                response["id"] = call_id
            _append_part(contents, "user", {"functionResponse": response})
        return
    _append_part(contents, "user", {"text": "Continue."})


def _function_call_arguments_json(args: Any) -> str:
    if isinstance(args, dict):
        return json.dumps(args, ensure_ascii=False)
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return "{}"
        if stripped[0] in "{[":
            try:
                parsed = json.loads(args)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False)
        return json.dumps({"input": args}, ensure_ascii=False)
    if args is None:
        return "{}"
    return json.dumps({"input": args}, ensure_ascii=False)


def system_instruction_text(envelope: dict[str, Any]) -> str:
    request = envelope.get("request")
    if not isinstance(request, dict):
        return ""
    instruction = request.get("systemInstruction")
    if not isinstance(instruction, dict):
        return ""
    return _content_text(instruction.get("parts"))


def responses_to_cloudcode(payload: dict[str, Any], *, project: str) -> dict[str, Any]:
    effort = extract_reasoning_effort(payload)
    model_slug = str(payload.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    wire = wire_model(model_slug, high_effort=is_high_effort(effort))
    system_chunks: list[str] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_chunks.append(instructions.strip())
    items = _iter_input_items(payload)
    call_names = _index_function_names(items)
    contents: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            _append_content(contents, "user", item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "message")
        if item_type in _CALL_INPUT_TYPES:
            part = _function_call_part(item)
            if part:
                _append_part(contents, "model", part)
            continue
        if item_type in _OUTPUT_INPUT_TYPES:
            for part in _function_output_cloud_parts(item, call_names):
                _append_part(contents, "user", part)
            continue
        if item_type in _SKIP_INPUT_TYPES:
            continue
        role = str(item.get("role") or "user").strip().lower()
        source = item.get("content") if "content" in item else item.get("text")
        if role in {"system", "developer"}:
            text = _content_text(source)
            if text.strip():
                system_chunks.append(text.strip())
            continue
        cloud_role = "model" if role in {"assistant", "model"} else "user"
        for part in _cloudcode_parts_from_content(source):
            _append_part(contents, cloud_role, part)
    _ensure_contents_end_with_user(contents)
    if not contents:
        raise HTTPException(status_code=400, detail="Antigravity request is missing user content")
    system_text = "\n\n".join(chunk for chunk in system_chunks if chunk)
    if len(system_text) > MAX_SYSTEM_INSTRUCTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail="Antigravity system instructions exceed the supported size",
        )
    think = thinking_config_for(model_slug, effort)
    generation: dict[str, Any] = {"thinkingConfig": think}
    if is_claude_model(model_slug):
        budget = int(think.get("thinkingBudget") or 1024)
        generation["maxOutputTokens"] = max(_CLAUDE_MAX_OUTPUT_TOKENS, budget + 1)
    request: dict[str, Any] = {
        "contents": contents,
        "generationConfig": generation,
    }
    if system_text:
        request["systemInstruction"] = {"role": "user", "parts": [{"text": system_text}]}
    declarations = function_declarations(payload, claude=is_claude_model(model_slug))
    orig_to_wire: dict[str, str] = {}
    wire_to_orig: dict[str, str] = {}
    for declaration in declarations:
        original = declaration.pop("_original_name", None)
        if isinstance(original, str) and original:
            orig_to_wire[original] = str(declaration["name"])
            wire_to_orig[str(declaration["name"])] = original
    _rewrite_cloudcode_tool_names(contents, orig_to_wire)
    if declarations:
        request["tools"] = [{"functionDeclarations": declarations}]
    envelope = {
        "project": project,
        "model": wire,
        "requestType": "agent",
        "userAgent": "antigravity",
        "request": request,
    }
    if wire_to_orig:
        envelope["_ts_claude_names"] = wire_to_orig
    return envelope


def parse_cloudcode_stream_bytes(raw: bytes) -> list[dict[str, Any]]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    if text[0] in "[{":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    payloads: list[dict[str, Any]] = []
    buffer = text.replace("\r\n", "\n")
    for block in buffer.split("\n\n"):
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            continue
        data = "\n".join(data_lines)
        if not data or data == "[DONE]":
            continue
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payloads.append(parsed)
    return payloads


def extract_image_b64_from_cloudcode(raw: bytes | dict[str, Any] | list[Any]) -> str:
    if isinstance(raw, bytes):
        payloads = parse_cloudcode_stream_bytes(raw)
    elif isinstance(raw, list):
        payloads = [item for item in raw if isinstance(item, dict)]
    elif isinstance(raw, dict):
        payloads = [raw]
    else:
        payloads = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        inner = _unwrap_cloudcode(payload)
        candidates = inner.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                blob = part.get("inlineData") or part.get("inline_data")
                if not isinstance(blob, dict):
                    continue
                data = blob.get("data")
                if isinstance(data, str) and data.strip():
                    return data.strip()
    raise HTTPException(
        status_code=502,
        detail="Antigravity image endpoint returned no image data",
    )


def _unwrap_cloudcode(payload: dict[str, Any]) -> dict[str, Any]:
    inner = payload.get("response")
    return inner if isinstance(inner, dict) else payload


def _usage_from_metadata(metadata: Any) -> dict[str, int]:
    if not isinstance(metadata, dict):
        return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    prompt = int(metadata.get("promptTokenCount") or 0)
    completion = int(metadata.get("candidatesTokenCount") or 0)
    total = int(metadata.get("totalTokenCount") or prompt + completion)
    return {"input_tokens": prompt, "output_tokens": completion, "total_tokens": total}


def _call_has_thought_signature(call: dict[str, Any]) -> bool:
    return bool(call.get("thought_signature") or call.get("thoughtSignature"))


def _apply_thought_signature(call: dict[str, Any], signature: str) -> None:
    call["thought_signature"] = signature
    call["thoughtSignature"] = signature
    remember_thought_signature(str(call.get("call_id") or ""), signature)


def _merge_extracted_function_call(existing: list[dict[str, Any]], call: dict[str, Any]) -> None:
    cid = call.get("call_id")
    if not isinstance(cid, str) or not cid:
        existing.append(call)
        return
    for index, prev in enumerate(existing):
        if prev.get("call_id") != cid:
            continue
        if _call_has_thought_signature(call) or not _call_has_thought_signature(prev):
            existing[index] = call
        return
    existing.append(call)


def _extract_function_call(part: dict[str, Any]) -> dict[str, Any] | None:
    raw = part.get("functionCall")
    if not isinstance(raw, dict):
        raw = part.get("function_call")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    call_id = raw.get("id")
    if not isinstance(call_id, str) or not call_id.strip():
        call_id = f"call_{uuid.uuid4().hex[:12]}"
    call_id = call_id.strip()
    signature = _extract_thought_signature(part, raw)
    if signature:
        remember_thought_signature(call_id, signature)
    out: dict[str, Any] = {
        "name": name.strip(),
        "call_id": call_id,
        "arguments": _function_call_arguments_json(raw.get("args") if "args" in raw else raw.get("arguments")),
    }
    if signature:
        out["thought_signature"] = signature
        out["thoughtSignature"] = signature
    return out


def cloudcode_payloads_to_codex_sse(
    payloads: list[dict[str, Any]],
    *,
    model: str,
    response_id: str | None = None,
    name_map: dict[str, str] | None = None,
) -> bytes:
    response_id = response_id or f"resp_ag_{uuid.uuid4().hex[:12]}"
    visible: list[str] = []
    reasoning: list[str] = []
    function_calls: list[dict[str, Any]] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    blocked = False
    pending_sig: str | None = None
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        inner = _unwrap_cloudcode(payload)
        feedback = inner.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            blocked = True
        metadata = inner.get("usageMetadata")
        if isinstance(metadata, dict):
            usage = _usage_from_metadata(metadata)
        candidates = inner.get("candidates")
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content")
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                part_sig = _extract_thought_signature(part)
                if part_sig:
                    pending_sig = part_sig
                call = _extract_function_call(part)
                if call is not None:
                    if name_map:
                        call["name"] = name_map.get(call["name"], call["name"])
                    if not _call_has_thought_signature(call) and pending_sig:
                        _apply_thought_signature(call, pending_sig)
                    pending_sig = None
                    _merge_extracted_function_call(function_calls, call)
                    continue
                text = part.get("text")
                if not isinstance(text, str) or not text:
                    continue
                if part.get("thought") is True:
                    reasoning.append(text)
                else:
                    visible.append(text)
    if pending_sig and function_calls and not _call_has_thought_signature(function_calls[0]):
        _apply_thought_signature(function_calls[0], pending_sig)
    visible_text = "".join(visible)
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "response.created",
            {
                "type": "response.created",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": int(dt.datetime.now(dt.UTC).timestamp()),
                    "status": "in_progress",
                    "model": model,
                    "output": [],
                },
            },
        )
    ]
    if reasoning:
        events.append(
            (
                "response.reasoning_summary_text.delta",
                {
                    "type": "response.reasoning_summary_text.delta",
                    "delta": "".join(reasoning),
                },
            )
        )
    if blocked:
        events.append(
            (
                "response.refusal.delta",
                {"type": "response.refusal.delta", "delta": "The request was blocked."},
            )
        )
        function_calls = []
    output: list[dict[str, Any]] = []
    if blocked:
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        output.append(
            {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "status": "incomplete",
                "content": [{"type": "refusal", "refusal": "The request was blocked."}],
            }
        )
    elif visible_text:
        msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        message = {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": visible_text}],
        }
        events.extend(
            [
                (
                    "response.output_item.added",
                    {
                        "type": "response.output_item.added",
                        "item": {
                            "id": msg_id,
                            "type": "message",
                            "role": "assistant",
                            "status": "in_progress",
                            "content": [],
                        },
                    },
                ),
                (
                    "response.content_part.added",
                    {
                        "type": "response.content_part.added",
                        "item_id": msg_id,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": ""},
                    },
                ),
                (
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": msg_id,
                        "content_index": 0,
                        "delta": visible_text,
                    },
                ),
                (
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": msg_id,
                        "content_index": 0,
                        "text": visible_text,
                    },
                ),
                (
                    "response.content_part.done",
                    {
                        "type": "response.content_part.done",
                        "item_id": msg_id,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": visible_text},
                    },
                ),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "item": message},
                ),
            ]
        )
        output.append(message)
    for call in function_calls:
        item_id = f"fc_{uuid.uuid4().hex[:12]}"
        item = {
            "id": item_id,
            "type": "function_call",
            "status": "completed",
            "name": call["name"],
            "call_id": call["call_id"],
            "arguments": call["arguments"],
        }
        signature = call.get("thought_signature") or call.get("thoughtSignature")
        if isinstance(signature, str) and signature.strip():
            item["thought_signature"] = signature.strip()
            item["thoughtSignature"] = signature.strip()
            remember_thought_signature(str(call["call_id"]), signature.strip())
            remember_thought_signature(item_id, signature.strip())
        added = dict(item)
        added["status"] = "in_progress"
        added["arguments"] = ""
        events.extend(
            [
                (
                    "response.output_item.added",
                    {"type": "response.output_item.added", "item": added},
                ),
                (
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item_id,
                        "call_id": call["call_id"],
                        "delta": call["arguments"],
                    },
                ),
                (
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": item_id,
                        "call_id": call["call_id"],
                        "arguments": call["arguments"],
                    },
                ),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "item": item},
                ),
            ]
        )
        output.append(item)
    events.append(
        (
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": int(dt.datetime.now(dt.UTC).timestamp()),
                    "status": "incomplete" if blocked else "completed",
                    "model": model,
                    "output": output,
                    "output_text": visible_text,
                    "usage": usage,
                },
            },
        )
    )
    return "".join(
        f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n" for name, data in events
    ).encode()


def cloudcode_bytes_to_codex_sse(
    raw: bytes,
    *,
    model: str,
    response_id: str | None = None,
    name_map: dict[str, str] | None = None,
) -> bytes:
    return cloudcode_payloads_to_codex_sse(
        parse_cloudcode_stream_bytes(raw),
        model=model,
        response_id=response_id,
        name_map=name_map,
    )


def google_error_payload(body: Any) -> dict[str, Any] | None:
    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        return error
    if isinstance(body.get("status"), str) or isinstance(body.get("message"), str):
        return body
    return None


def google_error_status(body: Any) -> str:
    error = google_error_payload(body)
    if not error:
        return ""
    status = error.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return ""


def google_error_message(body: Any) -> str:
    error = google_error_payload(body)
    if not error:
        return ""
    message = error.get("message")
    return message.strip() if isinstance(message, str) else ""


def is_google_capacity_error(status: int, body: Any) -> bool:
    if status == 503:
        return True
    if google_error_status(body) == "UNAVAILABLE":
        return True
    message = google_error_message(body).lower()
    return "no capacity available" in message


def is_huge_system_instruction(envelope: dict[str, Any]) -> bool:
    return len(system_instruction_text(envelope)) > HUGE_SYSTEM_INSTRUCTION_CHARS


def extract_project_id(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if not isinstance(payload, dict):
        return None
    for key in ("cloudaicompanionProject", "projectId", "project"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("id") or value.get("projectId")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _persist(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _parse_expires_at(value: Any) -> dt.datetime | None:
    text = _expires_at_text(value)
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def load_credentials(payload: dict[str, Any], *, path: Path) -> AntigravityCredentials:
    stored = _canonical_payload(payload) or payload
    access = _text(stored, "accessToken", "access_token")
    refresh = _text(stored, "refreshToken", "refresh_token")
    if not access or not refresh:
        raise HTTPException(status_code=503, detail="Antigravity credential file is missing tokens")
    return AntigravityCredentials(
        access_token=access,
        refresh_token=refresh,
        expires_at=_parse_expires_at(_field(stored, "expiresAt", "expires_at")),
        email=_text(stored, "email"),
        project_id=_text(stored, "projectId", "project_id"),
        client_id=_text(stored, "clientId", "client_id"),
        path=path,
        raw=payload,
        host=DEFAULT_HOST,
    )


def should_refresh(credentials: AntigravityCredentials, *, now: dt.datetime | None = None) -> bool:
    if not credentials.refresh_token:
        return False
    current = now or dt.datetime.now(dt.UTC)
    if credentials.expires_at is None:
        return True
    return credentials.expires_at - current <= _REFRESH_WINDOW


def build_headers(
    credentials: AntigravityCredentials,
    *,
    accept: str = "application/json",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.access_token}",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": accept,
    }


def persist_credential_fields(
    credentials: AntigravityCredentials, updates: dict[str, Any]
) -> AntigravityCredentials:
    raw = dict(credentials.raw)
    raw.update(updates)
    _persist(Path(credentials.path), raw)
    return load_credentials(raw, path=Path(credentials.path))


async def refresh_credentials(
    credentials: AntigravityCredentials, client: httpx.AsyncClient | None = None
) -> AntigravityCredentials:
    if not credentials.refresh_token:
        raise HTTPException(status_code=401, detail="Antigravity authentication expired")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": credentials.refresh_token,
    }
    client_id = credentials.client_id or oauth_client_id()
    secret = _text(credentials.raw, "clientSecret", "client_secret") or oauth_client_secret()
    if client_id:
        data["client_id"] = client_id
    if secret:
        data["client_secret"] = secret

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        last: httpx.HTTPError | None = None
        for attempt in range(3):
            try:
                return await active_client.post(
                    TOKEN_URL,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data=data,
                    timeout=httpx.Timeout(
                        30.0,
                        connect=10.0,
                        pool=settings.gateway_pool_timeout_seconds,
                    ),
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = exc
                await asyncio.sleep(0.4 * (attempt + 1))
        assert last is not None
        raise last

    try:
        if client is None:
            async with new_client() as temporary_client:
                response = await request(temporary_client)
        else:
            response = await request(client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Antigravity token refresh timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Antigravity token endpoint") from exc
    if response.status_code >= 400:
        try:
            error = response.json()
        except json.JSONDecodeError:
            error = {}
        code = error.get("error") if isinstance(error, dict) else None
        if response.status_code == 401 or code in {"invalid_grant", "unauthorized_client"}:
            raise HTTPException(status_code=401, detail="Antigravity authentication expired")
        raise HTTPException(status_code=502, detail="Cannot reach Antigravity token endpoint")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Antigravity token refresh returned invalid JSON") from exc
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise HTTPException(status_code=502, detail="Antigravity token refresh returned no access token")
    updates: dict[str, Any] = {"accessToken": access}
    if isinstance(body.get("refresh_token"), str) and body["refresh_token"]:
        updates["refreshToken"] = body["refresh_token"]
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        updates["expiresAt"] = int(dt.datetime.now(dt.UTC).timestamp()) + int(expires_in)
    return persist_credential_fields(credentials, updates)


class AntigravityAdapter:
    id = "antigravity"
    display_name = "Antigravity"
    capabilities = {CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT}
    ready = True
    default_host = DEFAULT_HOST
    user_agent = USER_AGENT
    wire_models = WIRE_MODELS
    wire_models_high_effort = WIRE_MODELS_HIGH_EFFORT

    def parse_import(self, payload: Any, filename: str) -> list[ImportedAccount]:
        return parse_oauth_payload(payload, filename=filename)

    def catalog(self) -> list[ModelInfo]:
        models = [
            ModelInfo(
                id=model_id,
                provider=self.id,
                type="text",
                capabilities=_TEXT_CAPS,
                default=is_default,
                owned_by="anthropic" if model_id.startswith("claude-") else "google",
            )
            for model_id, is_default in PUBLIC_MODELS
        ]
        models.extend(
            ModelInfo(
                id=alias,
                provider=self.id,
                type="text",
                capabilities=_TEXT_CAPS,
                owned_by="anthropic" if alias.startswith("claude-") else "google",
                alias_of=target,
            )
            for alias, target in ALIAS_MODELS
        )
        models.append(
            ModelInfo(
                id=IMAGE_MODEL,
                provider=self.id,
                type="image",
                capabilities=_IMAGE_CAPS,
                default=True,
                owned_by="google",
            )
        )
        from app.providers.catalog_extra import extras_for

        seen = {item.id for item in models}
        for extra in extras_for(self.id):
            if extra.model_id in seen or public_slug_for_variant(extra.model_id):
                continue
            seen.add(extra.model_id)
            models.append(
                ModelInfo(
                    id=extra.model_id,
                    provider=self.id,
                    type=extra.model_type,
                    capabilities=_IMAGE_CAPS if extra.model_type == "image" else _TEXT_CAPS,
                    owned_by="anthropic" if extra.model_id.startswith("claude-") else "google",
                )
            )
        return models
