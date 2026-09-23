"""Email verified self-service access, isolated from administrator sessions."""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import re
import secrets
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, field_validator

from app.billing import (
    BillingError,
    list_ledger,
    list_public_pricing,
    money2,
    redeem_card,
    usd_text,
    user_wallet,
)
from app.bundle import codex_bundle
from app.client_skills_pack import list_client_skills, zip_client_skill
from app.codex_gateway import GatewayError, codex_gateway
from app.config import settings
from app.desktop_recipes import DESKTOP_PROTOCOL, desktop_import_url, harness_catalog, public_v1_url
from app.store.gateway import gateway_store, iso_now, utc_now

router = APIRouter(prefix="/portal/api")
COOKIE = "ts_portal_session"


def _expiry(minutes: int) -> str:
    return (utc_now() + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _digest(value: str) -> str:
    return hmac.new(codex_gateway.secret, value.encode(), hashlib.sha256).hexdigest()


_CAPTCHA_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"


def _normalize_captcha(value: str) -> str:
    return (value or "").strip().upper()


def _ip(request: Request) -> str:
    return _digest("ip:" + (request.client.host if request.client else "unknown"))


def require_enabled() -> None:
    if not (settings.portal_enabled and settings.gateway_public_base_url.startswith("https://")
            and settings.portal_smtp_host and settings.portal_smtp_from):
        raise GatewayError(503, "User portal is not configured", code="portal_unavailable")


def _origin(request: Request) -> None:
    require_enabled()
    expected = urlsplit(settings.gateway_public_base_url)
    expected_origin = f"{expected.scheme}://{expected.netloc}"
    actual = request.headers.get("origin", "").rstrip("/")
    if (
        not actual
        or len(actual) != len(expected_origin)
        or not hmac.compare_digest(actual, expected_origin)
    ):
        raise GatewayError(403, "Same-origin request required", code="portal_origin_required")


class MailRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    email: str = Field(min_length=5, max_length=254)
    captcha_id: str = Field(min_length=16, max_length=64)
    captcha: str = Field(min_length=4, max_length=8)

    @field_validator("captcha", mode="before")
    @classmethod
    def _captcha_casefold(cls, value: Any) -> str:
        return _normalize_captcha(str(value or ""))


def json_ready(value: Any) -> Any:
    """MySQL SUM/COUNT come back as Decimal and crash JSONResponse."""
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    if hasattr(value, "as_tuple") and hasattr(value, "quantize"):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    return value


class VerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=16, max_length=64)
    email: str = Field(min_length=5, max_length=254)
    code: str = Field(min_length=6, max_length=6)
    device_id: str = Field(default="", max_length=64)


_DEVICE_RE = re.compile(r"^[0-9a-f]{64}$")
REWARD_ALREADY_CLAIMED = "这台电脑已经领取过新人注册奖励，本次不再发放。"


def _normalize_device(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if _DEVICE_RE.fullmatch(text) is None:
        raise GatewayError(422, "电脑标识无效", code="invalid_device_id")
    return text


def _email(value: str) -> str:
    address = value.strip().lower()
    if address.count("@") != 1 or len(address) > 254 or any(c.isspace() for c in address):
        raise GatewayError(422, "Invalid email address", code="invalid_email")
    domain = address.rsplit("@", 1)[1]
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise GatewayError(422, "Invalid email address", code="invalid_email")
    return address


def _captcha_png(answer: str) -> bytes:
    image = Image.new("RGB", (190, 66), "#e9f4f3")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=35)
    for _ in range(18):
        x, y = secrets.randbelow(190), secrets.randbelow(66)
        draw.line((x, y, min(189, x + secrets.randbelow(65)), min(65, y + secrets.randbelow(35))), fill="#8fb4b0", width=1)
    for index, char in enumerate(answer):
        draw.text((15 + index * 32, 8 + secrets.randbelow(10)), char, font=font, fill="#173c46")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@router.get("/captcha")
async def captcha(request: Request, response: Response) -> dict[str, str]:
    require_enabled()
    answer = "".join(secrets.choice(_CAPTCHA_ALPHABET) for _ in range(5))
    challenge_id = uuid.uuid4().hex
    now = iso_now()

    def save(db):
        db.execute("DELETE FROM portal_captchas WHERE expires_at<?", (now,))
        count = db.execute("SELECT COUNT(*) AS n FROM portal_captchas WHERE ip_hash=?", (_ip(request),)).fetchone()["n"]
        if count >= 40:
            return False
        db.execute(
            "INSERT INTO portal_captchas(id,answer_hash,ip_hash,expires_at) VALUES(?,?,?,?)",
            (challenge_id, _digest("captcha:" + challenge_id + ":" + _normalize_captcha(answer)), _ip(request), _expiry(5)),
        )
        return True

    if not await gateway_store.call(save):
        raise GatewayError(429, "Too many image requests", code="captcha_rate_limited")
    response.headers["Cache-Control"] = "no-store"
    return {"id": challenge_id, "image": "data:image/png;base64," + base64.b64encode(_captcha_png(answer)).decode()}


def _send_mail(email: str, code: str) -> None:
    message = EmailMessage()
    message["From"] = settings.portal_smtp_from
    message["To"] = email
    message["Subject"] = "MinKing AI 登录验证码"
    message.set_content(f"您的登录验证码为 {code}，10 分钟内有效。若非本人操作，请忽略。")
    if settings.portal_smtp_ssl:
        client = smtplib.SMTP_SSL(settings.portal_smtp_host, settings.portal_smtp_port, timeout=15)
    else:
        client = smtplib.SMTP(settings.portal_smtp_host, settings.portal_smtp_port, timeout=15)
    with client:
        if not settings.portal_smtp_ssl:
            client.starttls()
        if settings.portal_smtp_username:
            client.login(settings.portal_smtp_username, settings.portal_smtp_password)
        client.send_message(message)


async def _issue_code(request: Request, body: MailRequest) -> dict[str, str]:
    email = _email(body.email)
    name = (body.name or "").strip()
    code = f"{secrets.randbelow(1000000):06d}"
    challenge_id = uuid.uuid4().hex
    now = iso_now()
    ip_hash = _ip(request)
    minute = (utc_now() - dt.timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
    hour = (utc_now() - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    resolved = {"name": name}

    def consume(db):
        row = db.execute("SELECT answer_hash,ip_hash,expires_at,consumed_at FROM portal_captchas WHERE id=?", (body.captcha_id,)).fetchone()
        valid = row and row["consumed_at"] is None and row["expires_at"] > now and row["ip_hash"] == ip_hash
        # Consume even an incorrect answer: an image cannot be guessed repeatedly.
        if valid:
            db.execute("UPDATE portal_captchas SET consumed_at=? WHERE id=? AND consumed_at IS NULL", (now, body.captcha_id))
        if not valid or not hmac.compare_digest(
            row["answer_hash"],
            _digest("captcha:" + body.captcha_id + ":" + _normalize_captcha(body.captcha)),
        ):
            return "captcha"
        user = db.execute("SELECT name FROM portal_users WHERE email=?", (email,)).fetchone()
        if not resolved["name"]:
            if not user:
                return "unknown_email"
            resolved["name"] = user["name"]
        recent = db.execute("SELECT COUNT(*) AS n FROM portal_codes WHERE email=? AND created_at>?", (email, minute)).fetchone()["n"]
        hourly = db.execute("SELECT COUNT(*) AS n FROM portal_codes WHERE ip_hash=? AND created_at>?", (ip_hash, hour)).fetchone()["n"]
        if recent or hourly >= 10:
            return "rate"
        db.execute("UPDATE portal_codes SET consumed_at=? WHERE email=? AND consumed_at IS NULL", (now, email))
        db.execute(
            "INSERT INTO portal_codes(id,email,name,ip_hash,code_hash,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            (challenge_id, email, resolved["name"], ip_hash, _digest("otp:" + challenge_id + ":" + code), now, _expiry(10)),
        )
        return "ok"

    result = await gateway_store.call(consume)
    if result == "captcha":
        raise GatewayError(422, "Image code expired or incorrect", code="invalid_captcha")
    if result == "unknown_email":
        raise GatewayError(404, "该邮箱尚未注册，请先注册", code="email_not_registered")
    if result == "need_name":
        raise GatewayError(422, "注册请填写姓名", code="invalid_name")
    if result == "rate":
        raise GatewayError(429, "Please wait before requesting another code", code="mail_rate_limited")
    try:
        await asyncio.to_thread(_send_mail, email, code)
    except Exception as exc:
        await gateway_store.execute("DELETE FROM portal_codes WHERE id=?", (challenge_id,))
        raise GatewayError(503, "Verification email could not be sent", code="mail_unavailable") from exc
    return {"challenge_id": challenge_id, "expires_in": "600"}


@router.post("/auth/send-code")
async def send_code(request: Request, body: MailRequest) -> dict[str, str]:
    _origin(request)
    return await _issue_code(request, body)


@router.post("/desktop/auth/send-code")
async def desktop_send_code(request: Request, body: MailRequest) -> dict[str, str]:
    require_enabled()
    return await _issue_code(request, body)


@dataclass(slots=True)
class PortalSession:
    user_id: str
    csrf_token: str
    name: str
    email: str
    raw_token: str = ""
    via: str = "cookie"


async def _complete_email_login(
    body: VerifyRequest,
    *,
    minutes: int,
    kind: str = "session:",
    device_hash: str = "",
) -> tuple[str, str, str, str, str]:
    email = _email(body.email)
    now = iso_now()
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    user_id = uuid.uuid4().hex
    key_id = uuid.uuid4().hex
    raw_key = "sk-ts-" + secrets.token_urlsafe(32)

    def complete(db):
        row = db.execute("SELECT * FROM portal_codes WHERE id=? AND email=?", (body.challenge_id, email)).fetchone()
        if not row or row["consumed_at"] is not None or row["expires_at"] <= now or int(row["attempts"]) >= 5:
            return None
        correct = hmac.compare_digest(row["code_hash"], _digest("otp:" + body.challenge_id + ":" + body.code))
        db.execute("UPDATE portal_codes SET attempts=attempts+1,consumed_at=? WHERE id=?", (now if correct else None, body.challenge_id))
        if not correct:
            return None
        existed = db.execute("SELECT id FROM portal_users WHERE email=?", (email,)).fetchone()
        grant = "0.00"
        notice = ""
        if existed is None:
            billing_row = db.execute(
                "SELECT new_user_usd FROM billing_settings WHERE id=1"
            ).fetchone()
            grant = usd_text((billing_row or {}).get("new_user_usd") or "0.00")
            if device_hash:
                inserted = db.execute(
                    "INSERT OR IGNORE INTO signup_devices(device_hash,user_id,created_at) VALUES(?,?,?)"
                    if gateway_store.engine != "mysql"
                    else "INSERT IGNORE INTO signup_devices(device_hash,user_id,created_at) VALUES(?,?,?)",
                    (device_hash, user_id, now),
                )
                if int(getattr(inserted, "rowcount", 1) or 0) == 0:
                    grant = "0.00"
                    notice = REWARD_ALREADY_CLAIMED
        db.execute(
            "INSERT INTO portal_users(id,email,name,usd_credit,created_at) VALUES(?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET name=excluded.name",
            (user_id, email, row["name"], grant, now),
        )
        user = db.execute("SELECT id,name,usd_credit FROM portal_users WHERE email=?", (email,)).fetchone()
        owner = user["id"]
        key = db.execute("SELECT id FROM api_keys WHERE owner_user_id=?", (owner,)).fetchone()
        if key is None:
            db.execute(
                "INSERT INTO api_keys(id,name,key_prefix,fingerprint,key_ciphertext,owner_user_id,usd_credit,status,created_at) VALUES(?,?,?,?,?,?,?,'active',?)",
                (key_id, row["name"], raw_key[:12], codex_gateway.digest(raw_key), codex_gateway._encrypt_api_key(raw_key), owner, grant if existed is None else "0.00", now),
            )
            owned_key = key_id
        else:
            owned_key = key["id"]
        if existed is None:
            grant_amount = money2(grant)
            if grant_amount > 0:
                db.execute(
                    """
                    INSERT INTO wallet_ledger(
                        id,user_id,key_id,amount_usd,balance_after,kind,request_id,card_id,actor,reason,
                        official_usd,multiplier,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        uuid.uuid4().hex,
                        user["id"],
                        owned_key,
                        grant,
                        grant,
                        "grant",
                        f"new-user:{user['id']}",
                        None,
                        "system",
                        "new_user_usd",
                        None,
                        None,
                        now,
                    ),
                )
        db.execute(
            "INSERT INTO portal_sessions(session_hash,user_id,csrf_token,created_at,expires_at) VALUES(?,?,?,?,?)",
            (_digest(kind + token), owner, csrf, now, _expiry(minutes)),
        )
        return user["name"], notice

    outcome = await gateway_store.call(complete)
    if outcome is None:
        raise GatewayError(401, "Invalid or expired email code", code="invalid_email_code")
    name, notice = outcome
    return name, email, token, csrf, notice


@router.post("/auth/verify")
async def verify(request: Request, body: VerifyRequest) -> JSONResponse:
    _origin(request)
    name, email, token, csrf, notice = await _complete_email_login(
        body, minutes=12 * 60, kind="session:", device_hash=_normalize_device(body.device_id)
    )
    response = JSONResponse({"name": name, "email": email, "csrf_token": csrf, "reward_notice": notice})
    root = str(request.scope.get("root_path", "")).rstrip("/")
    cookie_path = f"{root}/" if root else "/"
    response.set_cookie(COOKIE, token, max_age=12*3600, httponly=True, secure=True, samesite="strict", path=cookie_path)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/desktop/auth/verify")
async def desktop_verify(request: Request, body: VerifyRequest) -> JSONResponse:
    require_enabled()
    email = _email(body.email)
    device_hash = _normalize_device(body.device_id)
    existed = await gateway_store.one("SELECT id FROM portal_users WHERE email=?", (email,))
    if existed is None and not device_hash:
        raise GatewayError(
            422,
            "请更新客户端后再注册。每台电脑只能领取一次新人奖励。",
            code="device_id_required",
        )
    name, email, token, csrf, notice = await _complete_email_login(
        body, minutes=30 * 24 * 60, kind="desktop:", device_hash=device_hash
    )
    response = JSONResponse({
        "name": name,
        "email": email,
        "token": token,
        "csrf_token": csrf,
        "expires_in": 30 * 24 * 3600,
        "token_type": "Bearer",
        "reward_notice": notice,
    })
    response.headers["Cache-Control"] = "no-store"
    return response


def _session_token(request: Request) -> tuple[str, str]:
    cookie = request.cookies.get(COOKIE, "")
    if cookie:
        return cookie, "cookie"
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token and not token.lower().startswith("sk-"):
            return token, "bearer"
    return "", ""


async def session(request: Request) -> PortalSession:
    require_enabled()
    token, via = _session_token(request)
    kind = "desktop:" if via == "bearer" else "session:"
    row = await gateway_store.one(
        "SELECT s.user_id,s.csrf_token,u.name,u.email FROM portal_sessions s JOIN portal_users u ON u.id=s.user_id WHERE s.session_hash=? AND s.expires_at>?",
        (_digest(kind + token), iso_now()),
    ) if token else None
    if not row:
        raise GatewayError(401, "Email login required", code="portal_auth_required")
    return PortalSession(row["user_id"], row["csrf_token"], row["name"], row["email"], token, via)


async def write_session(request: Request, user: PortalSession = Depends(session)) -> PortalSession:
    if user.via == "bearer":
        return user
    _origin(request)
    if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), user.csrf_token):
        raise GatewayError(403, "Invalid CSRF token", code="invalid_csrf_token")
    return user


@router.get("/auth/session")
async def current(user: PortalSession = Depends(session)) -> JSONResponse:
    response = JSONResponse({"name": user.name, "email": user.email, "csrf_token": user.csrf_token})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/auth/logout")
async def logout(request: Request, user: PortalSession = Depends(write_session)) -> JSONResponse:
    kind = "desktop:" if user.via == "bearer" else "session:"
    await gateway_store.execute("DELETE FROM portal_sessions WHERE session_hash=?", (_digest(kind + user.raw_token),))
    response = JSONResponse({"ok": True})
    root = str(request.scope.get("root_path", "")).rstrip("/")
    cookie_path = f"{root}/" if root else "/"
    response.delete_cookie(COOKIE, path=cookie_path, secure=True, samesite="strict")
    return response


async def _owned_key(user: PortalSession) -> dict:
    key = await gateway_store.one("SELECT id,key_prefix,status,created_at FROM api_keys WHERE owner_user_id=?", (user.user_id,))
    if not key or key["status"] == "deleted":
        raise GatewayError(404, "Your API key is unavailable", code="api_key_not_found")
    return key


def _period_start(days: int) -> str:
    return (utc_now() - dt.timedelta(days=days)).isoformat().replace("+00:00", "Z")


async def _call_summary(owner_user_id: str, since: str) -> dict[str, Any]:
    row = await gateway_store.one(
        """
        SELECT COUNT(*) AS calls,
               SUM(CASE WHEN c.status='success' THEN 1 ELSE 0 END) AS successes,
               SUM(CASE WHEN c.status='failed' THEN 1 ELSE 0 END) AS failures,
               COALESCE(SUM(c.total_tokens),0) AS tokens,
               COALESCE(SUM(c.input_tokens),0) AS input_tokens,
               COALESCE(SUM(c.output_tokens),0) AS output_tokens,
               COALESCE(SUM(c.cached_tokens),0) AS cached_tokens
        FROM call_records c
        JOIN api_keys k ON k.id=c.key_id
        WHERE k.owner_user_id=? AND c.started_at>=? AND c.status!='in_progress'
        """,
        (owner_user_id, since),
    )
    return dict(row or {})


@router.get("/dashboard")
async def dashboard(user: PortalSession = Depends(session)) -> JSONResponse:
    key = await _owned_key(user)
    since = _period_start(90)
    daily = await gateway_store.all(
        "SELECT SUBSTR(started_at,1,10) AS day,COUNT(*) AS calls,SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes FROM call_records WHERE key_id=? AND started_at>=? AND status!='in_progress' GROUP BY SUBSTR(started_at,1,10) ORDER BY day",
        (key["id"], since),
    )
    summary = await _call_summary(user.user_id, since)
    today = await _call_summary(user.user_id, utc_now().date().isoformat() + "T00:00:00Z")
    week = await _call_summary(user.user_id, _period_start(7))
    month = await _call_summary(user.user_id, _period_start(30))
    downloads = await gateway_store.one("SELECT COUNT(*) AS total FROM portal_downloads WHERE user_id=?", (user.user_id,))
    credit = await gateway_store.one("SELECT usd_credit FROM portal_users WHERE id=?", (user.user_id,))
    recent = await gateway_store.all(
        "SELECT started_at,model,status,total_tokens,endpoint FROM call_records WHERE key_id=? ORDER BY started_at DESC LIMIT 12",
        (key["id"],),
    )
    total = int((summary or {}).get("calls") or 0)
    successes = int((summary or {}).get("successes") or 0)
    failures = int((summary or {}).get("failures") or 0)
    balance = usd_text((credit or {}).get("usd_credit") or "0.00")
    payload = {
        "user": {"name": user.name, "email": user.email, "usd_credit": balance},
        "balance": balance,
        "today_calls": int((today or {}).get("calls") or 0),
        "calls_7d": int((week or {}).get("calls") or 0),
        "calls_30d": int((month or {}).get("calls") or 0),
        "key": {"prefix": key["key_prefix"], "status": key["status"], "created_at": key["created_at"]},
        "metrics": {
            "calls": total,
            "successes": successes,
            "failures": failures,
            "success_rate": round(100 * successes / total, 1) if total else None,
            "failure_rate": round(100 * failures / total, 1) if total else None,
            "tokens": int((summary or {}).get("tokens") or 0),
            "input_tokens": int((summary or {}).get("input_tokens") or 0),
            "output_tokens": int((summary or {}).get("output_tokens") or 0),
            "cached_tokens": int((summary or {}).get("cached_tokens") or 0),
            "downloads": int((downloads or {}).get("total") or 0),
        },
        "periods": {"day": today or {}, "week": week or {}, "month": month or {}},
        "daily": daily,
        "recent": recent,
        "desktop": {
            "protocol": DESKTOP_PROTOCOL,
            "import_url": desktop_import_url(email=user.email),
        },
    }
    response = JSONResponse(json_ready(payload))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/key/rotate")
async def rotate(user: PortalSession = Depends(write_session)) -> dict[str, str]:
    key = await _owned_key(user)
    if key["status"] != "active":
        raise GatewayError(409, "API key is not active", code="api_key_inactive")
    await codex_gateway.rotate_api_key(key["id"])
    return {"ok": "true"}


@router.post("/bundle")
async def download(user: PortalSession = Depends(write_session)) -> Response:
    key = await _owned_key(user)
    payload = await codex_bundle(key["id"], owner_user_id=user.user_id)
    await gateway_store.execute(
        "INSERT INTO portal_downloads(id,user_id,key_id,created_at) VALUES(?,?,?,?)",
        (uuid.uuid4().hex, user.user_id, key["id"], iso_now()),
    )
    return Response(payload, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="minking-api-codex.zip"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


@router.get("/desktop/bootstrap")
async def desktop_bootstrap(user: PortalSession = Depends(session)) -> JSONResponse:
    if user.via != "bearer":
        raise GatewayError(401, "Desktop token required", code="portal_auth_required")
    key = await _owned_key(user)
    credit = await gateway_store.one("SELECT usd_credit FROM portal_users WHERE id=?", (user.user_id,))
    try:
        catalog = (await codex_gateway.models(client_version="bundle"))["models"]
        models = [item["slug"] for item in catalog if isinstance(item.get("slug"), str)]
    except GatewayError:
        catalog = []
        models = []
    response = JSONResponse({
        "user": {"name": user.name, "email": user.email, "usd_credit": usd_text((credit or {}).get("usd_credit") or "0.00")},
        "key": {"prefix": key["key_prefix"], "status": key["status"]},
        "public_base_url": public_v1_url(),
        "models": models,
        "catalog": {"models": catalog},
        "harnesses": harness_catalog(messages_ready=True),
        "desktop": {
            "protocol": DESKTOP_PROTOCOL,
            "import_url": desktop_import_url(email=user.email),
        },
    })
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/desktop/key")
async def desktop_key(user: PortalSession = Depends(write_session)) -> JSONResponse:
    if user.via != "bearer":
        raise GatewayError(401, "Desktop token required", code="portal_auth_required")
    key = await _owned_key(user)
    if key["status"] != "active":
        raise GatewayError(409, "API key is not active", code="api_key_inactive")
    row = await gateway_store.one("SELECT key_ciphertext FROM api_keys WHERE id=?", (key["id"],))
    raw = codex_gateway._decrypt_api_key(row["key_ciphertext"]) if row else None
    if raw is None:
        raise GatewayError(409, "API key cannot be recovered; rotate it first", code="key_not_recoverable")
    response = JSONResponse({"key": raw, "prefix": key["key_prefix"]})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/desktop/logout")
async def desktop_logout(user: PortalSession = Depends(write_session)) -> JSONResponse:
    if user.via != "bearer":
        raise GatewayError(401, "Desktop token required", code="portal_auth_required")
    await gateway_store.execute("DELETE FROM portal_sessions WHERE session_hash=?", (_digest("desktop:" + user.raw_token),))
    response = JSONResponse({"ok": True})
    response.headers["Cache-Control"] = "no-store"
    return response


def _require_desktop(user: PortalSession) -> None:
    if user.via != "bearer":
        raise GatewayError(401, "Desktop token required", code="portal_auth_required")


@router.get("/desktop/skills")
async def desktop_skills(user: PortalSession = Depends(session)) -> JSONResponse:
    _require_desktop(user)
    response = JSONResponse({"skills": list_client_skills()})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/desktop/skills/{name}")
async def desktop_skill_zip(name: str, user: PortalSession = Depends(session)) -> Response:
    _require_desktop(user)
    payload, digest = zip_client_skill(name)
    return Response(
        payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.zip"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Skill-Sha256": digest,
        },
    )


class RedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=32)


def _billing_http(exc: BillingError) -> GatewayError:
    return GatewayError(exc.status, exc.message, code=exc.code, error_type="billing_error")


@router.get("/calls")
async def portal_calls(
    user: PortalSession = Depends(session),
    page: int = 1,
    page_size: int = 25,
    start: str | None = None,
    end: str | None = None,
    model: str | None = None,
    endpoint: str | None = None,
    status: str | None = None,
) -> JSONResponse:
    safe_page = max(1, int(page))
    safe_size = max(1, min(int(page_size), 100))
    clauses = ["k.owner_user_id=?"]
    values: list[Any] = [user.user_id]
    if start:
        clauses.append("c.started_at>=?")
        values.append(start)
    if end:
        clauses.append("c.started_at<?")
        values.append(end)
    if model:
        clauses.append("(c.model=? OR c.request_model=? OR c.response_model=?)")
        values.extend([model, model, model])
    if endpoint:
        clauses.append("c.endpoint=?")
        values.append(endpoint)
    if status:
        clauses.append("c.status=?")
        values.append(status)
    where = " AND ".join(clauses)
    total_row = await gateway_store.one(
        f"""
        SELECT COUNT(*) AS n FROM call_records c
        JOIN api_keys k ON k.id=c.key_id
        WHERE {where}
        """,
        tuple(values),
    )
    rows = await gateway_store.all(
        f"""
        SELECT c.request_id,c.started_at,c.ended_at,c.duration_ms,c.endpoint,c.model,
               c.request_model,c.response_model,c.status,c.error_code,c.http_status,
               c.input_tokens,c.output_tokens,c.cached_tokens,c.total_tokens,
               c.usage_unknown,c.usd_charged
        FROM call_records c
        JOIN api_keys k ON k.id=c.key_id
        WHERE {where}
        ORDER BY c.started_at DESC LIMIT ? OFFSET ?
        """,
        (*values, safe_size, (safe_page - 1) * safe_size),
    )
    data = []
    for row in rows:
        item = dict(row)
        item["usage_unknown"] = bool(item.get("usage_unknown"))
        if item.get("usd_charged") is not None:
            item["usd_charged"] = usd_text(item.get("usd_charged"))
        data.append(item)
    payload = {
        "data": data,
        "page": safe_page,
        "page_size": safe_size,
        "total": int((total_row or {}).get("n") or 0),
    }
    response = JSONResponse(json_ready(payload))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/wallet")
async def portal_wallet(user: PortalSession = Depends(session)) -> JSONResponse:
    payload = await user_wallet(user.user_id)
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/wallet/ledger")
async def portal_wallet_ledger(
    user: PortalSession = Depends(session),
    page: int = 1,
    page_size: int = 50,
    kind: str | None = None,
) -> JSONResponse:
    payload = await list_ledger(user_id=user.user_id, kind=kind, page=page, page_size=page_size)
    response = JSONResponse(json_ready(payload))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/wallet/redeem")
async def portal_redeem(
    request: Request,
    body: RedeemRequest,
    user: PortalSession = Depends(write_session),
) -> JSONResponse:
    try:
        payload = await redeem_card(user.user_id, body.code, ip_hash=_ip(request))
    except BillingError as exc:
        raise _billing_http(exc) from exc
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/pricing")
async def portal_pricing() -> JSONResponse:
    payload = await list_public_pricing()
    response = JSONResponse(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


_PUBLIC_HARNESSES = [
    {"id": "codex", "name": "Codex", "summary": "CLI 与 Desktop 走 Responses，模型目录随工作台更新。"},
    {"id": "grok", "name": "Grok", "summary": "官方客户端接入同一把密钥。本地模式仍使用已经登录的官方账号。"},
    {"id": "claude_code", "name": "Claude Code", "summary": "一键切换接口地址和模型档位，不覆盖官方登录。"},
    {"id": "workbuddy", "name": "WorkBuddy", "summary": "同步 WorkBuddy 与 CodeBuddy 的模型清单和推理开关。"},
    {"id": "zcode", "name": "ZCode", "summary": "把选中的模型写入 ZCode 的提供方配置。"},
    {"id": "antigravity", "name": "Antigravity", "summary": "把 Antigravity 的模型线路切到 MinKing，官方登录留在本机。"},
]


@router.get("/harnesses")
async def portal_harnesses(user: PortalSession = Depends(session)) -> JSONResponse:
    del user
    response = JSONResponse({"data": _PUBLIC_HARNESSES})
    response.headers["Cache-Control"] = "no-store"
    return response


class PlaygroundRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=2000)
    mode: Literal["text", "image", "video"] = "text"


def _playground_target(body: PlaygroundRequest) -> tuple[str, dict[str, Any]]:
    if body.mode == "image":
        return "/v1/images/generations", {"model": body.model, "prompt": body.prompt}
    if body.mode == "video":
        return "/v1/videos", {"model": body.model, "prompt": body.prompt}
    return "/v1/responses", {"model": body.model, "input": body.prompt, "stream": False}


def _playground_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
            elif isinstance(item.get("text"), str):
                chunks.append(item["text"])
    if isinstance(payload.get("output_text"), str):
        chunks.append(payload["output_text"])
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            message = choice.get("message") if isinstance(choice, dict) else None
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                chunks.append(message["content"])
    return "\n".join(chunk.strip() for chunk in chunks if chunk.strip())[:4000]


def _playground_public(status: int, raw: bytes, mode: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "status": status, "message": "调用没有返回可读结果"}
    if not isinstance(payload, dict):
        return {"ok": status < 400, "status": status, "message": "调用已返回"}
    if status >= 400:
        err = payload.get("error")
        message = err.get("message") if isinstance(err, dict) else "调用失败"
        return {"ok": False, "status": status, "message": str(message)[:300]}
    if mode == "image":
        data = payload.get("data")
        encoded = ""
        if isinstance(data, list) and data and isinstance(data[0], dict):
            encoded = str(data[0].get("b64_json") or "")
        if encoded and len(encoded) <= 280000:
            return {"ok": True, "status": status, "image": f"data:image/png;base64,{encoded}"}
        return {"ok": True, "status": status, "message": "图片已生成，请在调用明细确认扣费。"}
    if mode == "video":
        return {"ok": True, "status": status, "message": "视频任务已提交，请在调用明细确认进度和扣费。"}
    text = _playground_text(payload)
    return {"ok": True, "status": status, "text": text or "调用已完成，请在调用明细查看用量。"}


@router.post("/playground")
async def portal_playground(
    request: Request,
    body: PlaygroundRequest,
    user: PortalSession = Depends(write_session),
) -> JSONResponse:
    import httpx

    key = await _owned_key(user)
    if key["status"] != "active":
        raise GatewayError(409, "API key is not active", code="api_key_inactive")
    row = await gateway_store.one("SELECT key_ciphertext FROM api_keys WHERE id=?", (key["id"],))
    raw = codex_gateway._decrypt_api_key(row["key_ciphertext"]) if row else None
    if not raw:
        raise GatewayError(409, "API key cannot be recovered; rotate it first", code="key_not_recoverable")
    path, payload = _playground_target(body)
    transport = httpx.ASGITransport(app=request.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://portal.internal", timeout=180) as client:
            upstream = await client.post(
                path,
                json=payload,
                headers={"Authorization": f"Bearer {raw}", "User-Agent": "MinKingPortal/playground"},
            )
    except httpx.HTTPError as exc:
        raise GatewayError(502, "调用测试没有完成", code="playground_failed") from exc
    response = JSONResponse(_playground_public(upstream.status_code, upstream.content, body.mode))
    response.headers["Cache-Control"] = "no-store"
    return response
