from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlencode, urlunsplit, urlsplit

import httpx
from fastapi import HTTPException

from app.config import settings
from app.http_client import new_client
from app.providers.oauth_login import jwt_claims
from app.providers.base import (
    CAP_CHAT,
    CAP_IMAGE,
    CAP_IMAGE_EDIT,
    CAP_RESPONSES,
    CAP_STREAM,
    CAP_VIDEO,
    ImportedAccount,
    ModelInfo,
    looks_like_image_slug,
    looks_like_video_slug,
)

AuthMode = Literal["oauth", "api_key"]
_REFRESH_WINDOW = dt.timedelta(minutes=5)
_ISSUER_KEY = re.compile(r"^https://auth\.x\.ai::")
OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OAUTH_AUTH_URL = "https://auth.x.ai/oauth2/authorize"
OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
OAUTH_DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
OAUTH_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OAUTH_USERINFO_URL = "https://auth.x.ai/oauth2/userinfo"
OAUTH_CALLBACK_PORT = 56121
OAUTH_REDIRECT_URI = "http://127.0.0.1:56121/callback"
OAUTH_SCOPES = "openid profile email offline_access grok-cli:access api:access"
OAUTH_REFERRER = "grok-build"
OAUTH_BIND_HOSTS = ("127.0.0.1",)
OAUTH_ISSUER = "https://auth.x.ai"
_GROK_CAPS = (CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT, CAP_VIDEO)
logger = logging.getLogger("transfer_station.errors")


@dataclass(frozen=True, slots=True)
class GrokCredentials:
    auth_mode: AuthMode
    access_token: str
    refresh_token: str | None
    expires_at: dt.datetime | None
    user_id: str | None
    email: str | None
    client_id: str | None
    issuer_key: str | None
    source_path: str | None
    path: Path
    raw: dict[str, Any]


def grok_home() -> Path:
    configured = settings.grok_home.strip()
    if configured:
        return Path(configured).expanduser()
    env_home = os.environ.get("GROK_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".grok"


def local_auth_path() -> Path:
    return grok_home() / "auth.json"


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"^(.*?)(\.\d+)?([+-]\d{2}:\d{2})$", text)
    if match:
        head, fraction, tz = match.group(1), match.group(2) or "", match.group(3)
        digits = re.sub(r"\D", "", fraction)[:6].ljust(6, "0") if fraction else ""
        text = f"{head}.{digits}{tz}" if digits else f"{head}{tz}"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _entry_user_id(entry: dict[str, Any]) -> str | None:
    for key in ("user_id", "principal_id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _flatten_cli_entry(issuer_key: str, entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    payload["_issuer_key"] = issuer_key
    if not payload.get("oidc_client_id") and "::" in issuer_key:
        payload["oidc_client_id"] = issuer_key.split("::", 1)[1]
    return payload


def _imported(
    account_id: str,
    payload: dict[str, Any],
    *,
    label: str | None,
    auth_mode: str,
    expires_at: str | None,
    source_path: str | None = None,
) -> ImportedAccount:
    return ImportedAccount(
        account_id=account_id,
        payload=payload,
        expires_at=expires_at,
        label=label,
        auth_mode=auth_mode,
        capabilities=_GROK_CAPS,
        source_path=source_path,
    )


def parse_auth_payload(payload: Any, *, source: str, path: Any) -> list[ImportedAccount]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="Grok credential payload must be a JSON object")
    source_path = str(path) if path else None

    issuer_entries = [
        (key, value)
        for key, value in payload.items()
        if isinstance(key, str) and _ISSUER_KEY.match(key) and isinstance(value, dict)
    ]
    if issuer_entries:
        imported: list[ImportedAccount] = []
        for issuer_key, entry in issuer_entries:
            user_id = _entry_user_id(entry)
            token = entry.get("key") or entry.get("access_token")
            if not user_id or not isinstance(token, str) or not token:
                continue
            email = entry.get("email") if isinstance(entry.get("email"), str) else None
            imported.append(
                _imported(
                    f"grok:{user_id}",
                    _flatten_cli_entry(issuer_key, entry),
                    label=email or user_id,
                    auth_mode="oauth",
                    expires_at=entry.get("expires_at") if isinstance(entry.get("expires_at"), str) else None,
                    source_path=source_path if source == "local" else None,
                )
            )
        if imported:
            return imported
        raise HTTPException(status_code=503, detail="No Grok OAuth account was found in auth.json")

    api_key = payload.get("XAI_API_KEY") or payload.get("api_key") or payload.get("xai_api_key")
    if isinstance(api_key, str) and api_key.startswith("xai-"):
        fingerprint = api_key[4:12] or "key"
        return [
            _imported(
                f"grok:key:{fingerprint}",
                {"api_key": api_key, "auth_mode": "api_key"},
                label=f"xai-{fingerprint}",
                auth_mode="api_key",
                expires_at=None,
            )
        ]

    token = payload.get("key") or payload.get("access_token")
    refresh = payload.get("refresh_token")
    user_id = _entry_user_id(payload)
    if isinstance(token, str) and token:
        if not user_id:
            user_id = "imported"
        email = payload.get("email") if isinstance(payload.get("email"), str) else None
        stored = dict(payload)
        if refresh and "refresh_token" not in stored:
            stored["refresh_token"] = refresh
        return [
            _imported(
                f"grok:{user_id}",
                stored,
                label=email or user_id,
                auth_mode="oauth",
                expires_at=payload.get("expires_at") if isinstance(payload.get("expires_at"), str) else None,
            )
        ]

    raise HTTPException(status_code=503, detail="No Grok access token was found in auth.json")


def load_credentials(payload: dict[str, Any], *, path: Path) -> GrokCredentials:
    nested = payload.get("entry") if isinstance(payload.get("entry"), dict) else None
    data = nested or payload
    api_key = data.get("api_key") or data.get("XAI_API_KEY")
    token = data.get("key") or data.get("access_token")
    if isinstance(api_key, str) and api_key.startswith("xai-") and not (
        isinstance(token, str) and token
    ):
        return GrokCredentials(
            auth_mode="api_key",
            access_token=api_key,
            refresh_token=None,
            expires_at=None,
            user_id=None,
            email=None,
            client_id=None,
            issuer_key=None,
            source_path=payload.get("source_path") if isinstance(payload.get("source_path"), str) else None,
            path=path,
            raw=payload,
        )
    if not isinstance(token, str) or not token:
        raise HTTPException(status_code=503, detail="Grok credential file is missing an access token")
    refresh = data.get("refresh_token")
    return GrokCredentials(
        auth_mode="oauth",
        access_token=token,
        refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        expires_at=_parse_datetime(data.get("expires_at")),
        user_id=_entry_user_id(data),
        email=data.get("email") if isinstance(data.get("email"), str) else None,
        client_id=(
            data.get("oidc_client_id")
            if isinstance(data.get("oidc_client_id"), str)
            else None
        ),
        issuer_key=data.get("_issuer_key") if isinstance(data.get("_issuer_key"), str) else None,
        source_path=payload.get("source_path") if isinstance(payload.get("source_path"), str) else None,
        path=path,
        raw=payload,
    )


def should_refresh(credentials: GrokCredentials, *, now: dt.datetime | None = None) -> bool:
    if credentials.auth_mode != "oauth" or not credentials.refresh_token:
        return False
    current = now or dt.datetime.now(dt.UTC)
    if credentials.expires_at is None:
        return True
    return credentials.expires_at - current <= _REFRESH_WINDOW


def remember_text_models(model_ids: list[str]) -> None:
    from app.providers.catalog_extra import infer_model_type, set_upstream

    rows = []
    for model_id in model_ids:
        if not isinstance(model_id, str) or not model_id.startswith("grok-"):
            continue
        if looks_like_image_slug(model_id) or looks_like_video_slug(model_id):
            continue
        rows.append((model_id, infer_model_type(model_id)))
    set_upstream("grok", rows)


def parse_openai_model_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for item in data:
        model_id = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else ""
        if not isinstance(model_id, str):
            continue
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        found.append(model_id)
    return found


def upstream_base_url(auth_mode: str) -> str:
    if auth_mode == "api_key":
        return settings.grok_api_base_url.rstrip("/")
    return settings.grok_oauth_base_url.rstrip("/")


def build_headers(
    credentials: GrokCredentials,
    *,
    accept: str = "application/json",
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": f"xai-grok-workspace/{settings.grok_cli_version}",
    }
    if credentials.auth_mode == "oauth":
        headers["X-XAI-Token-Auth"] = "xai-grok-cli"
        headers["x-grok-client-version"] = settings.grok_cli_version
    return headers


_SAFE_REJECT_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\[\]-]{0,80}")


def grok_reject_hint(message: str | None) -> str:
    """Classify an xAI 4xx without retaining freeform upstream text."""
    if not isinstance(message, str) or not message.strip():
        return "none"
    lowered = message.lower()
    if "argument not supported" in lowered:
        quoted = re.search(r'"([^"]{1,80})"', message)
        token = quoted.group(1) if quoted else ""
        if token and _SAFE_REJECT_TOKEN.fullmatch(token):
            return f"unsupported_argument:{token}"
        bare = re.search(
            r"Argument not supported:\s*([A-Za-z][A-Za-z0-9_.\[\]-]{0,80})",
            message,
        )
        if bare:
            return f"unsupported_argument:{bare.group(1)}"
        return "unsupported_argument"
    if "tool parameter root" in lowered:
        named = re.search(
            r"\b([A-Za-z][A-Za-z0-9_-]{0,80}): tool parameter root",
            message,
        )
        if named:
            return f"tool_parameter_root:{named.group(1)}"
        return "tool_parameter_root"
    if "missing field" in lowered:
        return "missing_field"
    return "other"


def map_upstream_status(status_code: int, message: str | None = None) -> tuple[int, str]:
    if status_code == 426:
        return (
            426,
            "Grok CLI proxy rejected the client version; set GROK_CLI_VERSION to match the local Grok CLI",
        )
    if status_code == 401:
        return 401, "Grok authentication expired; import a refreshed auth.json"
    if status_code == 403:
        detail = "Current Grok account is not allowed to use this model"
        return 403, f"{detail}: {message}" if message else detail
    if status_code == 429:
        return 429, "Grok rate limit reached; retry later"
    if status_code >= 500:
        return 502, f"Grok upstream is temporarily unavailable (HTTP {status_code})"
    if status_code >= 400:
        base = f"Grok upstream request failed (HTTP {status_code})"
        return status_code, f"{base}: {message}" if message else base
    return status_code, "ok"


def _persist(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def _write_back_source(credentials: GrokCredentials, entry: dict[str, Any]) -> None:
    source = credentials.source_path
    issuer_key = credentials.issuer_key
    if not source or not issuer_key:
        return
    path = Path(source)
    if not path.is_file():
        return
    try:
        original = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(original, dict) or not isinstance(original.get(issuer_key), dict):
        return
    merged = dict(original[issuer_key])
    for field in ("key", "access_token", "refresh_token", "expires_at"):
        if field in entry and entry[field]:
            merged[field] = entry[field]
    original[issuer_key] = merged
    _persist(path, original)


async def refresh_credentials(
    credentials: GrokCredentials, client: httpx.AsyncClient | None = None
) -> GrokCredentials:
    if not credentials.refresh_token or not credentials.client_id:
        raise HTTPException(
            status_code=401, detail="Grok authentication expired; import a refreshed auth.json"
        )

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.post(
            settings.grok_refresh_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
                "client_id": credentials.client_id,
            },
            timeout=httpx.Timeout(
                30.0,
                connect=10.0,
                pool=settings.gateway_pool_timeout_seconds,
            ),
        )

    try:
        if client is None:
            async with new_client() as temporary_client:
                response = await request(temporary_client)
        else:
            response = await request(client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Grok token refresh timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Grok token endpoint") from exc
    if response.status_code >= 400:
        raise HTTPException(
            status_code=401, detail="Grok authentication expired; import a refreshed auth.json"
        )
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Grok token refresh returned invalid JSON") from exc
    access = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(access, str) or not access:
        raise HTTPException(status_code=502, detail="Grok token refresh returned no access token")

    raw = dict(credentials.raw)
    entry = dict(raw.get("entry") or raw)
    entry["key"] = access
    entry["access_token"] = access
    if isinstance(body.get("refresh_token"), str) and body["refresh_token"]:
        entry["refresh_token"] = body["refresh_token"]
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        entry["expires_at"] = (
            dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(expires_in))
        ).isoformat().replace("+00:00", "Z")
    if "entry" in raw:
        raw["entry"] = entry
    else:
        raw.update(entry)
    _persist(Path(credentials.path), raw)
    _write_back_source(credentials, entry)
    return load_credentials(raw, path=Path(credentials.path))


def oauth_redirect_uri() -> str:
    return OAUTH_REDIRECT_URI


def build_oauth_auth_url(
    state: str,
    *,
    code_challenge: str,
    nonce: str,
    redirect_uri: str | None = None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri or OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "referrer": OAUTH_REFERRER,
    }
    return f"{OAUTH_AUTH_URL}?{urlencode(params, quote_via=quote)}"


async def _grok_form_post(
    url: str,
    data: dict[str, str],
    *,
    client: httpx.AsyncClient | None = None,
) -> httpx.Response:
    timeout = httpx.Timeout(30.0, connect=10.0, pool=settings.gateway_pool_timeout_seconds)

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.post(
            url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": f"xai-grok-workspace/{settings.grok_cli_version}",
            },
            data=data,
            timeout=timeout,
        )

    try:
        if client is None:
            async with new_client() as temporary_client:
                return await request(temporary_client)
        return await request(client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Grok login timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Grok login endpoint") from exc


async def start_device_authorization(
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    response = await _grok_form_post(
        OAUTH_DEVICE_URL,
        {
            "client_id": OAUTH_CLIENT_ID,
            "scope": OAUTH_SCOPES,
            "referrer": OAUTH_REFERRER,
        },
        client=client,
    )
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail="Grok device login could not start")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Grok login returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Grok login returned invalid JSON")
    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification = body.get("verification_uri_complete") or body.get("verification_uri")
    if not isinstance(device_code, str) or not device_code.strip():
        raise HTTPException(status_code=502, detail="Grok login did not return a device code")
    if not isinstance(user_code, str) or not user_code.strip():
        raise HTTPException(status_code=502, detail="Grok login did not return a user code")
    if not isinstance(verification, str) or not verification.strip():
        raise HTTPException(status_code=502, detail="Grok login did not return a verification URL")
    verification = _device_page_url(verification, user_code=user_code.strip())
    expires_in = body.get("expires_in")
    interval = body.get("interval")
    return {
        "device_code": device_code.strip(),
        "user_code": user_code.strip(),
        "verification_url": verification.strip(),
        "expires_in": int(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 600,
        "interval": int(interval) if isinstance(interval, (int, float)) and interval > 0 else 5,
    }


async def poll_device_authorization(
    device_code: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    token = (device_code or "").strip()
    if not token:
        raise HTTPException(status_code=422, detail="Grok login did not return a device code")
    response = await _grok_form_post(
        OAUTH_TOKEN_URL,
        {
            "grant_type": OAUTH_DEVICE_GRANT,
            "client_id": OAUTH_CLIENT_ID,
            "device_code": token,
        },
        client=client,
    )
    if response.status_code == 200:
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=502, detail="Grok login returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=502, detail="Grok login returned invalid JSON")
        return body
    error_name = ""
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str):
            error_name = error.strip()
    if error_name in {"authorization_pending", "slow_down"} or not error_name:
        return None
    if error_name == "access_denied":
        raise HTTPException(status_code=422, detail="Grok login was cancelled or not authorized")
    if error_name == "expired_token":
        raise HTTPException(status_code=410, detail="Grok login expired. Start again.")
    logger.warning("grok_device_poll status=%s error=%s", response.status_code, error_name[:80])
    return None


def _device_page_url(value: str, user_code: str | None = None) -> str:
    split = urlsplit((value or "").strip())
    host = (split.netloc or "").lower()
    if split.scheme == "https" and host in {"accounts.x.ai", "auth.x.ai"}:
        path = split.path or "/oauth2/device"
        pairs = [(key, item) for key, item in parse_qsl(split.query, keep_blank_values=False) if key == "user_code"]
    else:
        host = "accounts.x.ai"
        path = "/oauth2/device"
        pairs = []
    code = (user_code or (pairs[0][1] if pairs else "")).strip()
    query = urlencode([("user_code", code)]) if code else ""
    return urlunsplit(("https", host, path, query, ""))


async def _grok_userinfo(
    access_token: str, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.get(
            OAUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=httpx.Timeout(30.0, connect=10.0, pool=settings.gateway_pool_timeout_seconds),
        )

    try:
        if client is None:
            async with new_client() as temporary_client:
                response = await request(temporary_client)
        else:
            response = await request(client)
    except httpx.HTTPError:
        return {}
    if response.status_code >= 400:
        return {}
    try:
        body = response.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


async def exchange_authorization_code(
    code: str,
    *,
    redirect_uri: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    token = (code or "").strip()
    verifier = (code_verifier or "").strip()
    if not token or not verifier:
        raise HTTPException(status_code=422, detail="Grok login did not return an authorization code")

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.post(
            OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": OAUTH_CLIENT_ID,
                "code": token,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            timeout=httpx.Timeout(
                30.0,
                connect=10.0,
                pool=settings.gateway_pool_timeout_seconds,
            ),
        )

    try:
        if client is None:
            async with new_client() as temporary_client:
                response = await request(temporary_client)
        else:
            response = await request(client)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Grok login timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Grok login endpoint") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail="Grok login could not exchange the authorization code")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Grok login returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Grok login returned invalid JSON")
    return await credentials_from_token_response(body, client=client)


async def credentials_from_token_response(
    body: dict[str, Any],
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        raise HTTPException(status_code=422, detail="Grok login did not return an access token")
    if not isinstance(refresh, str) or not refresh.strip():
        raise HTTPException(
            status_code=422,
            detail="Grok login did not return a refresh token. Click Allow on the consent screen and retry.",
        )
    id_token = body.get("id_token") if isinstance(body.get("id_token"), str) else None
    claims = jwt_claims(id_token or "") or {}
    userinfo = await _grok_userinfo(access, client)
    user_id = (
        (claims.get("sub") if isinstance(claims.get("sub"), str) else None)
        or (userinfo.get("sub") if isinstance(userinfo.get("sub"), str) else None)
        or _entry_user_id(userinfo)
    )
    email = (
        (claims.get("email") if isinstance(claims.get("email"), str) else None)
        or (userinfo.get("email") if isinstance(userinfo.get("email"), str) else None)
    )
    if not user_id:
        raise HTTPException(status_code=422, detail="Grok login did not return an account id")
    expires_at = None
    expires_in = body.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expires_at = (
            dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(expires_in))
        ).isoformat().replace("+00:00", "Z")
    issuer_key = f"{OAUTH_ISSUER}::{OAUTH_CLIENT_ID}"
    entry = {
        "user_id": user_id.strip(),
        "key": access.strip(),
        "access_token": access.strip(),
        "refresh_token": refresh.strip(),
        "oidc_client_id": OAUTH_CLIENT_ID,
    }
    if isinstance(email, str) and email.strip():
        entry["email"] = email.strip()
    if expires_at:
        entry["expires_at"] = expires_at
    return {issuer_key: entry}


async def exchange_device_authorization(
    device_code: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any] | None:
    body = await poll_device_authorization(device_code, client=client)
    if body is None:
        return None
    return await credentials_from_token_response(body, client=client)


_GROK_INPUT_ITEM_TYPES = frozenset(
    {
        "message",
        "web_search_call",
        "file_search_call",
        "code_interpreter_call",
        "image_generation_call",
        "x_search_call",
    }
)
_IMAGE_CONTENT_TYPES = frozenset({"input_image", "image_url", "image"})
_TEXT_CONTENT_TYPES = frozenset({"input_text", "output_text", "text"})
_IMAGE_DETAILS = frozenset({"auto", "low", "high"})
_GROK_IMAGE_DATA_PREFIXES = ("data:image/png", "data:image/jpeg", "data:image/jpg")
_DROPPED_INPUT_TYPES = frozenset(
    {
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
        "item_reference",
    }
)
_COMPACTION_ITEM_TYPES = frozenset({"compaction", "context_compaction"})
TOOL_SEARCH_WIRE_NAME = "codex_tool_search"
_TOOL_SEARCH_NAMES = frozenset({"tool_search", TOOL_SEARCH_WIRE_NAME})
COMPACT_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its "
    "thinking process. You also have access to the state of the tools that were used by "
    "that language model. Use this to build on the work that has already been done and "
    "avoid duplicating work. Here is the summary produced by the other language model, "
    "use the information in this summary to assist with your own analysis:"
)
COMPACT_SUMMARIZATION_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for "
    "another LLM that will resume the task.\n\nInclude:\n"
    "- Current progress and key decisions made\n"
    "- Important context, constraints, or user preferences\n"
    "- What remains to be done (clear next steps)\n"
    "- Any critical data, examples, or references needed to continue\n"
    "- All user messages so far, verbatim or near-verbatim, in chronological order\n"
    "- Next Step — the immediate next action aligned with the user's most recent "
    "explicit request, with a verbatim quote from that message.\n\n"
    "Be concise, structured, and focused on helping the next LLM continue the work."
)
_GROK_RESPONSE_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "stream",
        "store",
        "reasoning",
        "include",
        "temperature",
        "top_p",
        "max_output_tokens",
        "parallel_tool_calls",
        "text",
        "prompt_cache_key",
    }
)
_GROK_INCLUDE_ALIASES = {
    "reasoning.encrypted_content": "reasoning.encrypted_content",
    "web_search_call_output": "web_search_call.action.sources",
    "web_search_call.action.sources": "web_search_call.action.sources",
    "code_execution_call_output": "code_interpreter_call.outputs",
    "code_execution_call.outputs": "code_interpreter_call.outputs",
    "code_interpreter_call.outputs": "code_interpreter_call.outputs",
    "file_search_call.results": "file_search_call.results",
    "collections_search_call_output": "file_search_call.results",
    "message.output_text.logprobs": "message.output_text.logprobs",
}
_SHELL_CALL_TYPES = {
    "shell_call": "shell_command",
    "local_shell_call": "local_shell",
    "unified_exec_call": "unified_exec",
    "command_execution": "shell_command",
}
_TOOL_OUTPUT_TYPES = frozenset(
    {
        "function_call_output",
        "custom_tool_call_output",
        "shell_call_output",
        "local_shell_call_output",
        "unified_exec_call_output",
        "mcp_call_output",
        "computer_call_output",
        "apply_patch_call_output",
    }
)
_GROK_BUILTIN_TOOLS = {
    "web_search": "web_search",
    "web_search_preview": "web_search",
    "web_search_preview_2025_03_11": "web_search",
    "x_search": "x_search",
    "code_interpreter": "code_interpreter",
    "code_execution": "code_interpreter",
    "file_search": "file_search",
    "attachment_search": "attachment_search",
    "collections_search": "collections_search",
    "image_generation": "image_generation",
}
_CODEX_CLIENT_TOOLS = {
    "local_shell",
    "shell",
    "shell_command",
    "unified_exec",
    "apply_patch",
    "custom_tool",
    "view_image",
    "computer",
    "computer_use_preview",
    "exec",
}
_FREEFORM_TOOL_TYPES = frozenset({"custom", "freeform", "custom_tool"})
_SHELL_TOOL_NAMES = frozenset(
    {"shell_command", "local_shell", "shell", "unified_exec", "exec_command", "bash"}
)
_EXEC_COMMAND_NAMES = frozenset({"exec_command"})
_PATCH_TOOL_NAMES = frozenset({"apply_patch", "ApplyPatch"})
_COMPUTER_TOOL_NAMES = frozenset({"computer", "computer_use_preview"})
_COMPUTER_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "object",
            "description": "Computer-use action payload from the Codex client",
        }
    },
    "required": ["action"],
}
_TOOL_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "BM25 query over deferred MCP and connector tools",
        }
    },
    "required": ["query"],
}
_TOOL_SEARCH_DESCRIPTION = (
    "Discover deferred tools by name. Call this before using MCP or connector tools "
    "that are not already in the tool list. Put the search query in `query`."
)
_FREEFORM_PARAMETERS = {
    "type": "object",
    "properties": {
        "input": {
            "type": "string",
            "description": (
                "Freeform tool input. For apply_patch this is the full patch document "
                "including *** Begin Patch / *** Update File / *** Add File / *** Delete File."
            ),
        }
    },
    "required": ["input"],
}
_SHELL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Command argv to execute in the workspace",
        },
        "workdir": {"type": "string", "description": "Working directory for the command"},
        "timeout_ms": {"type": "number"},
        "justification": {"type": "string"},
    },
    "required": ["command"],
}
_EXEC_COMMAND_PARAMETERS = {
    "type": "object",
    "properties": {
        "cmd": {
            "type": "string",
            "description": "Command to execute in the user workspace",
        },
        "workdir": {"type": "string", "description": "Working directory for the command"},
        "yield_time_ms": {
            "type": "integer",
            "description": "Milliseconds to wait before yielding PTY output",
        },
    },
    "required": ["cmd"],
}
_APPLY_PATCH_DESCRIPTION = (
    "Apply a file patch to the workspace. Put the full apply_patch document in `input`. "
    "Do not describe the edit in chat; call this tool so the client can modify files."
)
_SHELL_DESCRIPTION = (
    "Run a command in the user workspace. Results come back as a transcript: "
    "Output fenced in triple backticks, then [exit code: N]. "
    "Empty Output with [exit code: 0] means the command succeeded; "
    "stdout may be missing even when the side effect completed."
)
_SHELL_RESULT_NOTE = (
    " Results come back as a transcript: Output in triple backticks, then [exit code: N]. "
    "Empty Output with [exit code: 0] means the command succeeded; stdout may be missing."
)
_COMPUTER_DESCRIPTION = "Perform a computer-use action in the user workspace."


@dataclass(frozen=True, slots=True)
class GrokRewriteSpec:
    freeform_tool_names: frozenset[str] = frozenset()
    tool_search_wire_name: str | None = None
    namespace_map: dict[str, str] = field(default_factory=dict)

    def active(self) -> bool:
        return bool(self.freeform_tool_names or self.tool_search_wire_name or self.namespace_map)


@dataclass(frozen=True, slots=True)
class GrokSanitizeResult:
    payload: dict[str, Any]
    freeform_tool_names: frozenset[str]
    tool_search_wire_name: str | None = None
    namespace_map: dict[str, str] = field(default_factory=dict)
    compact_v2: bool = False

    @property
    def rewrite(self) -> GrokRewriteSpec:
        return GrokRewriteSpec(
            freeform_tool_names=self.freeform_tool_names,
            tool_search_wire_name=self.tool_search_wire_name,
            namespace_map=dict(self.namespace_map),
        )


def _tool_name(item: dict[str, Any], tool_type: str) -> str | None:
    function = item.get("function")
    if isinstance(function, dict):
        nested = function.get("name")
        if isinstance(nested, str) and nested:
            return nested
    name = item.get("name")
    if isinstance(name, str) and name:
        return name
    if tool_type in _CODEX_CLIENT_TOOLS:
        return tool_type
    return None


def _collect_tool_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _parameters_empty(params: Any) -> bool:
    if not isinstance(params, dict):
        return True
    properties = params.get("properties")
    if isinstance(properties, dict) and properties:
        return False
    if params.get("anyOf") or params.get("oneOf") or params.get("allOf"):
        return False
    return True


def _schema_is_object(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    schema_type = schema.get("type")
    if schema_type == "object" or "properties" in schema:
        return True
    if isinstance(schema_type, list) and "object" in schema_type:
        return True
    return False


def _object_union_branches(options: Any) -> list[dict[str, Any]]:
    if not isinstance(options, list):
        return []
    return [item for item in options if _schema_is_object(item)]


def _normalize_tool_parameters(params: Any, *, name: str, tool_type: str) -> dict[str, Any]:
    """Force an object-root JSON Schema; xAI 400s on scalar/null anyOf at the root."""
    if not isinstance(params, dict):
        return _default_parameters(name, tool_type)
    out = dict(params)
    for key in ("anyOf", "oneOf"):
        branches = _object_union_branches(out.pop(key, None))
        if not branches:
            continue
        first = dict(branches[0])
        for field, value in first.items():
            if field in {"anyOf", "oneOf", "allOf"}:
                continue
            out.setdefault(field, value)
    all_of = out.pop("allOf", None)
    if isinstance(all_of, list):
        for item in all_of:
            if not _schema_is_object(item):
                continue
            for field, value in item.items():
                out.setdefault(field, value)
    if not _schema_is_object(out):
        return _default_parameters(name, tool_type)
    out["type"] = "object"
    out.pop("$ref", None)
    out.pop("$schema", None)
    out.pop("nullable", None)
    return out


def _param_root_kind(params: Any) -> str:
    if not isinstance(params, dict):
        return type(params).__name__
    if isinstance(params.get("anyOf"), list):
        return "anyOf"
    if isinstance(params.get("oneOf"), list):
        return "oneOf"
    if isinstance(params.get("allOf"), list):
        return "allOf"
    schema_type = params.get("type")
    if schema_type == "object" or "properties" in params:
        return "object"
    if isinstance(schema_type, str) and schema_type:
        return schema_type
    return "other"


def _sanitize_grok_include(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    seen: set[str] = set()
    mapped: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        alias = _GROK_INCLUDE_ALIASES.get(item.strip())
        if not alias or alias in seen:
            continue
        seen.add(alias)
        mapped.append(alias)
    return mapped or None


def _json_object_arguments(value: Any) -> str:
    if isinstance(value, dict) and "input" in value:
        text = value.get("input")
        if not isinstance(text, str):
            text = "" if text is None else json.dumps(text, ensure_ascii=False)
        return json.dumps({"input": text}, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "input" in parsed:
                return _json_object_arguments(parsed)
        return json.dumps({"input": value}, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return '{"input":""}'
    return json.dumps({"input": json.dumps(value, ensure_ascii=False)}, ensure_ascii=False, separators=(",", ":"))


def extract_freeform_input(arguments: Any) -> str:
    if isinstance(arguments, dict):
        value = arguments.get("input")
        if isinstance(value, str):
            return value
        if value is None:
            return ""
        return json.dumps(value, ensure_ascii=False)
    if not isinstance(arguments, str):
        return "" if arguments is None else str(arguments)
    stripped = arguments.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        if isinstance(parsed, dict) and "input" in parsed:
            return extract_freeform_input(parsed)
    return arguments


def is_tool_search_name(name: str | None) -> bool:
    return isinstance(name, str) and name in _TOOL_SEARCH_NAMES


def grok_model_supports_reasoning_effort(model: str) -> bool:
    lowered = model.strip().lower()
    return not lowered.startswith("grok-build") and "composer" not in lowered


def map_grok_reasoning_effort(effort: Any) -> str:
    value = str(effort or "high").strip().lower()
    if value in {"minimal", "low", "none"}:
        return "low"
    if value == "medium":
        return "medium"
    if value in {"xhigh", "x-high"}:
        return "xhigh"
    return "high"


def normalize_tool_search_arguments(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        query = value.get("query")
        if isinstance(query, str):
            return {"query": query}
        if query is None:
            return {"query": ""}
        return {"query": json.dumps(query, ensure_ascii=False)}
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return normalize_tool_search_arguments(parsed)
        return {"query": value}
    if value is None:
        return {"query": ""}
    return {"query": str(value)}


def _as_rewrite_spec(value: GrokRewriteSpec | frozenset[str] | None) -> GrokRewriteSpec:
    if isinstance(value, GrokRewriteSpec):
        return value
    if isinstance(value, frozenset):
        return GrokRewriteSpec(freeform_tool_names=value)
    return GrokRewriteSpec()


def _compaction_to_message(item: dict[str, Any]) -> dict[str, Any] | None:
    text = item.get("encrypted_content") or item.get("summary") or item.get("content")
    if isinstance(text, list):
        text = _stringify_tool_output(text)
    if not isinstance(text, str) or not text.strip():
        return None
    return {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }


def _tool_search_call_to_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    if not call_id:
        return None
    arguments = item.get("arguments")
    if arguments is None:
        arguments = item.get("input") or item.get("query")
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": TOOL_SEARCH_WIRE_NAME,
        "arguments": json.dumps(normalize_tool_search_arguments(arguments), ensure_ascii=False, separators=(",", ":")),
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    return mapped


def _discovered_tools_from_item(item: dict[str, Any]) -> list[Any]:
    tools = item.get("tools")
    if isinstance(tools, list):
        return tools
    output = item.get("output")
    if isinstance(output, list):
        return output
    if isinstance(output, str):
        stripped = output.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(output)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                nested = parsed.get("tools")
                if isinstance(nested, list):
                    return nested
    return []


def _tool_search_output_to_function_output(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    if not call_id:
        return None
    discovered = _discovered_tools_from_item(item)
    output = item.get("output")
    if isinstance(output, str) and output.strip():
        text = output
    else:
        text = json.dumps(discovered, ensure_ascii=False, separators=(",", ":")) if discovered else "[]"
    return {"type": "function_call_output", "call_id": call_id, "output": text}


def compact_v2_requested(payload: dict[str, Any]) -> bool:
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "compaction_trigger" for item in items)


def compact_history_usable(payload: dict[str, Any]) -> bool:
    items = payload.get("input")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"compaction_trigger", "reasoning"}:
            continue
        return True
    return False


def build_grok_compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    items = out.get("input")
    kept: list[Any] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"compaction_trigger", "reasoning"}:
                continue
            kept.append(item)
    kept.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": COMPACT_SUMMARIZATION_PROMPT}],
        }
    )
    out["input"] = kept
    out.pop("tools", None)
    out.pop("tool_choice", None)
    out.pop("parallel_tool_calls", None)
    return out


def extract_grok_message_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, list):
        output = payload.get("response", {}).get("output") if isinstance(payload.get("response"), dict) else None
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
    return "\n".join(parts).strip()


def grok_compact_encrypted_content(summary: str) -> str:
    text = summary.strip()
    if text.startswith(COMPACT_SUMMARY_PREFIX):
        return text
    return f"{COMPACT_SUMMARY_PREFIX}\n{text}"


def grok_compact_v2_sse(
    *,
    response_id: str,
    encrypted_content: str | None = None,
    usage: dict[str, Any] | None = None,
    failed_code: str | None = None,
    failed_message: str | None = None,
) -> str:
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(dt.datetime.now(dt.UTC).timestamp()),
            "status": "in_progress",
            "output": [],
        },
    }
    frames = [f"event: response.created\ndata: {json.dumps(created, ensure_ascii=False)}"]
    if failed_code:
        failed = {
            "type": "response.failed",
            "sequence_number": 1,
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": int(dt.datetime.now(dt.UTC).timestamp()),
                "status": "failed",
                "output": [],
                "error": {"code": failed_code, "message": failed_message or failed_code},
            },
        }
        frames.append(f"event: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}")
        return "\n\n".join(frames)
    item = {"type": "compaction", "encrypted_content": encrypted_content or ""}
    done = {
        "type": "response.output_item.done",
        "sequence_number": 1,
        "output_index": 0,
        "item": item,
    }
    completed = {
        "type": "response.completed",
        "sequence_number": 2,
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(dt.datetime.now(dt.UTC).timestamp()),
            "status": "completed",
            "output": [item],
            "usage": usage
            or {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        },
    }
    frames.append(f"event: response.output_item.done\ndata: {json.dumps(done, ensure_ascii=False)}")
    frames.append(f"event: response.completed\ndata: {json.dumps(completed, ensure_ascii=False)}")
    return "\n\n".join(frames)


def grok_compact_json_response(*, encrypted_content: str) -> dict[str, Any]:
    return {"output": [{"type": "compaction", "encrypted_content": encrypted_content}]}


@dataclass
class ParsedToolResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    status: str | None = None
    command: str | None = None
    kind: str = "generic"
    raw: str = ""


def _stringify_tool_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [text for text in (_stringify_tool_output(block) for block in value) if text]
        return "\n".join(parts)
    if isinstance(value, dict):
        block_type = str(value.get("type") or "")
        if block_type in {"input_text", "output_text", "text"}:
            text = value.get("text")
            return text if isinstance(text, str) else ""
        if block_type in _IMAGE_CONTENT_TYPES:
            return "[image]"
        if block_type == "computer_screenshot":
            return "[computer screenshot]"
        for key in ("stdout", "stderr", "text", "output", "content"):
            inner = value.get(key)
            if isinstance(inner, str) and inner:
                return inner
            if isinstance(inner, list):
                text = _stringify_tool_output(inner)
                if text:
                    return text
        if isinstance(value.get("structuredContent"), dict):
            return json.dumps(value["structuredContent"], ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False)


def _function_call_arguments(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return "{}"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "{}"
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
            if isinstance(parsed, list):
                return json.dumps({"command": parsed}, ensure_ascii=False, separators=(",", ":"))
        return json.dumps({"input": value}, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, list):
        return json.dumps({"command": value}, ensure_ascii=False, separators=(",", ":"))
    return json.dumps({"input": value}, ensure_ascii=False, separators=(",", ":"))


def _call_id_of(item: dict[str, Any]) -> str | None:
    call_id = item.get("call_id") or item.get("id")
    return call_id if isinstance(call_id, str) and call_id else None


def _custom_call_to_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    call_id = _call_id_of(item)
    if not isinstance(name, str) or not name or not call_id:
        return None
    raw_input = item.get("input")
    if raw_input is None:
        raw_input = item.get("arguments")
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": _json_object_arguments(raw_input),
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    if isinstance(item.get("status"), str) and item["status"]:
        mapped["status"] = item["status"]
    return mapped


def _shell_call_to_function_call(item: dict[str, Any], name: str) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    if not call_id:
        return None
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    command = (
        (action.get("command") if action else None)
        or (action.get("commands") if action else None)
        or item.get("command")
        or item.get("commands")
        or item.get("input")
    )
    if command is None:
        raw_arguments = item.get("arguments")
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            arguments = _function_call_arguments(raw_arguments)
        else:
            arguments = "{}"
    else:
        payload: dict[str, Any] = {"command": command}
        workdir = action.get("working_directory") or item.get("workdir") or item.get("working_directory")
        if isinstance(workdir, str) and workdir:
            payload["workdir"] = workdir
        timeout_ms = action.get("timeout_ms") if action else item.get("timeout_ms")
        if isinstance(timeout_ms, (int, float)):
            payload["timeout_ms"] = timeout_ms
        arguments = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    return mapped


def _normalize_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    call_id = _call_id_of(item)
    if not isinstance(name, str) or not name or not call_id:
        return None
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": _function_call_arguments(item.get("arguments")),
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    if isinstance(item.get("status"), str) and item["status"]:
        mapped["status"] = item["status"]
    return mapped


_CODEX_EXIT_LINE = re.compile(r"^Exit code:\s*(-?\d+)\s*$", re.IGNORECASE)
_GROK_EXIT_LINE = re.compile(r"^\[exit code:\s*(-?\d+)\]\s*$", re.IGNORECASE)
_PROCESS_EXITED = re.compile(r"Process exited with code\s+(-?\d+)", re.IGNORECASE)
_COMPLETED_STATUSES = frozenset({"completed", "complete", "success", "succeeded"})
_FAILED_STATUSES = frozenset({"failed", "incomplete", "cancelled", "canceled", "error"})


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _join_command(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, list) and value:
        return " ".join(str(part) for part in value)
    return None


def _command_from_arguments(arguments: Any) -> str | None:
    if isinstance(arguments, dict):
        return _join_command(
            arguments.get("command")
            or arguments.get("commands")
            or arguments.get("cmd")
            or arguments.get("input")
        )
    if not isinstance(arguments, str) or not arguments.strip():
        return None
    stripped = arguments.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return _command_from_arguments(parsed)
    return stripped


def _command_from_item(item: dict[str, Any]) -> str | None:
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    command = _join_command(
        action.get("command")
        or action.get("commands")
        or action.get("cmd")
        or item.get("command")
        or item.get("commands")
        or item.get("cmd")
        or item.get("input")
    )
    if command:
        return command
    return _command_from_arguments(item.get("arguments"))


_BOM_PREFIXES = ("\ufeff", "\xef\xbb\xbf", "ï»¿")
_EXIT_CODE_ANYWHERE = re.compile(r"exit\s*code\s*[:：]\s*(-?\d+)", re.IGNORECASE)
_EXEC_META_PREFIXES = (
    "exit code:",
    "wall time:",
    "duration:",
    "elapsed:",
    "total output lines:",
    "output:",
)


def _lstrip_boms(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for prefix in _BOM_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix) :]
                changed = True
    return text


def _normalize_exec_text(text: str) -> str:
    return _lstrip_boms(text.replace("\r\n", "\n").replace("\r", "\n"))


def _parse_codex_command_output(text: str) -> ParsedToolResult | None:
    lines = [_lstrip_boms(line) for line in _normalize_exec_text(text).split("\n")]
    if not lines:
        return None
    matched = _CODEX_EXIT_LINE.match(lines[0].strip())
    if not matched:
        desktop = _parse_desktop_exec_output(text)
        if desktop:
            return desktop
        return _loose_parse_exec_output(text)
    parsed = ParsedToolResult(kind="shell", exit_code=int(matched.group(1)), raw=text)
    rest = lines[1:]
    output_at: int | None = None
    for index, line in enumerate(rest):
        stripped = line.strip()
        if stripped.lower() == "output:":
            output_at = index + 1
            break
        if stripped == "" or stripped.lower().startswith(
            ("wall time:", "duration:", "elapsed:", "total output lines:")
        ):
            continue
        break
    if output_at is not None:
        parsed.stdout = "\n".join(rest[output_at:])
        return parsed
    skipped = 0
    for line in rest:
        stripped = line.strip()
        if stripped == "" or stripped.lower().startswith(
            ("wall time:", "duration:", "elapsed:", "total output lines:")
        ):
            skipped += 1
            continue
        break
    parsed.stdout = "\n".join(rest[skipped:])
    return parsed


def _parse_desktop_exec_output(text: str) -> ParsedToolResult | None:
    normalized = _normalize_exec_text(text)
    match = _PROCESS_EXITED.search(_lstrip_boms(normalized))
    if not match:
        return None
    parsed = ParsedToolResult(kind="shell", exit_code=int(match.group(1)), raw=text)
    body: list[str] = []
    saw_output = False
    for line in normalized.split("\n"):
        stripped = _lstrip_boms(line).strip()
        lower = stripped.lower()
        if not stripped:
            if saw_output:
                body.append(line)
            continue
        if lower.startswith("output:"):
            saw_output = True
            remainder = stripped.split(":", 1)[-1].strip()
            if remainder:
                body.append(remainder)
            continue
        if _PROCESS_EXITED.search(stripped) or lower.startswith(
            ("chunk id:", "wall time:", "original token count:")
        ):
            continue
        if saw_output:
            body.append(line)
    parsed.stdout = "\n".join(body)
    return parsed


def _loose_parse_exec_output(text: str) -> ParsedToolResult | None:
    normalized = _normalize_exec_text(text)
    exit_match = _EXIT_CODE_ANYWHERE.search(_lstrip_boms(normalized))
    if not exit_match:
        return None
    parsed = ParsedToolResult(kind="shell", exit_code=int(exit_match.group(1)), raw=text)
    body: list[str] = []
    saw_output = False
    for line in normalized.split("\n"):
        stripped = _lstrip_boms(line).strip()
        lower = stripped.lower()
        if not stripped:
            continue
        if _EXIT_CODE_ANYWHERE.match(lower) or lower.startswith(_EXEC_META_PREFIXES):
            if lower == "output:" or lower.startswith("output:"):
                remainder = stripped.split(":", 1)[-1].strip()
                saw_output = True
                if remainder:
                    body.append(remainder)
            continue
        body.append(line)
    parsed.stdout = "\n".join(body)
    if saw_output or parsed.exit_code is not None:
        return parsed
    return parsed


def _parse_grok_bash_transcript(text: str) -> ParsedToolResult | None:
    if "[exit code:" not in text.lower() and "i executed a terminal command:" not in text.lower():
        return None
    parsed = ParsedToolResult(kind="shell", raw=text)
    lines = text.replace("\r\n", "\n").split("\n")
    body: list[str] = []
    in_fence = False
    saw_output = False
    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("i executed a terminal command:"):
            command = stripped.split(":", 1)[-1].strip().strip("`")
            parsed.command = command or parsed.command
            continue
        exit_match = _GROK_EXIT_LINE.match(stripped)
        if exit_match:
            parsed.exit_code = int(exit_match.group(1))
            continue
        if lower == "output:":
            saw_output = True
            continue
        if stripped == "```":
            in_fence = not in_fence
            continue
        if saw_output or in_fence:
            body.append(line)
    parsed.stdout = "\n".join(body).strip("\n")
    return parsed if parsed.exit_code is not None or parsed.command or saw_output else None


def _merge_shell_chunk(target: ParsedToolResult, chunk: dict[str, Any]) -> None:
    target.kind = "shell"
    stdout = chunk.get("stdout")
    stderr = chunk.get("stderr")
    if isinstance(stdout, str) and stdout:
        target.stdout = f"{target.stdout}\n{stdout}" if target.stdout else stdout
    if isinstance(stderr, str) and stderr:
        target.stderr = f"{target.stderr}\n{stderr}" if target.stderr else stderr
    outcome = chunk.get("outcome") if isinstance(chunk.get("outcome"), dict) else {}
    exit_code = _as_int(outcome.get("exit_code"))
    if exit_code is None:
        exit_code = _as_int(chunk.get("exit_code"))
    if exit_code is not None:
        target.exit_code = exit_code
    outcome_type = outcome.get("type")
    if outcome_type == "timeout":
        target.status = "failed"
        if target.exit_code is None:
            target.exit_code = 124
    elif isinstance(outcome_type, str) and outcome_type:
        target.status = target.status or "completed"


def _parse_result_value(value: Any) -> ParsedToolResult:
    if value is None:
        return ParsedToolResult()
    if isinstance(value, str):
        normalized = _normalize_exec_text(value)
        stripped = normalized.strip()
        if not stripped:
            return ParsedToolResult(raw=value)
        prose = _parse_codex_command_output(normalized)
        if prose:
            return prose
        grok = _parse_grok_bash_transcript(value)
        if grok:
            return grok
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                loaded = json.loads(value)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, dict):
                parsed = _parse_result_value(loaded)
                parsed.raw = parsed.raw or value
                return parsed
        return ParsedToolResult(raw=value, stdout=value)
    if isinstance(value, list):
        if value and all(
            isinstance(block, dict)
            and ("stdout" in block or "stderr" in block or "outcome" in block)
            for block in value
        ):
            merged = ParsedToolResult(kind="shell")
            for block in value:
                _merge_shell_chunk(merged, block)
            return merged
        return _parse_result_value(_stringify_tool_output(value) or "")
    if not isinstance(value, dict):
        text = str(value)
        return ParsedToolResult(raw=text, stdout=text)
    block_type = str(value.get("type") or "")
    if block_type in {"input_text", "output_text", "text"}:
        text = value.get("text")
        return _parse_result_value(text if isinstance(text, str) else "")
    if block_type == "computer_screenshot":
        return ParsedToolResult(kind="generic", stdout="[computer screenshot]", status="completed")
    if "stdout" in value or "stderr" in value or "outcome" in value:
        parsed = ParsedToolResult(kind="shell")
        _merge_shell_chunk(parsed, value)
        return parsed
    if isinstance(value.get("content"), list) or isinstance(value.get("structuredContent"), dict):
        texts: list[str] = []
        content = value.get("content")
        if isinstance(content, list):
            text = _stringify_tool_output(content)
            if text:
                texts.append(text)
        structured = value.get("structuredContent")
        if isinstance(structured, dict) and structured:
            texts.append(json.dumps(structured, ensure_ascii=False))
        status = "failed" if value.get("isError") else "completed"
        joined = "\n".join(texts)
        return ParsedToolResult(kind="generic", stdout=joined, raw=joined, status=status)
    if any(key in value for key in ("exit_code", "stdout", "stderr", "status", "output")):
        nested = value.get("output")
        parsed = _parse_result_value(nested) if nested not in (None, "") else ParsedToolResult()
        if _as_int(value.get("exit_code")) is not None:
            parsed.exit_code = _as_int(value.get("exit_code"))
            parsed.kind = "shell"
        if isinstance(value.get("stdout"), str) and value["stdout"] and not parsed.stdout:
            parsed.stdout = value["stdout"]
        if isinstance(value.get("stderr"), str) and value["stderr"] and not parsed.stderr:
            parsed.stderr = value["stderr"]
        if isinstance(value.get("status"), str) and value["status"]:
            parsed.status = value["status"]
        if parsed.exit_code is not None or parsed.stdout or parsed.stderr:
            parsed.kind = "shell"
        if not parsed.raw:
            parsed.raw = parsed.stdout
        return parsed
    text = _stringify_tool_output(value)
    return ParsedToolResult(raw=text, stdout=text)


def _parse_item_result(item: dict[str, Any]) -> ParsedToolResult:
    parsed = _parse_result_value(item.get("output"))
    if isinstance(item.get("stdout"), str) and item["stdout"] and not parsed.stdout:
        parsed.stdout = item["stdout"]
        parsed.kind = "shell"
    if isinstance(item.get("stderr"), str) and item["stderr"] and not parsed.stderr:
        parsed.stderr = item["stderr"]
        parsed.kind = "shell"
    if parsed.exit_code is None:
        parsed.exit_code = _as_int(item.get("exit_code"))
        if parsed.exit_code is not None:
            parsed.kind = "shell"
    if not parsed.status and isinstance(item.get("status"), str) and item["status"]:
        parsed.status = item["status"]
    if not parsed.command:
        parsed.command = _command_from_item(item)
    item_type = str(item.get("type") or "")
    if item_type in _SHELL_CALL_TYPES or item_type.endswith("_output") and item_type.startswith(
        ("shell", "local_shell", "unified_exec")
    ):
        parsed.kind = "shell"
    if item_type in {"apply_patch_call", "apply_patch_call_output", "custom_tool_call_output"}:
        name = item.get("name")
        if item_type.startswith("apply_patch") or name in _PATCH_TOOL_NAMES:
            parsed.kind = "patch"
    if item_type in {"computer_call", "computer_call_output"}:
        parsed.kind = "generic" if parsed.kind != "shell" else parsed.kind
    return parsed


def _format_grok_bash_transcript(command: str | None, parsed: ParsedToolResult) -> str:
    stdout = parsed.stdout or ""
    if parsed.stderr:
        stdout = f"{stdout}\n{parsed.stderr}" if stdout else parsed.stderr
    if not stdout.strip():
        stdout = "Command completed (no captured stdout)."
    exit_code = parsed.exit_code
    if exit_code is None:
        if parsed.status in _FAILED_STATUSES:
            exit_code = 1
        else:
            exit_code = 0
    lines: list[str] = []
    command_text = command or parsed.command
    if command_text:
        lines.append(f"I executed a terminal command: `{command_text}`")
        lines.append("")
    lines.append("Output:")
    lines.append("```")
    lines.append(stdout.rstrip("\n"))
    lines.append("```")
    lines.append("")
    lines.append(f"[exit code: {exit_code}]")
    return "\n".join(lines)


def _format_tool_result(
    parsed: ParsedToolResult,
    *,
    name: str | None,
    command: str | None,
) -> str:
    tool_name = name or ""
    if tool_name in _PATCH_TOOL_NAMES or parsed.kind == "patch":
        text = (parsed.stdout or parsed.raw or "").strip()
        if text:
            return parsed.stdout or parsed.raw
        if parsed.exit_code not in (None, 0) or parsed.status in _FAILED_STATUSES:
            return text or "Failed"
        return "Success"
    shell = (
        tool_name in _SHELL_TOOL_NAMES
        or parsed.kind == "shell"
        or parsed.exit_code is not None
    )
    if shell:
        return _format_grok_bash_transcript(command, parsed)
    text = parsed.stdout or parsed.raw
    if text.strip():
        return text
    if parsed.status in _FAILED_STATUSES:
        return json.dumps({"status": parsed.status}, ensure_ascii=False, separators=(",", ":"))
    if parsed.status or parsed.exit_code is not None:
        body: dict[str, Any] = {}
        if parsed.exit_code is not None:
            body["exit_code"] = parsed.exit_code
        if parsed.status:
            body["status"] = parsed.status
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return text


def _grok_function_output_text(
    item: dict[str, Any],
    *,
    name: str | None = None,
    command: str | None = None,
) -> str:
    parsed = _parse_item_result(item)
    if name in _SHELL_TOOL_NAMES:
        parsed.kind = "shell"
        blob = _stringify_tool_output(item.get("output"))
        if not (parsed.stdout or "").strip() and parsed.exit_code is None:
            loose = _loose_parse_exec_output(blob) if blob else None
            if loose is not None:
                parsed = loose
                parsed.kind = "shell"
        if not (parsed.stdout or "").strip() and parsed.exit_code is None:
            raw = blob or parsed.raw or ""
            if len(raw.encode("utf-8")) <= 96 and not re.search(
                r"error|traceback|failed|denied", raw, re.IGNORECASE
            ):
                parsed.exit_code = 0
                parsed.stdout = ""
    return _format_tool_result(parsed, name=name, command=command)


def _tool_output_to_function_output(
    item: dict[str, Any],
    *,
    name: str | None = None,
    command: str | None = None,
) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    if not call_id:
        return None
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": _grok_function_output_text(item, name=name, command=command),
    }


def _sanitize_reasoning_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    out.pop("internal_chat_message_metadata_passthrough", None)
    for field_name in ("content", "summary"):
        value = out.get(field_name)
        if value is None or not isinstance(value, list):
            out.pop(field_name, None)
    if out.get("encrypted_content") is None:
        out.pop("encrypted_content", None)
    return out


def _structured_patch_to_input(item: dict[str, Any]) -> str | None:
    operations: list[Any] = []
    if isinstance(item.get("operation"), dict):
        operations = [item["operation"]]
    elif isinstance(item.get("operations"), list):
        operations = [op for op in item["operations"] if isinstance(op, dict)]
    if not operations:
        return None
    parts = ["*** Begin Patch"]
    for operation in operations:
        kind = str(operation.get("type") or operation.get("operation") or "update_file")
        path = str(operation.get("path") or operation.get("file") or "")
        diff = operation.get("diff") or operation.get("contents") or operation.get("content") or ""
        diff_text = diff if isinstance(diff, str) else json.dumps(diff, ensure_ascii=False)
        lowered = kind.lower()
        if lowered in {"add_file", "create_file", "add"}:
            parts.append(f"*** Add File: {path}")
            for line in diff_text.splitlines() or [""]:
                parts.append(line if line.startswith("+") else f"+{line}")
        elif lowered in {"delete_file", "delete"}:
            parts.append(f"*** Delete File: {path}")
        else:
            parts.append(f"*** Update File: {path}")
            parts.extend(diff_text.splitlines())
    parts.append("*** End Patch")
    return "\n".join(parts)


def _patch_call_to_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    name = item.get("name")
    if not isinstance(name, str) or not name:
        name = "apply_patch"
    if not call_id:
        return None
    raw_input = item.get("input")
    if raw_input is None:
        raw_input = item.get("patch") or item.get("diff")
    if raw_input is None:
        raw_input = _structured_patch_to_input(item)
    if raw_input is None:
        raw_input = item.get("arguments")
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": _json_object_arguments(raw_input),
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    if isinstance(item.get("status"), str) and item["status"]:
        mapped["status"] = item["status"]
    return mapped


def _computer_call_to_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    call_id = _call_id_of(item)
    if not call_id:
        return None
    name = item.get("name")
    if not isinstance(name, str) or not name:
        name = "computer"
    action = item.get("action") if isinstance(item.get("action"), dict) else None
    if action is not None:
        arguments = json.dumps({"action": action}, ensure_ascii=False, separators=(",", ":"))
    else:
        arguments = _function_call_arguments(item.get("arguments"))
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    return mapped


def _mcp_call_to_function_call(item: dict[str, Any]) -> dict[str, Any] | None:
    name = item.get("name")
    call_id = _call_id_of(item)
    if not isinstance(name, str) or not name or not call_id:
        return None
    arguments = item.get("arguments")
    if arguments is None:
        arguments = item.get("input")
    mapped: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": _function_call_arguments(arguments),
    }
    if isinstance(item.get("id"), str) and item["id"]:
        mapped["id"] = item["id"]
    return mapped


def _item_has_result(item: dict[str, Any]) -> bool:
    if item.get("output") not in (None, ""):
        return True
    if item.get("stdout") not in (None, "") or item.get("stderr") not in (None, ""):
        return True
    return item.get("exit_code") is not None


def _index_tool_calls(items: list[Any]) -> dict[str, tuple[str | None, str | None]]:
    index: dict[str, tuple[str | None, str | None]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        call_id = _call_id_of(item)
        if not call_id:
            continue
        item_type = str(item.get("type") or "")
        name = item.get("name") if isinstance(item.get("name"), str) else None
        if item_type in _SHELL_CALL_TYPES:
            name = name or _SHELL_CALL_TYPES[item_type]
        elif item_type == "apply_patch_call":
            name = name or "apply_patch"
        elif item_type == "computer_call":
            name = name or "computer"
        command = _command_from_item(item)
        if name or command:
            index[call_id] = (name, command)
    return index


def _expand_call_with_output(
    call: dict[str, Any] | None,
    item: dict[str, Any],
    *,
    name: str | None,
    command: str | None,
) -> list[dict[str, Any]]:
    if call is None:
        return []
    if not _item_has_result(item):
        return [call]
    output = _tool_output_to_function_output(item, name=name, command=command)
    if output is None:
        return [call]
    return [call, output, *_user_image_messages_from(item.get("output"))]


def _image_url_from(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if not isinstance(value, dict):
        return None
    for key in ("url", "image_url"):
        inner = value.get(key)
        if isinstance(inner, str) and inner.strip():
            return inner.strip()
        if isinstance(inner, dict):
            url = inner.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _image_detail_from(block: dict[str, Any]) -> str | None:
    candidates: list[Any] = [block.get("detail")]
    nested = block.get("image_url")
    if isinstance(nested, dict):
        candidates.append(nested.get("detail"))
    for value in candidates:
        if isinstance(value, str) and value in _IMAGE_DETAILS:
            return value
    return None


def _grok_image_usable(url: str) -> bool:
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return True
    return lowered.startswith(_GROK_IMAGE_DATA_PREFIXES)


def _responses_image_block(url: str, detail: str | None) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "input_image", "image_url": url}
    if detail in _IMAGE_DETAILS:
        block["detail"] = detail
    return block


def _chat_image_block(url: str, detail: str | None) -> dict[str, Any]:
    image: dict[str, Any] = {"url": url}
    if detail in _IMAGE_DETAILS:
        image["detail"] = detail
    return {"type": "image_url", "image_url": image}


def _omitted_image_text(reason: str, *, output: bool = False) -> dict[str, str]:
    return {
        "type": "output_text" if output else "input_text",
        "text": f"[image omitted: {reason}]",
    }


def _sanitize_grok_content_block(block: Any, *, output: bool) -> list[dict[str, Any]]:
    if isinstance(block, str):
        return [{"type": "output_text" if output else "input_text", "text": block}]
    if not isinstance(block, dict):
        return []
    block_type = str(block.get("type") or "")
    if block_type in _TEXT_CONTENT_TYPES or not block_type and isinstance(block.get("text"), str):
        text = block.get("text")
        if not isinstance(text, str):
            text = block.get("content") if isinstance(block.get("content"), str) else None
        if not isinstance(text, str):
            return []
        if block_type == "output_text" or output:
            return [{"type": "output_text", "text": text}]
        return [{"type": "input_text", "text": text}]
    looks_like_image = (
        block_type in _IMAGE_CONTENT_TYPES
        or block_type == "computer_screenshot"
        or "image_url" in block
        or isinstance(block.get("file_id"), str)
    )
    if looks_like_image:
        url = _image_url_from(block.get("image_url")) or _image_url_from(block.get("url"))
        if not url:
            return [_omitted_image_text("unresolved file", output=output)]
        if not _grok_image_usable(url):
            return [_omitted_image_text("unsupported format", output=output)]
        return [_responses_image_block(url, _image_detail_from(block))]
    return []


def _sanitize_grok_message_item(item: dict[str, Any]) -> dict[str, Any]:
    out = dict(item)
    if not out.get("type"):
        out["type"] = "message"
    content = out.get("content")
    output = str(out.get("role") or "") == "assistant"
    if isinstance(content, str):
        return out
    if not isinstance(content, list):
        return out
    blocks: list[dict[str, Any]] = []
    for block in content:
        blocks.extend(_sanitize_grok_content_block(block, output=output))
    out["content"] = blocks or [{
        "type": "output_text" if output else "input_text",
        "text": "",
    }]
    return out


def _collect_image_refs(value: Any) -> list[tuple[str, str | None]]:
    found: list[tuple[str, str | None]] = []
    seen: set[str] = set()

    def add(url: str | None, detail: str | None) -> None:
        if not url or url in seen or not _grok_image_usable(url):
            return
        seen.add(url)
        found.append((url, detail if detail in _IMAGE_DETAILS else None))

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        block_type = str(node.get("type") or "")
        if (
            block_type in _IMAGE_CONTENT_TYPES
            or block_type == "computer_screenshot"
            or isinstance(node.get("image_url"), (str, dict))
        ):
            add(
                _image_url_from(node.get("image_url")) or _image_url_from(node.get("url")),
                _image_detail_from(node),
            )
            if block_type in _IMAGE_CONTENT_TYPES or block_type == "computer_screenshot":
                return
        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child)

    walk(value)
    return found


def _user_image_messages_from(value: Any) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for url, detail in _collect_image_refs(value):
        messages.append(
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "[image from tool result]"},
                    _responses_image_block(url, detail or "high"),
                ],
            }
        )
    return messages


def _sanitize_grok_chat_content(content: Any) -> Any:
    if isinstance(content, str) or content is None:
        return content
    if not isinstance(content, list):
        return content
    blocks: list[dict[str, Any]] = []
    for block in content:
        for item in _sanitize_grok_content_block(block, output=False):
            if item.get("type") == "input_image":
                url = item.get("image_url")
                if isinstance(url, str):
                    blocks.append(_chat_image_block(url, item.get("detail") if isinstance(item.get("detail"), str) else None))
                continue
            text = item.get("text")
            if item.get("type") in {"input_text", "output_text"} and isinstance(text, str):
                blocks.append({"type": "text", "text": text})
    return blocks or content


def _sanitize_grok_chat_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    out = dict(message)
    out["content"] = _sanitize_grok_chat_content(out.get("content"))
    return out


def sanitize_grok_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Codex/OpenAI vision blocks onto Grok Chat Completions image_url parts."""
    out = dict(payload)
    messages = out.get("messages")
    if isinstance(messages, list):
        out["messages"] = [_sanitize_grok_chat_message(item) for item in messages]
    tools = out.get("tools")
    if isinstance(tools, list) and tools:
        mapped_choice = _map_grok_tool_choice(out.get("tool_choice"), tools)
        if mapped_choice is None:
            out.pop("tool_choice", None)
        else:
            out["tool_choice"] = mapped_choice
    else:
        out.pop("tool_choice", None)
    return out


def _sanitize_grok_input_item(
    item: dict[str, Any],
    call_index: dict[str, tuple[str | None, str | None]],
) -> list[dict[str, Any]]:
    item_type = str(item.get("type") or "")
    call_id = _call_id_of(item)
    indexed_name, indexed_command = call_index.get(call_id, (None, None)) if call_id else (None, None)
    if item_type in _DROPPED_INPUT_TYPES:
        return []
    if item_type == "compaction_trigger":
        return []
    if item_type in _COMPACTION_ITEM_TYPES:
        mapped = _compaction_to_message(item)
        return [mapped] if mapped else []
    if item_type == "tool_search_call":
        mapped = _tool_search_call_to_function_call(item)
        return [mapped] if mapped else []
    if item_type == "tool_search_output":
        mapped = _tool_search_output_to_function_output(item)
        return [mapped] if mapped else []
    if item_type == "apply_patch_call":
        return _expand_call_with_output(
            _patch_call_to_function_call(item),
            item,
            name=indexed_name or "apply_patch",
            command=None,
        )
    if item_type == "custom_tool_call":
        name = item.get("name") if isinstance(item.get("name"), str) else None
        if name in _PATCH_TOOL_NAMES:
            return _expand_call_with_output(
                _patch_call_to_function_call(item),
                item,
                name=name,
                command=None,
            )
        if name in _SHELL_TOOL_NAMES:
            return _expand_call_with_output(
                _custom_call_to_function_call(item),
                item,
                name=name,
                command=_command_from_item(item),
            )
        mapped = _custom_call_to_function_call(item)
        return [mapped] if mapped else []
    if item_type == "function_call":
        mapped = _normalize_function_call(item)
        return _expand_call_with_output(
            mapped,
            item,
            name=indexed_name or (mapped.get("name") if mapped else None),
            command=indexed_command or _command_from_item(item),
        ) if mapped and _item_has_result(item) else ([mapped] if mapped else [])
    if item_type in _SHELL_CALL_TYPES:
        name = _SHELL_CALL_TYPES[item_type]
        return _expand_call_with_output(
            _shell_call_to_function_call(item, name),
            item,
            name=indexed_name or name,
            command=indexed_command or _command_from_item(item),
        )
    if item_type == "computer_call":
        return _expand_call_with_output(
            _computer_call_to_function_call(item),
            item,
            name=indexed_name or "computer",
            command=None,
        )
    if item_type == "mcp_call":
        mapped = _mcp_call_to_function_call(item)
        return _expand_call_with_output(
            mapped,
            item,
            name=indexed_name or (mapped.get("name") if mapped else None),
            command=None,
        ) if mapped else []
    if item_type in _TOOL_OUTPUT_TYPES:
        mapped = _tool_output_to_function_output(
            item,
            name=indexed_name,
            command=indexed_command,
        )
        items = [mapped] if mapped else []
        items.extend(_user_image_messages_from(item.get("output")))
        return items
    if item_type == "reasoning":
        return [_sanitize_reasoning_item(item)]
    if not item_type or item_type == "message":
        return [_sanitize_grok_message_item(item)]
    if item_type in _GROK_INPUT_ITEM_TYPES:
        return [dict(item)]
    if call_id and (item.get("name") or item.get("action") or item.get("command")):
        mapped = _normalize_function_call(item) or _shell_call_to_function_call(
            item, str(item.get("name") or "shell_command")
        )
        return _expand_call_with_output(
            mapped,
            item,
            name=indexed_name,
            command=indexed_command or _command_from_item(item),
        ) if mapped else []
    if call_id and _item_has_result(item):
        mapped = _tool_output_to_function_output(
            item, name=indexed_name, command=indexed_command
        )
        items = [mapped] if mapped else []
        items.extend(_user_image_messages_from(item.get("output")))
        return items
    return []


def _pair_function_calls(items: list[Any]) -> list[Any]:
    pending: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id and call_id not in pending:
            pending[call_id] = item
    used: set[str] = set()
    paired: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            paired.append(item)
            continue
        if item.get("type") == "function_call_output":
            continue
        paired.append(item)
        if item.get("type") == "function_call":
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id in pending:
                paired.append(pending[call_id])
                used.add(call_id)
    for call_id, output in pending.items():
        if call_id not in used:
            paired.append(output)
    return paired


def _default_parameters(name: str, tool_type: str) -> dict[str, Any]:
    if name in _EXEC_COMMAND_NAMES:
        return dict(_EXEC_COMMAND_PARAMETERS)
    if name in _PATCH_TOOL_NAMES or (
        tool_type in _FREEFORM_TOOL_TYPES and name not in _COMPUTER_TOOL_NAMES
    ):
        return dict(_FREEFORM_PARAMETERS)
    if name in _SHELL_TOOL_NAMES or tool_type in _SHELL_TOOL_NAMES:
        return dict(_SHELL_PARAMETERS)
    if name in _COMPUTER_TOOL_NAMES or tool_type in _COMPUTER_TOOL_NAMES:
        return dict(_COMPUTER_PARAMETERS)
    return {"type": "object", "properties": {}}


def _map_grok_tool(tool: Any) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(tool, dict):
        return None, False
    item = dict(tool)
    tool_type = str(item.get("type") or "function")
    if tool_type == "namespace":
        return None, False
    if tool_type == "tool_search" or is_tool_search_name(_tool_name(item, tool_type)):
        return (
            {
                "type": "function",
                "name": TOOL_SEARCH_WIRE_NAME,
                "description": item.get("description")
                if isinstance(item.get("description"), str) and item.get("description")
                else _TOOL_SEARCH_DESCRIPTION,
                "parameters": _TOOL_SEARCH_PARAMETERS,
            },
            False,
        )
    if tool_type in _GROK_BUILTIN_TOOLS:
        mapped_type = _GROK_BUILTIN_TOOLS[tool_type]
        mapped_builtin: dict[str, Any] = {"type": mapped_type}
        if mapped_type == "file_search":
            store_ids = item.get("vector_store_ids")
            if not isinstance(store_ids, list):
                return None, False
            keep_ids = [value for value in store_ids if isinstance(value, str) and value]
            if not keep_ids:
                return None, False
            mapped_builtin["vector_store_ids"] = keep_ids
        return mapped_builtin, False
    name = _tool_name(item, tool_type)
    if not name:
        return None, False
    function = item.get("function")
    if isinstance(function, dict):
        for field in ("description", "parameters", "strict"):
            if field in function and field not in item:
                item[field] = function[field]
    freeform = tool_type in _FREEFORM_TOOL_TYPES or (
        name in _PATCH_TOOL_NAMES and tool_type != "function"
    )
    mapped: dict[str, Any] = {"type": "function", "name": name}
    description = item.get("description")
    parameters = item.get("parameters")
    empty_parameters = _parameters_empty(parameters)
    if name in _SHELL_TOOL_NAMES:
        text = description.strip() if isinstance(description, str) and description.strip() else ""
        mapped["description"] = (
            text + _SHELL_RESULT_NOTE if text and "[exit code:" not in text else (text or _SHELL_DESCRIPTION)
        )
    elif isinstance(description, str) and description:
        mapped["description"] = description
    elif empty_parameters and name in _PATCH_TOOL_NAMES:
        mapped["description"] = _APPLY_PATCH_DESCRIPTION
    elif empty_parameters and name in _COMPUTER_TOOL_NAMES:
        mapped["description"] = _COMPUTER_DESCRIPTION
    if empty_parameters:
        mapped["parameters"] = _default_parameters(name, tool_type)
    else:
        mapped["parameters"] = _normalize_tool_parameters(
            parameters, name=name, tool_type=tool_type
        )
    if "strict" in item:
        mapped["strict"] = item["strict"]
    return mapped, freeform


def _map_grok_tool_choice(choice: Any, tools: list[dict[str, Any]]) -> Any:
    """Grok ModelToolChoice only accepts auto|required|none strings, not objects."""
    if not tools:
        return None
    if choice is None:
        return None
    if isinstance(choice, str) and choice in {"auto", "required", "none"}:
        return choice
    if isinstance(choice, dict):
        mode = str(choice.get("type") or "").strip().lower()
        if mode in {"auto", "required", "none"}:
            return mode
        return "required"
    if choice:
        return "required"
    return "auto"


def _record_namespace(tool: dict[str, Any], namespace: str, namespace_map: dict[str, str]) -> None:
    name = _tool_name(tool, str(tool.get("type") or "function"))
    if isinstance(name, str) and name:
        namespace_map[name] = namespace


def _expand_raw_tools(
    raw_tools: list[Any], namespace_map: dict[str, str] | None = None
) -> list[Any]:
    expanded: list[Any] = []
    queue: list[tuple[Any, str | None]] = [(tool, None) for tool in raw_tools]
    while queue:
        tool, namespace = queue.pop(0)
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "")
        if tool_type == "namespace":
            ns = tool.get("name") if isinstance(tool.get("name"), str) else namespace
            inner = _collect_tool_list(tool.get("tools"))
            if isinstance(ns, str) and ns and namespace_map is not None:
                for child in inner:
                    if isinstance(child, dict):
                        _record_namespace(child, ns, namespace_map)
            queue[0:0] = [(child, ns if isinstance(ns, str) else None) for child in inner]
            continue
        if isinstance(namespace, str) and namespace and namespace_map is not None:
            _record_namespace(tool, namespace, namespace_map)
        expanded.append(tool)
    return expanded


def _preview_output(value: Any) -> str:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) > 96:
            return f"str:{len(encoded)}"
        return "str:" + encoded.decode("ascii", "backslashreplace")[:80]
    if isinstance(value, list):
        return f"list:{len(value)}"
    if isinstance(value, dict):
        keys = ",".join(sorted(str(key) for key in list(value)[:6]))
        return f"dict:{keys}"
    return type(value).__name__


def _type_counts(items: list[Any]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        key = str(item.get("type") or "other") if isinstance(item, dict) else "other"
        counts[key] = counts.get(key, 0) + 1
    return ",".join(f"{name}:{count}" for name, count in sorted(counts.items()))


def sanitize_grok_responses(payload: dict[str, Any]) -> GrokSanitizeResult:
    """Map Codex Responses onto xAI Responses: tools, history items, and freeform names."""
    compact_v2 = compact_v2_requested(payload)
    out = {key: value for key, value in payload.items() if key in _GROK_RESPONSE_FIELDS}
    include = _sanitize_grok_include(out.get("include"))
    if include:
        out["include"] = include
    else:
        out.pop("include", None)

    raw_tools: list[Any] = []
    existing_tools = out.get("tools")
    if isinstance(existing_tools, list):
        raw_tools.extend(existing_tools)

    dropped = 0
    empty_stdout = 0
    result_stats: list[str] = []
    namespace_map: dict[str, str] = {}
    items = out.get("input")
    if isinstance(items, list):
        in_types = _type_counts(items)
        call_index = _index_tool_calls(items)
        kept: list[Any] = []
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            item_type = str(item.get("type") or "")
            if item_type == "additional_tools":
                raw_tools.extend(_collect_tool_list(item.get("tools")))
                raw_tools.extend(_collect_tool_list(item.get("additional_tools")))
                continue
            if item_type == "tool_search_output":
                raw_tools.extend(_discovered_tools_from_item(item))
            if item_type in _TOOL_OUTPUT_TYPES or _item_has_result(item):
                parsed = _parse_item_result(item)
                call_id = _call_id_of(item)
                indexed_name = call_index.get(call_id, (None, None))[0] if call_id else None
                if indexed_name in _SHELL_TOOL_NAMES:
                    parsed.kind = "shell"
                in_len = len((parsed.stdout or "").encode("utf-8"))
                result_stats.append(
                    f"{indexed_name or item_type}:{parsed.kind}:{in_len}:{_preview_output(item.get('output'))}"
                )
                if parsed.kind == "shell" and not (parsed.stdout or "").strip():
                    empty_stdout += 1
            expanded = _sanitize_grok_input_item(item, call_index)
            if not expanded:
                dropped += 1
                continue
            kept.extend(expanded)
        out["input"] = _pair_function_calls(kept)
    else:
        in_types = ""

    model = str(out.get("model") or "")
    if "reasoning" in out:
        effort = None
        current = out.get("reasoning")
        if isinstance(current, dict):
            effort = current.get("effort")
        reasoning = {"summary": "concise"}
        if grok_model_supports_reasoning_effort(model):
            reasoning["effort"] = map_grok_reasoning_effort(effort)
        out["reasoning"] = reasoning

    tools: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    freeform_names: set[str] = set()
    tool_search_wire_name: str | None = None
    for tool in _expand_raw_tools(raw_tools, namespace_map):
        mapped, freeform = _map_grok_tool(tool)
        if mapped is None:
            continue
        key = (str(mapped.get("type") or ""), str(mapped.get("name") or ""))
        if key in seen:
            continue
        seen.add(key)
        tools.append(mapped)
        name = mapped.get("name")
        if freeform and isinstance(name, str) and name:
            freeform_names.add(name)
        if isinstance(name, str) and name == TOOL_SEARCH_WIRE_NAME:
            tool_search_wire_name = TOOL_SEARCH_WIRE_NAME
    if tools:
        out["tools"] = tools
        mapped_choice = _map_grok_tool_choice(out.get("tool_choice"), tools)
        if mapped_choice is None:
            out.pop("tool_choice", None)
        else:
            out["tool_choice"] = mapped_choice
    else:
        out.pop("tools", None)
        out.pop("tool_choice", None)
        out.pop("parallel_tool_calls", None)
    tool_names = ",".join(
        str(tool.get("name") or tool.get("type") or "")
        for tool in tools
        if isinstance(tool, dict)
    )
    param_roots: dict[str, int] = {}
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        kind = _param_root_kind(tool.get("parameters"))
        param_roots[kind] = param_roots.get(kind, 0) + 1
    shell_mapped = sum(
        1
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or "") in _SHELL_TOOL_NAMES
    )
    logger.info(
        "grok_sanitize in_types=%s out_types=%s empty_stdout=%s dropped=%s tools=%s "
        "tools_n=%s shell_mapped=%s param_roots=%s freeform=%s compact=%s results=%s",
        in_types,
        _type_counts(out["input"]) if isinstance(out.get("input"), list) else "",
        empty_stdout,
        dropped,
        tool_names,
        len(tools),
        shell_mapped,
        ",".join(f"{name}:{count}" for name, count in sorted(param_roots.items())),
        ",".join(sorted(freeform_names)),
        int(compact_v2),
        ",".join(result_stats[:12]),
    )
    return GrokSanitizeResult(
        payload=out,
        freeform_tool_names=frozenset(freeform_names),
        tool_search_wire_name=tool_search_wire_name,
        namespace_map=namespace_map,
        compact_v2=compact_v2,
    )


def sanitize_grok_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Map Codex Responses onto xAI Responses: additional_tools → tools, keep Grok-native items."""
    return sanitize_grok_responses(payload).payload


def rewrite_grok_output_item(
    item: dict[str, Any], spec: GrokRewriteSpec | frozenset[str]
) -> dict[str, Any]:
    rewrite = _as_rewrite_spec(spec)
    if item.get("type") != "function_call":
        return item
    name = item.get("name")
    if isinstance(name, str) and name in rewrite.freeform_tool_names:
        rewritten = {key: value for key, value in item.items() if key != "arguments"}
        rewritten["type"] = "custom_tool_call"
        rewritten["input"] = extract_freeform_input(item.get("arguments"))
        return rewritten
    if isinstance(name, str) and rewrite.tool_search_wire_name and name == rewrite.tool_search_wire_name:
        rewritten = {key: value for key, value in item.items() if key not in {"arguments", "name"}}
        rewritten["type"] = "tool_search_call"
        rewritten["execution"] = "client"
        rewritten["arguments"] = normalize_tool_search_arguments(item.get("arguments"))
        if "status" not in rewritten:
            rewritten["status"] = "completed"
        return rewritten
    if isinstance(name, str) and name in rewrite.namespace_map:
        rewritten = dict(item)
        rewritten["namespace"] = rewrite.namespace_map[name]
        return rewritten
    return item


def rewrite_grok_completed_response(
    response: dict[str, Any], spec: GrokRewriteSpec | frozenset[str]
) -> dict[str, Any]:
    rewrite = _as_rewrite_spec(spec)
    if not rewrite.active():
        return response
    output = response.get("output")
    if not isinstance(output, list):
        return response
    rewritten = dict(response)
    rewritten["output"] = [
        rewrite_grok_output_item(item, rewrite) if isinstance(item, dict) else item
        for item in output
    ]
    return rewritten


def rewrite_grok_codex_sse_payload(
    event_type: str,
    payload: dict[str, Any],
    spec: GrokRewriteSpec | frozenset[str],
    state: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    rewrite = _as_rewrite_spec(spec)
    if not rewrite.active():
        return [(event_type, payload)]

    tracked: dict[str, str] = state.setdefault("freeform_items", {})
    search_ids: set[str] = state.setdefault("tool_search_items", set())
    full_input_ids: set[str] = state.setdefault("full_input_ids", set())

    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = payload.get("item")
        if isinstance(item, dict):
            rewritten_item = rewrite_grok_output_item(item, rewrite)
            item_id = rewritten_item.get("id")
            name = rewritten_item.get("name")
            if (
                rewritten_item.get("type") == "custom_tool_call"
                and isinstance(item_id, str)
                and isinstance(name, str)
            ):
                tracked[item_id] = name
                if event_type == "response.output_item.added" and rewritten_item.get("input"):
                    full_input_ids.add(item_id)
            if rewritten_item.get("type") == "tool_search_call" and isinstance(item_id, str):
                search_ids.add(item_id)
                if event_type == "response.output_item.added":
                    rewritten_item = dict(rewritten_item)
                    rewritten_item["status"] = "in_progress"
                    if not rewritten_item.get("arguments"):
                        rewritten_item["arguments"] = {}
            if rewritten_item is not item:
                rewritten = dict(payload)
                rewritten["item"] = rewritten_item
                return [(event_type, rewritten)]
        return [(event_type, payload)]

    if event_type == "response.function_call_arguments.delta":
        item_id = payload.get("item_id")
        if isinstance(item_id, str) and (item_id in tracked or item_id in search_ids):
            return []
        return [(event_type, payload)]

    if event_type == "response.function_call_arguments.done":
        item_id = payload.get("item_id")
        if isinstance(item_id, str) and item_id in search_ids:
            return []
        if not (isinstance(item_id, str) and item_id in tracked):
            return [(event_type, payload)]
        if item_id in full_input_ids:
            return []
        input_text = extract_freeform_input(payload.get("arguments"))
        delta: dict[str, Any] = {
            "type": "response.custom_tool_call_input.delta",
            "item_id": item_id,
            "delta": input_text,
        }
        done: dict[str, Any] = {
            "type": "response.custom_tool_call_input.done",
            "item_id": item_id,
            "input": input_text,
        }
        if "output_index" in payload:
            delta["output_index"] = payload["output_index"]
            done["output_index"] = payload["output_index"]
        return [
            ("response.custom_tool_call_input.delta", delta),
            ("response.custom_tool_call_input.done", done),
        ]

    if event_type in {"response.completed", "response.incomplete"}:
        response = payload.get("response")
        if isinstance(response, dict):
            rewritten_response = rewrite_grok_completed_response(response, rewrite)
            if rewritten_response is not response:
                rewritten = dict(payload)
                rewritten["response"] = rewritten_response
                return [(event_type, rewritten)]
        return [(event_type, payload)]

    return [(event_type, payload)]


def rewrite_grok_codex_sse_block(
    block: str,
    spec: GrokRewriteSpec | frozenset[str],
    state: dict[str, Any],
) -> list[str]:
    from app.providers.codex_protocol import parse_sse_event

    event, data = parse_sse_event(block)
    if not data or data == "[DONE]":
        return [block]
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return [block]
    if not isinstance(payload, dict):
        return [block]
    event_type = str(payload.get("type") or event)
    rewritten = rewrite_grok_codex_sse_payload(event_type, payload, spec, state)
    return [
        f"event: {new_event}\ndata: {json.dumps(new_payload, ensure_ascii=False)}"
        for new_event, new_payload in rewritten
    ]


class GrokAdapter:
    id = "grok"
    display_name = "Grok"
    capabilities = set(_GROK_CAPS)
    ready = True

    def parse_import(self, payload: Any, filename: str) -> list[ImportedAccount]:
        return parse_auth_payload(payload, source="pool", path=filename)

    def catalog(self) -> list[ModelInfo]:
        models = [
            ModelInfo(
                id=settings.grok_default_model,
                provider=self.id,
                type="text",
                capabilities=(CAP_RESPONSES, CAP_CHAT, CAP_STREAM),
                default=True,
                owned_by="x-ai",
            ),
            ModelInfo(
                id="gpt-reserve",
                provider=self.id,
                type="text",
                capabilities=(CAP_RESPONSES, CAP_CHAT, CAP_STREAM),
                owned_by="x-ai",
                alias_of=settings.grok_default_model,
            ),
            ModelInfo(
                id="grok-4.5",
                provider=self.id,
                type="text",
                capabilities=(CAP_RESPONSES, CAP_CHAT, CAP_STREAM),
                owned_by="x-ai",
            ),
        ]
        from app.providers.catalog_extra import extras_for

        known = {item.id for item in models}
        for extra in extras_for(self.id):
            if extra.model_id in known:
                continue
            known.add(extra.model_id)
            caps = (CAP_VIDEO,) if extra.model_type == "video" else (
                (CAP_IMAGE, CAP_IMAGE_EDIT) if extra.model_type == "image" else (CAP_RESPONSES, CAP_CHAT, CAP_STREAM)
            )
            models.append(
                ModelInfo(
                    id=extra.model_id,
                    provider=self.id,
                    type=extra.model_type,
                    capabilities=caps,
                    owned_by="x-ai",
                )
            )
        models.extend([
            ModelInfo(
                id=settings.grok_image_model,
                provider=self.id,
                type="image",
                capabilities=(CAP_IMAGE, CAP_IMAGE_EDIT),
                default=True,
                owned_by="x-ai",
            ),
            ModelInfo(
                id=settings.grok_video_model,
                provider=self.id,
                type="video",
                capabilities=(CAP_VIDEO,),
                default=True,
                owned_by="x-ai",
            ),
        ])
        if settings.grok_video_edit_model != settings.grok_video_model:
            models.append(
                ModelInfo(
                    id=settings.grok_video_edit_model,
                    provider=self.id,
                    type="video",
                    capabilities=(CAP_VIDEO,),
                    owned_by="x-ai",
                )
            )
        return models


_GROK_IMAGE_REF_TYPE = "image_url"
_GROK_MAX_EDIT_IMAGES = 5
_GROK_IMAGE_KEEP = (
    "model",
    "prompt",
    "n",
    "user",
    "response_format",
    "aspect_ratio",
    "resolution",
    "quality",
)
_GROK_SIZE_ASPECT = {
    "256x256": "1:1",
    "512x512": "1:1",
    "1024x1024": "1:1",
    "1024x768": "4:3",
    "768x1024": "3:4",
    "1792x1024": "16:9",
    "1024x1792": "9:16",
    "1536x1024": "3:2",
    "1024x1536": "2:3",
}
_GROK_ASPECT_PAIRS = (
    (1.0, 1.0),
    (3.0, 4.0),
    (4.0, 3.0),
    (9.0, 16.0),
    (16.0, 9.0),
    (2.0, 3.0),
    (3.0, 2.0),
    (9.0, 19.5),
    (19.5, 9.0),
    (9.0, 20.0),
    (20.0, 9.0),
    (1.0, 2.0),
    (2.0, 1.0),
    (21.0, 9.0),
    (5.0, 2.0),
)
_GROK_QUALITY_MAP = {
    "hd": "medium",
    "high": "medium",
    "standard": "low",
    "low": "low",
    "medium": "medium",
    "auto": "auto",
}


def _grok_image_ref(value: Any) -> dict[str, str] | None:
    if isinstance(value, str):
        url = value.strip()
        return {"url": url, "type": _GROK_IMAGE_REF_TYPE} if url else None
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    if not isinstance(url, str) or not url.strip():
        nested = value.get("image_url")
        if isinstance(nested, str) and nested.strip():
            url = nested.strip()
        elif isinstance(nested, dict):
            inner = nested.get("url")
            url = inner.strip() if isinstance(inner, str) and inner.strip() else ""
        else:
            url = ""
    else:
        url = url.strip()
    if not url:
        return None
    return {"url": url, "type": _GROK_IMAGE_REF_TYPE}


def _iter_grok_image_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _format_grok_aspect(width: float, height: float) -> str:
    def _part(value: float) -> str:
        return str(int(value)) if value == int(value) else str(value)

    return f"{_part(width)}:{_part(height)}"


def _grok_aspect_ratio_from_size(size: Any) -> str | None:
    if not isinstance(size, str) or not size.strip():
        return None
    text = size.strip().lower()
    if text == "auto":
        return "auto"
    mapped = _GROK_SIZE_ASPECT.get(text)
    if mapped:
        return mapped
    match = re.match(r"^(\d+)\s*x\s*(\d+)$", text)
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    best_width, best_height = min(
        _GROK_ASPECT_PAIRS, key=lambda pair: abs(pair[0] / pair[1] - ratio)
    )
    return _format_grok_aspect(best_width, best_height)


def shape_grok_image_body(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenAI Images JSON into Grok's object image/mask shape."""
    shaped: dict[str, Any] = {}
    for key in _GROK_IMAGE_KEEP:
        if key in body:
            shaped[key] = body[key]
    if "aspect_ratio" not in shaped:
        mapped_ratio = _grok_aspect_ratio_from_size(body.get("size"))
        if mapped_ratio:
            shaped["aspect_ratio"] = mapped_ratio
    quality = shaped.get("quality")
    if isinstance(quality, str):
        mapped_quality = _GROK_QUALITY_MAP.get(quality.strip().lower())
        if mapped_quality:
            shaped["quality"] = mapped_quality
        else:
            shaped.pop("quality", None)
    elif "quality" in shaped:
        shaped.pop("quality", None)
    resolution = shaped.get("resolution")
    if isinstance(resolution, str):
        normalized = resolution.strip().lower()
        if normalized in {"1k", "2k"}:
            shaped["resolution"] = normalized
        else:
            shaped.pop("resolution", None)
    elif "resolution" in shaped:
        shaped.pop("resolution", None)
    count = shaped.get("n")
    if "n" in shaped and not isinstance(count, int):
        shaped.pop("n", None)
    if kind in {"edit", "edits"}:
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        for key in ("image", "images"):
            for item in _iter_grok_image_values(body.get(key)):
                ref = _grok_image_ref(item)
                if ref is None or ref["url"] in seen:
                    continue
                seen.add(ref["url"])
                refs.append(ref)
                if len(refs) >= _GROK_MAX_EDIT_IMAGES:
                    break
            if len(refs) >= _GROK_MAX_EDIT_IMAGES:
                break
        if len(refs) == 1:
            shaped["image"] = refs[0]
        elif refs:
            shaped["images"] = refs
        mask_ref = _grok_image_ref(body.get("mask"))
        if mask_ref is not None:
            shaped["mask"] = mask_ref
    if not str(shaped.get("response_format") or "").strip():
        shaped["response_format"] = "b64_json"
    return shaped


def _quota_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        return _quota_number(value.get("val") if value.get("val") is not None else value.get("value"))
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_quota_snapshot(billing: Any) -> dict[str, Any] | None:
    """Map CLI billing JSON into the Codex-shaped quota window used by the admin bar."""
    if not isinstance(billing, dict):
        return None
    config = billing["config"] if isinstance(billing.get("config"), dict) else billing
    used = _quota_number(config.get("creditUsagePercent"))
    remaining_amount = None
    total_amount = None
    if used is None:
        cap_val = _quota_number(config.get("onDemandCap"))
        used_val = _quota_number(config.get("onDemandUsed"))
        if cap_val is not None and cap_val > 0 and used_val is not None:
            used = used_val / cap_val * 100.0
            remaining_amount = max(0.0, cap_val - used_val)
            total_amount = cap_val
    if used is None:
        credits = config.get("credits") if isinstance(config.get("credits"), dict) else {}
        remaining_amount = _quota_number(
            config.get("remainingCredits")
            or config.get("creditsRemaining")
            or credits.get("remaining")
            or credits.get("remain")
        )
        total_amount = _quota_number(
            config.get("totalCredits")
            or config.get("creditLimit")
            or config.get("creditsLimit")
            or credits.get("limit")
            or credits.get("total")
            or credits.get("cap")
        )
        consumed = _quota_number(config.get("usedCredits") or credits.get("used"))
        if remaining_amount is not None and total_amount is not None and total_amount > 0:
            used = max(0.0, min(100.0, (1.0 - remaining_amount / total_amount) * 100.0))
        elif consumed is not None and total_amount is not None and total_amount > 0:
            used = consumed / total_amount * 100.0
            remaining_amount = max(0.0, total_amount - consumed)
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    if used is None and (period or config.get("billingPeriodEnd") or config.get("billingPeriodStart")):
        used = 0.0
    if used is None:
        return None
    used = max(0.0, min(100.0, used))
    end = _parse_datetime(period.get("end") or config.get("billingPeriodEnd"))
    start = _parse_datetime(period.get("start") or config.get("billingPeriodStart"))
    resets_at = int(end.timestamp()) if end is not None else None
    start_at = int(start.timestamp()) if start is not None else None
    window_minutes = None
    if start_at is not None and resets_at is not None and resets_at > start_at:
        window_minutes = max(0, round((resets_at - start_at) / 60))
    primary = {
        "used_percent": used,
        "remaining_percent": max(0.0, 100.0 - used),
        "window_minutes": window_minutes,
        "resets_at": resets_at,
    }
    if remaining_amount is not None:
        primary["remaining_amount"] = remaining_amount
    if total_amount is not None:
        primary["total_amount"] = total_amount
    return {
        "plan_type": "unknown",
        "limits": [
            {
                "limit_id": "grok",
                "limit_name": None,
                "primary": primary,
                "secondary": None,
            }
        ],
        "next_reset_at": resets_at,
        "reset_credits": {"available_count": 0, "credits": []},
        "quota_kind": "credits",
        "message": None,
    }
