from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

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
    ImportedAccount,
    ModelInfo,
)

AuthMode = Literal["chatgpt", "api"]
_REFRESH_WINDOW = dt.timedelta(minutes=5)
_LAST_REFRESH_MAX_AGE = dt.timedelta(days=8)
OAUTH_AUTH_URL = "https://auth.openai.com/oauth/authorize"
OAUTH_CALLBACK_PORT = 1455
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
OAUTH_SCOPES = "openid email profile offline_access"
OAUTH_BIND_HOSTS = ("127.0.0.1", "::1")
_CODEX_CLIENT_VERSION_RE = re.compile(
    r"(?:codex_exec|codex_cli_rs|codex|codex desktop)[/ ](?P<version>[0-9][0-9A-Za-z._-]*)",
    re.IGNORECASE,
)


def client_version_from_user_agent(user_agent: str | None) -> str | None:
    """Extract a Codex client version without retaining the full user agent."""
    match = _CODEX_CLIENT_VERSION_RE.search(user_agent or "")
    if not match:
        return None
    return match.group("version")[:80]


@dataclass(frozen=True, slots=True)
class CodexCredentials:
    auth_mode: AuthMode
    access_token: str
    account_id: str | None
    refresh_token: str | None
    expires_at: dt.datetime | None
    last_refresh: dt.datetime | None
    source: str
    path: Any
    raw: dict[str, Any]


def decode_jwt_claims(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return None
    padding = "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _parse_datetime(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _account_id_from_claims(claims: dict[str, Any] | None) -> str | None:
    auth = claims.get("https://api.openai.com/auth") if claims else None
    account_id = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return account_id if isinstance(account_id, str) and account_id else None


def _expiry_from_claims(claims: dict[str, Any] | None) -> dt.datetime | None:
    if not claims or not isinstance(claims.get("exp"), (int, float)):
        return None
    return dt.datetime.fromtimestamp(int(claims["exp"]), tz=dt.UTC)


def parse_auth_payload(payload: Any, *, source: str, path: Any) -> CodexCredentials:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="Codex credential payload must be a JSON object")
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    stored_account_id = tokens.get("account_id") or payload.get("account_id")
    api_key = payload.get("OPENAI_API_KEY") or payload.get("openai_api_key")
    if isinstance(access_token, str) and access_token:
        claims = decode_jwt_claims(access_token)
        account_id = (
            stored_account_id
            if isinstance(stored_account_id, str) and stored_account_id
            else _account_id_from_claims(claims)
        )
        return CodexCredentials(
            auth_mode="chatgpt",
            access_token=access_token,
            account_id=account_id,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            expires_at=_expiry_from_claims(claims),
            last_refresh=_parse_datetime(payload.get("last_refresh")),
            source=source,
            path=path,
            raw=payload,
        )
    if isinstance(api_key, str) and api_key:
        return CodexCredentials(
            auth_mode="api",
            access_token=api_key,
            account_id=None,
            refresh_token=None,
            expires_at=None,
            last_refresh=_parse_datetime(payload.get("last_refresh")),
            source=source,
            path=path,
            raw=payload,
        )
    raise HTTPException(status_code=503, detail="No Codex access token was found in auth.json")


def should_refresh(credentials: CodexCredentials, *, now: dt.datetime | None = None) -> bool:
    if credentials.auth_mode != "chatgpt" or not credentials.refresh_token:
        return False
    current = now or dt.datetime.now(dt.UTC)
    if credentials.expires_at is not None:
        return credentials.expires_at - current <= _REFRESH_WINDOW
    if credentials.last_refresh is None:
        return True
    return current - credentials.last_refresh >= _LAST_REFRESH_MAX_AGE


def upstream_base_url(auth_mode: str) -> str:
    if auth_mode == "api":
        return settings.codex_platform_upstream_url.rstrip("/")
    return settings.codex_upstream_url.rstrip("/")


def upstream_client_params() -> dict[str, str]:
    """ChatGPT Codex requires this global client version on catalog and inference calls."""
    return {"client_version": settings.codex_client_version}


def build_headers(
    credentials: CodexCredentials,
    *,
    accept: str = "text/event-stream",
    lite: bool = False,
    client_version: str | None = None,
) -> dict[str, str]:
    version = (client_version or "").strip() or settings.codex_client_version
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": accept,
        "OpenAI-Beta": "responses=v1",
        "originator": settings.codex_originator,
        "version": version,
        "session_id": str(uuid.uuid4()),
        "User-Agent": f"{settings.codex_originator}/{version} (transfer-station)",
    }
    if credentials.auth_mode == "chatgpt" and credentials.account_id:
        headers["ChatGPT-Account-Id"] = credentials.account_id
    if lite:
        headers["x-openai-internal-codex-responses-lite"] = "true"
    return headers


def _looks_like_html(text: str) -> bool:
    stripped = text.lstrip().lower()
    return stripped.startswith(("<!doctype", "<html")) or "<html" in stripped[:240]


def _upstream_message(response: httpx.Response) -> str | None:
    raw = response.text
    if _looks_like_html(raw):
        return None
    try:
        body = response.json()
    except json.JSONDecodeError:
        text = raw.strip()
        return text[:300] if text else None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()[:300]
    if isinstance(error, dict):
        for key in ("message", "detail", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:300]
    for key in ("detail", "message"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:300]
    return None


def upstream_error_metadata(response: httpx.Response) -> dict[str, str]:
    """Return bounded structural error fields without retaining the upstream message."""
    try:
        body = response.json()
    except json.JSONDecodeError:
        return {}
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return {}
    error = body["error"]
    metadata: dict[str, str] = {}
    for field in ("type", "code", "param"):
        value = error.get(field)
        if isinstance(value, str) and value:
            sanitized = "".join(
                character
                for character in value[:120]
                if character.isalnum() or character in "._-[]"
            )
            if sanitized:
                metadata[field] = sanitized
    return metadata


def map_upstream_status(status_code: int, message: str | None = None) -> tuple[int, str]:
    normalized = (message or "").lower()
    if status_code == 400 and any(
        marker in normalized for marker in ("capacity", "overloaded", "usage_limit_reached")
    ):
        return 429, "Codex capacity is temporarily unavailable; retry later"
    if status_code == 401:
        return 401, "Codex authentication expired; import a refreshed auth.json"
    if status_code == 403:
        detail = "Current Codex account is not allowed to use this model"
        return 403, f"{detail}: {message}" if message else detail
    if status_code == 429:
        return 429, "Codex rate limit reached; retry later"
    if status_code >= 500:
        return 502, f"Codex upstream is temporarily unavailable (HTTP {status_code})"
    if status_code >= 400:
        base = f"Codex upstream request failed (HTTP {status_code})"
        return status_code, f"{base}: {message}" if message else base
    return status_code, "ok"


async def refresh_credentials(
    credentials: CodexCredentials, client: httpx.AsyncClient | None = None
) -> CodexCredentials:
    if not credentials.refresh_token:
        raise HTTPException(status_code=401, detail="Codex authentication expired; import a refreshed auth.json")
    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.post(
            settings.codex_refresh_url,
            headers={"Content-Type": "application/json"},
            json={
                "client_id": settings.codex_client_id,
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
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
        raise HTTPException(status_code=504, detail="Codex token refresh timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Codex token endpoint") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=401, detail="Codex authentication expired; import a refreshed auth.json")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Codex token refresh returned invalid JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("access_token"), str):
        raise HTTPException(status_code=502, detail="Codex token refresh returned no access token")
    raw = dict(credentials.raw)
    tokens = dict(raw.get("tokens") or {})
    for key in ("access_token", "refresh_token", "id_token"):
        if isinstance(body.get(key), str) and body[key]:
            tokens[key] = body[key]
    raw["tokens"] = tokens
    raw["last_refresh"] = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    refreshed = parse_auth_payload(raw, source=credentials.source, path=credentials.path)
    _persist_refreshed(refreshed)
    return refreshed


def _persist_refreshed(credentials: CodexCredentials) -> None:
    path = Path(credentials.path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(credentials.raw, ensure_ascii=False), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def oauth_redirect_uri() -> str:
    return OAUTH_REDIRECT_URI


def build_oauth_auth_url(state: str, *, code_challenge: str, redirect_uri: str | None = None) -> str:
    params = {
        "client_id": settings.codex_client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri or OAUTH_REDIRECT_URI,
        "scope": OAUTH_SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return f"{OAUTH_AUTH_URL}?{urlencode(params)}"


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
        raise HTTPException(status_code=422, detail="Codex login did not return an authorization code")

    async def request(active_client: httpx.AsyncClient) -> httpx.Response:
        return await active_client.post(
            settings.codex_refresh_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": settings.codex_client_id,
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
        raise HTTPException(status_code=504, detail="Codex login timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Cannot reach Codex login endpoint") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=422, detail="Codex login could not exchange the authorization code")
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Codex login returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="Codex login returned invalid JSON")
    access = body.get("access_token")
    refresh = body.get("refresh_token")
    if not isinstance(access, str) or not access.strip():
        raise HTTPException(status_code=422, detail="Codex login did not return an access token")
    if not isinstance(refresh, str) or not refresh.strip():
        raise HTTPException(
            status_code=422,
            detail="Codex login did not return a refresh token. Click Allow on the consent screen and retry.",
        )
    id_token = body.get("id_token") if isinstance(body.get("id_token"), str) else None
    claims = decode_jwt_claims(access) or jwt_claims(id_token or "")
    account_id = _account_id_from_claims(claims)
    if not account_id:
        raise HTTPException(status_code=422, detail="Codex login did not return a ChatGPT account id")
    tokens: dict[str, Any] = {
        "access_token": access.strip(),
        "refresh_token": refresh.strip(),
        "account_id": account_id,
    }
    if isinstance(id_token, str) and id_token.strip():
        tokens["id_token"] = id_token.strip()
    return {
        "tokens": tokens,
        "last_refresh": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
    }


class CodexAdapter:
    id = "codex"
    display_name = "Codex"
    capabilities = {CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT}
    ready = True

    def parse_import(self, payload: Any, filename: str) -> list[ImportedAccount]:
        parsed = parse_auth_payload(payload, source="pool", path=filename)
        if parsed.auth_mode != "chatgpt" or not parsed.account_id:
            raise HTTPException(status_code=422, detail="Codex auth.json is missing a ChatGPT account_id")
        expires_at = (
            parsed.expires_at.isoformat().replace("+00:00", "Z")
            if parsed.expires_at is not None
            else None
        )
        account_id = parsed.account_id
        label = account_id if len(account_id) <= 8 else f"{account_id[:4]}…{account_id[-4:]}"
        return [
            ImportedAccount(
                account_id=account_id,
                payload=payload if isinstance(payload, dict) else {},
                expires_at=expires_at,
                label=label,
                auth_mode="oauth",
                capabilities=(CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT),
            )
        ]

    def catalog(self) -> list[ModelInfo]:
        text_caps = (CAP_RESPONSES, CAP_CHAT)
        image_caps = (CAP_IMAGE, CAP_IMAGE_EDIT)
        names = []
        seen: set[str] = set()
        for name in (
            settings.codex_default_model,
            "gpt-6",
            *sorted(settings.lite_models()),
            "gpt-5.5",
            settings.codex_image_model,
            "gpt-image-2.5",
            "gpt-image-2.5-sunburst",
            "gpt-image-2",
        ):
            if name and name not in seen:
                seen.add(name)
                names.append(name)
        models: list[ModelInfo] = []
        for name in names:
            is_image = name.startswith("gpt-image-")
            models.append(
                ModelInfo(
                    id=name,
                    provider=self.id,
                    type="image" if is_image else "text",
                    capabilities=image_caps if is_image else text_caps,
                    default=name in {settings.codex_default_model, settings.codex_image_model},
                    owned_by="openai",
                    alias_of=(
                        "gpt-6-astra"
                        if name == "gpt-6"
                        else "gpt-image-2.5-flare"
                        if name == "gpt-image-2.5"
                        else None
                    ),
                )
            )
        from app.providers.catalog_extra import extras_for

        seen = {item.id for item in models}
        for extra in extras_for(self.id):
            if extra.model_id in seen:
                continue
            seen.add(extra.model_id)
            models.append(
                ModelInfo(
                    id=extra.model_id,
                    provider=self.id,
                    type=extra.model_type,
                    capabilities=image_caps if extra.model_type == "image" else text_caps,
                    owned_by="openai",
                )
            )
        return models
