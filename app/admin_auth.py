from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request

from app.config import settings
from app.store.gateway import gateway_store, iso_now, utc_now

ADMIN_USERNAME = "admin"
SESSION_COOKIE = "ts_admin_session"
MIN_PASSWORD_LENGTH = 12


@dataclass(slots=True)
class AdminSession:
    username: str
    csrf_token: str
    expires_at: str
    raw_token: str | None = None


class AdminAuth:
    def __init__(self) -> None:
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=2,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    async def start(self) -> None:
        self._failures.clear()
        credential = await gateway_store.one(
            "SELECT username FROM admin_credentials WHERE username=?", (ADMIN_USERNAME,)
        )
        if credential is not None:
            return
        password = settings.admin_initial_password
        self.validate_password(password, setting_name="ADMIN_INITIAL_PASSWORD")
        password_hash = await asyncio.to_thread(self._hasher.hash, password)
        await gateway_store.execute(
            """
            INSERT INTO admin_credentials(username,password_hash,password_updated_at)
            VALUES(?,?,?)
            """,
            (ADMIN_USERNAME, password_hash, iso_now()),
        )

    @staticmethod
    def validate_password(password: str, *, setting_name: str = "password") -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"{setting_name} must contain at least {MIN_PASSWORD_LENGTH} characters")
        if password.isspace():
            raise ValueError(f"{setting_name} cannot contain only whitespace")

    @staticmethod
    def _digest(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    def _client_ip(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _prune_failures(self, ip: str) -> deque[float]:
        failures = self._failures[ip]
        cutoff = time.monotonic() - settings.admin_login_window_minutes * 60
        while failures and failures[0] <= cutoff:
            failures.popleft()
        return failures

    async def login(self, request: Request, username: str, password: str) -> AdminSession:
        ip = self._client_ip(request)
        failures = self._prune_failures(ip)
        if len(failures) >= settings.admin_login_max_failures:
            from app.codex_gateway import GatewayError

            raise GatewayError(
                429,
                "Too many login attempts; try again later",
                code="admin_login_rate_limited",
                error_type="authentication_error",
            )
        row = await gateway_store.one(
            "SELECT username,password_hash FROM admin_credentials WHERE username=?",
            (ADMIN_USERNAME,),
        )
        verified = False
        if row is not None and hmac.compare_digest(username, ADMIN_USERNAME):
            try:
                verified = await asyncio.to_thread(
                    self._hasher.verify, str(row["password_hash"]), password
                )
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                verified = False
        if not verified:
            failures.append(time.monotonic())
            from app.codex_gateway import GatewayError

            raise GatewayError(
                401,
                "Invalid username or password",
                code="invalid_admin_credentials",
                error_type="authentication_error",
            )
        self._failures.pop(ip, None)
        return await self.create_session()

    async def create_session(self) -> AdminSession:
        raw_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        expires = utc_now() + dt.timedelta(hours=settings.admin_session_hours)
        expires_at = expires.isoformat().replace("+00:00", "Z")
        await gateway_store.execute(
            """
            INSERT INTO admin_sessions(session_hash,username,csrf_token,created_at,expires_at)
            VALUES(?,?,?,?,?)
            """,
            (self._digest(raw_token), ADMIN_USERNAME, csrf_token, iso_now(), expires_at),
        )
        return AdminSession(ADMIN_USERNAME, csrf_token, expires_at, raw_token)

    async def require_session(self, request: Request) -> AdminSession:
        raw_token = request.cookies.get(SESSION_COOKIE, "")
        row: dict[str, Any] | None = None
        if raw_token:
            row = await gateway_store.one(
                """
                SELECT username,csrf_token,expires_at FROM admin_sessions
                WHERE session_hash=? AND expires_at>?
                """,
                (self._digest(raw_token), iso_now()),
            )
        if row is None:
            from app.codex_gateway import GatewayError

            raise GatewayError(
                401,
                "Administrator login required",
                code="admin_auth_required",
                error_type="authentication_error",
            )
        return AdminSession(
            str(row["username"]), str(row["csrf_token"]), str(row["expires_at"])
        )

    def require_same_origin(self, request: Request) -> None:
        supplied = request.headers.get("origin")
        if not supplied:
            referer = request.headers.get("referer", "")
            if referer:
                parsed = urlsplit(referer)
                supplied = f"{parsed.scheme}://{parsed.netloc}"
        expected = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        if not supplied or not hmac.compare_digest(supplied.rstrip("/"), expected.rstrip("/")):
            from app.codex_gateway import GatewayError

            raise GatewayError(
                403,
                "Same-origin request required",
                code="admin_origin_required",
                error_type="authentication_error",
            )

    async def require_write(self, request: Request) -> AdminSession:
        self.require_same_origin(request)
        session = await self.require_session(request)
        csrf = request.headers.get("x-csrf-token", "")
        if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
            from app.codex_gateway import GatewayError

            raise GatewayError(
                403,
                "Invalid CSRF token",
                code="invalid_csrf_token",
                error_type="authentication_error",
            )
        return session

    async def logout(self, request: Request) -> None:
        raw_token = request.cookies.get(SESSION_COOKIE, "")
        if raw_token:
            await gateway_store.execute(
                "DELETE FROM admin_sessions WHERE session_hash=?", (self._digest(raw_token),)
            )

    async def change_password(
        self, current_password: str, new_password: str
    ) -> AdminSession:
        self.validate_password(new_password, setting_name="new password")
        row = await gateway_store.one(
            "SELECT password_hash FROM admin_credentials WHERE username=?", (ADMIN_USERNAME,)
        )
        verified = False
        if row is not None:
            try:
                verified = await asyncio.to_thread(
                    self._hasher.verify, str(row["password_hash"]), current_password
                )
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                verified = False
        if not verified:
            from app.codex_gateway import GatewayError

            raise GatewayError(
                401,
                "Current password is incorrect",
                code="invalid_current_password",
                error_type="authentication_error",
            )
        password_hash = await asyncio.to_thread(self._hasher.hash, new_password)

        def operation(db: Any) -> None:
            db.execute(
                """
                UPDATE admin_credentials SET password_hash=?,password_updated_at=?
                WHERE username=?
                """,
                (password_hash, iso_now(), ADMIN_USERNAME),
            )
            db.execute("DELETE FROM admin_sessions")

        await gateway_store.call(operation)
        return await self.create_session()


admin_auth = AdminAuth()
