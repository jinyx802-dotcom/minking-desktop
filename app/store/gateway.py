from __future__ import annotations

import asyncio
import datetime as dt
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from app.config import settings

T = TypeVar("T")
SHANGHAI_TIMEZONE = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
_CONFLICT_RE = re.compile(
    r"ON CONFLICT\([^)]+\)\s+DO UPDATE SET\s+",
    re.IGNORECASE,
)
_EXCLUDED_RE = re.compile(r"excluded\.(\w+)", re.IGNORECASE)

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,
    credential_path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    expires_at TEXT,
    cooldown_until TEXT,
    last_refresh_at TEXT,
    network_failures INTEGER NOT NULL DEFAULT 0,
    route_successes INTEGER NOT NULL DEFAULT 0,
    route_failures INTEGER NOT NULL DEFAULT 0,
    route_window_started_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'codex',
    label TEXT,
    auth_mode TEXT,
    capabilities TEXT
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    key_ciphertext TEXT,
    owner_user_id TEXT,
    usd_credit TEXT NOT NULL DEFAULT '0.00',
    fast_enabled INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    last_used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_key_routes (
    key_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'codex',
    preferred_account_id TEXT,
    active_account_id TEXT,
    failover_until TEXT,
    last_failure_code TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(key_id, provider),
    FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
    FOREIGN KEY(preferred_account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
    FOREIGN KEY(active_account_id) REFERENCES accounts(account_id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS route_streaks (
    key_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    account_id TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(key_id, provider),
    FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS account_model_health (
    account_id TEXT NOT NULL,
    model TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    window_started_at TEXT NOT NULL,
    cooldown_until TEXT,
    PRIMARY KEY(account_id, model),
    FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS admin_credentials (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    password_updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    FOREIGN KEY(username) REFERENCES admin_credentials(username) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS portal_users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    usd_credit TEXT NOT NULL DEFAULT '0.00', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS portal_captchas (
    id TEXT PRIMARY KEY, answer_hash TEXT NOT NULL, ip_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL, consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS portal_codes (
    id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL, ip_hash TEXT NOT NULL,
    code_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, consumed_at TEXT
);
CREATE TABLE IF NOT EXISTS portal_sessions (
    session_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS portal_downloads (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, key_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_portal_codes_email ON portal_codes(email,created_at);
CREATE INDEX IF NOT EXISTS ix_portal_codes_ip ON portal_codes(ip_hash,created_at);
CREATE INDEX IF NOT EXISTS ix_portal_downloads_user ON portal_downloads(user_id,created_at);
CREATE TABLE IF NOT EXISTS file_artifacts (
    id TEXT PRIMARY KEY,
    owner_key_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS video_jobs (
    video_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'grok',
    created_at TEXT NOT NULL,
    upstream_id TEXT,
    model TEXT,
    seconds TEXT,
    size TEXT,
    status TEXT,
    remixed_from_video_id TEXT,
    FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE,
    FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_accounts_provider ON accounts(provider, status);
CREATE INDEX IF NOT EXISTS ix_admin_sessions_expiry ON admin_sessions(expires_at);
CREATE INDEX IF NOT EXISTS ix_file_artifacts_owner ON file_artifacts(owner_key_id,created_at DESC);
CREATE INDEX IF NOT EXISTS ix_file_artifacts_expiry ON file_artifacts(expires_at);
CREATE INDEX IF NOT EXISTS ix_api_key_routes_preferred ON api_key_routes(preferred_account_id);
CREATE INDEX IF NOT EXISTS ix_api_key_routes_active ON api_key_routes(active_account_id);
CREATE INDEX IF NOT EXISTS ix_video_jobs_account ON video_jobs(account_id);
CREATE INDEX IF NOT EXISTS ix_video_jobs_key ON video_jobs(key_id);
CREATE TABLE IF NOT EXISTS usage_daily (
    usage_date TEXT NOT NULL,
    key_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    unknown_usage_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(usage_date, key_id, account_id, model)
);
CREATE TABLE IF NOT EXISTS call_records (
    request_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    key_id TEXT NOT NULL,
    key_name TEXT NOT NULL,
    account_id TEXT,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    request_model TEXT,
    response_model TEXT,
    is_stream INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    http_status INTEGER,
    status TEXT NOT NULL DEFAULT 'in_progress',
    error_code TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    usage_unknown INTEGER NOT NULL DEFAULT 1,
    usd_charged TEXT
);
CREATE TABLE IF NOT EXISTS error_ring (
    slot INTEGER PRIMARY KEY,
    sequence INTEGER NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    level TEXT NOT NULL,
    category TEXT NOT NULL,
    user_name TEXT,
    method TEXT,
    path TEXT,
    status INTEGER,
    code TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_usage_dimensions ON usage_daily(usage_date, key_id, account_id, model);
CREATE INDEX IF NOT EXISTS ix_error_ring_sequence ON error_ring(sequence DESC);
CREATE INDEX IF NOT EXISTS ix_calls_started ON call_records(started_at DESC);
CREATE INDEX IF NOT EXISTS ix_calls_filters ON call_records(key_id,account_id,model,endpoint,status,started_at DESC);
CREATE TABLE IF NOT EXISTS billing_settings (
    id INTEGER PRIMARY KEY CHECK (id=1),
    price_multiplier TEXT NOT NULL DEFAULT '0.12',
    enforced INTEGER NOT NULL DEFAULT 0,
    new_user_usd TEXT NOT NULL DEFAULT '0.00',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS model_official_prices (
    model TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    modality TEXT NOT NULL,
    official_input_usd_per_1m TEXT,
    official_output_usd_per_1m TEXT,
    official_cached_usd_per_1m TEXT,
    official_reasoning_usd_per_1m TEXT,
    official_usd_per_image TEXT,
    official_usd_per_second TEXT,
    multiplier_override TEXT,
    sell_override_input TEXT,
    sell_override_output TEXT,
    sell_override_cached TEXT,
    sell_override_image TEXT,
    sell_override_second TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    official_synced_at TEXT,
    price_source TEXT NOT NULL DEFAULT 'catalog'
);
CREATE TABLE IF NOT EXISTS wallet_ledger (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    key_id TEXT,
    amount_usd TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    kind TEXT NOT NULL,
    request_id TEXT,
    card_id TEXT,
    actor TEXT,
    reason TEXT,
    official_usd TEXT,
    multiplier TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_wallet_ledger_user ON wallet_ledger(user_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_ledger_usage_request
    ON wallet_ledger(request_id) WHERE kind='usage' AND request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS card_keys (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    code_prefix TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'unused',
    expires_at TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    redeemed_by TEXT,
    redeemed_at TEXT,
    batch_id TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_card_keys_status ON card_keys(status, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_card_keys_batch ON card_keys(batch_id);
CREATE TABLE IF NOT EXISTS card_redemptions (
    id TEXT PRIMARY KEY,
    card_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(card_id) REFERENCES card_keys(id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS billing_nonces (
    nonce TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_redeem_attempts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    ip_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_card_redeem_attempts_user ON card_redeem_attempts(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_card_redeem_attempts_ip ON card_redeem_attempts(ip_hash, created_at);
CREATE TABLE IF NOT EXISTS signup_devices (
    device_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

MYSQL_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id VARCHAR(191) PRIMARY KEY,
    credential_path TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    expires_at VARCHAR(64),
    cooldown_until VARCHAR(64),
    last_refresh_at VARCHAR(64),
    network_failures INT NOT NULL DEFAULT 0,
    route_successes INT NOT NULL DEFAULT 0,
    route_failures INT NOT NULL DEFAULT 0,
    route_window_started_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    updated_at VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'codex',
    label VARCHAR(255),
    auth_mode VARCHAR(64),
    capabilities TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS api_keys (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(191) NOT NULL,
    key_prefix VARCHAR(64) NOT NULL,
    fingerprint VARCHAR(128) NOT NULL,
    key_ciphertext TEXT,
    owner_user_id VARCHAR(64),
    usd_credit DECIMAL(24,8) NOT NULL DEFAULT 0.00,
    fast_enabled TINYINT NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    last_used_at VARCHAR(64),
    created_at VARCHAR(64) NOT NULL,
    UNIQUE KEY uq_api_keys_fingerprint (fingerprint)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS api_key_routes (
    key_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'codex',
    preferred_account_id VARCHAR(191),
    active_account_id VARCHAR(191),
    failover_until VARCHAR(64),
    last_failure_code VARCHAR(64),
    updated_at VARCHAR(64) NOT NULL,
    PRIMARY KEY(key_id, provider),
    CONSTRAINT fk_routes_key FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE,
    CONSTRAINT fk_routes_preferred FOREIGN KEY(preferred_account_id) REFERENCES accounts(account_id) ON DELETE RESTRICT,
    CONSTRAINT fk_routes_active FOREIGN KEY(active_account_id) REFERENCES accounts(account_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS route_streaks (
    key_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    account_id VARCHAR(191) NOT NULL,
    failures INT NOT NULL DEFAULT 0,
    PRIMARY KEY(key_id, provider),
    CONSTRAINT fk_streak_key FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS account_model_health (
    account_id VARCHAR(191) NOT NULL,
    model VARCHAR(191) NOT NULL,
    failures INT NOT NULL DEFAULT 0,
    window_started_at VARCHAR(64) NOT NULL,
    cooldown_until VARCHAR(64),
    PRIMARY KEY(account_id, model),
    CONSTRAINT fk_model_health_account FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS admin_credentials (
    username VARCHAR(64) PRIMARY KEY,
    password_hash TEXT NOT NULL,
    password_updated_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS admin_sessions (
    session_hash VARCHAR(128) PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    csrf_token VARCHAR(128) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    CONSTRAINT fk_admin_sessions_user FOREIGN KEY(username) REFERENCES admin_credentials(username) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS portal_users (
    id VARCHAR(64) PRIMARY KEY, email VARCHAR(254) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL, usd_credit DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    created_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS portal_captchas (
    id VARCHAR(64) PRIMARY KEY, answer_hash VARCHAR(128) NOT NULL,
    ip_hash VARCHAR(128) NOT NULL, expires_at VARCHAR(64) NOT NULL,
    consumed_at VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS portal_codes (
    id VARCHAR(64) PRIMARY KEY, email VARCHAR(254) NOT NULL,
    name VARCHAR(100) NOT NULL, ip_hash VARCHAR(128) NOT NULL,
    code_hash VARCHAR(128) NOT NULL, created_at VARCHAR(64) NOT NULL,
    expires_at VARCHAR(64) NOT NULL, attempts INT NOT NULL DEFAULT 0,
    consumed_at VARCHAR(64), INDEX ix_portal_codes_email (email,created_at),
    INDEX ix_portal_codes_ip (ip_hash,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS portal_sessions (
    session_hash VARCHAR(128) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
    csrf_token VARCHAR(128) NOT NULL, created_at VARCHAR(64) NOT NULL,
    expires_at VARCHAR(64) NOT NULL,
    CONSTRAINT fk_portal_session_user FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS portal_downloads (
    id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
    key_id VARCHAR(64) NOT NULL, created_at VARCHAR(64) NOT NULL,
    INDEX ix_portal_downloads_user (user_id,created_at),
    CONSTRAINT fk_portal_download_user FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS file_artifacts (
    id VARCHAR(64) PRIMARY KEY,
    owner_key_id VARCHAR(64) NOT NULL,
    purpose VARCHAR(64) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    mime_type VARCHAR(128) NOT NULL,
    byte_size INT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    expires_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS video_jobs (
    video_id VARCHAR(191) PRIMARY KEY,
    account_id VARCHAR(191) NOT NULL,
    key_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL DEFAULT 'grok',
    created_at VARCHAR(64) NOT NULL,
    upstream_id VARCHAR(191) NULL,
    model VARCHAR(191) NULL,
    seconds VARCHAR(16) NULL,
    size VARCHAR(32) NULL,
    status VARCHAR(32) NULL,
    remixed_from_video_id VARCHAR(191) NULL,
    CONSTRAINT fk_video_account FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE,
    CONSTRAINT fk_video_key FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_accounts_provider ON accounts(provider, status);
CREATE INDEX ix_admin_sessions_expiry ON admin_sessions(expires_at);
CREATE INDEX ix_file_artifacts_owner ON file_artifacts(owner_key_id, created_at);
CREATE INDEX ix_file_artifacts_expiry ON file_artifacts(expires_at);
CREATE INDEX ix_api_key_routes_preferred ON api_key_routes(preferred_account_id);
CREATE INDEX ix_api_key_routes_active ON api_key_routes(active_account_id);
CREATE INDEX ix_video_jobs_account ON video_jobs(account_id);
CREATE INDEX ix_video_jobs_key ON video_jobs(key_id);
CREATE TABLE IF NOT EXISTS usage_daily (
    usage_date VARCHAR(16) NOT NULL,
    key_id VARCHAR(64) NOT NULL,
    account_id VARCHAR(191) NOT NULL,
    model VARCHAR(191) NOT NULL,
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cached_tokens BIGINT NOT NULL DEFAULT 0,
    reasoning_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    unknown_usage_count INT NOT NULL DEFAULT 0,
    PRIMARY KEY(usage_date, key_id, account_id, model)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS call_records (
    request_id VARCHAR(64) PRIMARY KEY,
    started_at VARCHAR(64) NOT NULL,
    ended_at VARCHAR(64),
    duration_ms INT,
    key_id VARCHAR(64) NOT NULL,
    key_name VARCHAR(191) NOT NULL,
    account_id VARCHAR(191),
    endpoint VARCHAR(191) NOT NULL,
    model VARCHAR(191) NOT NULL,
    request_model VARCHAR(191),
    response_model VARCHAR(191),
    is_stream TINYINT NOT NULL DEFAULT 0,
    attempts INT NOT NULL DEFAULT 0,
    http_status INT,
    status VARCHAR(32) NOT NULL DEFAULT 'in_progress',
    error_code VARCHAR(64),
    input_tokens BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    cached_tokens BIGINT NOT NULL DEFAULT 0,
    reasoning_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    usage_unknown TINYINT NOT NULL DEFAULT 1,
    usd_charged DECIMAL(12,2) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS error_ring (
    slot INT PRIMARY KEY,
    sequence INT NOT NULL,
    occurred_at VARCHAR(64) NOT NULL,
    level VARCHAR(32) NOT NULL,
    category VARCHAR(64) NOT NULL,
    user_name VARCHAR(191),
    method VARCHAR(16),
    path VARCHAR(255),
    status INT,
    code VARCHAR(64) NOT NULL,
    message VARCHAR(500) NOT NULL DEFAULT '',
    UNIQUE KEY uq_error_ring_sequence (sequence)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE INDEX ix_usage_dimensions ON usage_daily(usage_date, key_id, account_id, model);
CREATE INDEX ix_error_ring_sequence ON error_ring(sequence);
CREATE INDEX ix_calls_started ON call_records(started_at);
CREATE INDEX ix_calls_filters ON call_records(key_id, account_id, model, endpoint, status, started_at);
CREATE TABLE IF NOT EXISTS billing_settings (
    id TINYINT PRIMARY KEY,
    price_multiplier DECIMAL(12,4) NOT NULL DEFAULT 0.12,
    enforced TINYINT NOT NULL DEFAULT 0,
    new_user_usd DECIMAL(12,2) NOT NULL DEFAULT 0.00,
    updated_at VARCHAR(64)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS model_official_prices (
    model VARCHAR(191) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,
    modality VARCHAR(16) NOT NULL,
    official_input_usd_per_1m DECIMAL(12,4) NULL,
    official_output_usd_per_1m DECIMAL(12,4) NULL,
    official_cached_usd_per_1m DECIMAL(12,4) NULL,
    official_reasoning_usd_per_1m DECIMAL(12,4) NULL,
    official_usd_per_image DECIMAL(12,4) NULL,
    official_usd_per_second DECIMAL(12,4) NULL,
    multiplier_override DECIMAL(12,4) NULL,
    sell_override_input DECIMAL(12,4) NULL,
    sell_override_output DECIMAL(12,4) NULL,
    sell_override_cached DECIMAL(12,4) NULL,
    sell_override_image DECIMAL(12,4) NULL,
    sell_override_second DECIMAL(12,4) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    official_synced_at VARCHAR(64),
    price_source VARCHAR(16) NOT NULL DEFAULT 'catalog'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS wallet_ledger (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NULL,
    key_id VARCHAR(64) NULL,
    amount_usd DECIMAL(12,2) NOT NULL,
    balance_after DECIMAL(12,2) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    request_id VARCHAR(191) NULL,
    card_id VARCHAR(64) NULL,
    actor VARCHAR(191) NULL,
    reason VARCHAR(500) NULL,
    official_usd DECIMAL(12,4) NULL,
    multiplier DECIMAL(12,4) NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX ix_wallet_ledger_user (user_id, created_at),
    UNIQUE KEY uq_wallet_ledger_request (request_id),
    CONSTRAINT fk_ledger_user FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS card_keys (
    id VARCHAR(64) PRIMARY KEY,
    code_hash VARCHAR(64) NOT NULL,
    code_prefix VARCHAR(32) NOT NULL,
    amount_usd DECIMAL(12,2) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'unused',
    expires_at VARCHAR(64) NULL,
    created_by VARCHAR(64) NULL,
    created_at VARCHAR(64) NOT NULL,
    redeemed_by VARCHAR(64) NULL,
    redeemed_at VARCHAR(64) NULL,
    batch_id VARCHAR(64) NULL,
    note VARCHAR(200) NULL,
    UNIQUE KEY uq_card_keys_hash (code_hash),
    INDEX ix_card_keys_status (status, created_at),
    INDEX ix_card_keys_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS card_redemptions (
    id VARCHAR(64) PRIMARY KEY,
    card_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    amount_usd DECIMAL(12,2) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    CONSTRAINT fk_redeem_card FOREIGN KEY(card_id) REFERENCES card_keys(id) ON DELETE CASCADE,
    CONSTRAINT fk_redeem_user FOREIGN KEY(user_id) REFERENCES portal_users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS billing_nonces (
    nonce VARCHAR(128) PRIMARY KEY,
    created_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS card_redeem_attempts (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    ip_hash VARCHAR(128) NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    INDEX ix_card_redeem_attempts_user (user_id, created_at),
    INDEX ix_card_redeem_attempts_ip (ip_hash, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE IF NOT EXISTS signup_devices (
    device_hash VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    created_at VARCHAR(64) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def usage_date_today() -> str:
    """Return the Asia/Shanghai calendar date used by usage aggregates."""
    return dt.datetime.now(SHANGHAI_TIMEZONE).date().isoformat()


def adapt_sql(sql: str, engine: str) -> str:
    if engine != "mysql":
        return sql
    rewritten = _CONFLICT_RE.sub("ON DUPLICATE KEY UPDATE ", sql)
    rewritten = _EXCLUDED_RE.sub(r"VALUES(\1)", rewritten)
    return rewritten.replace("?", "%s")


class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cursor.fetchall()]


class _SqliteAdapter:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _SqliteCursor:
        return _SqliteCursor(self._connection.execute(adapt_sql(sql, "sqlite"), params))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class _MysqlCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.rowcount = cursor.rowcount

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._cursor.fetchall()]


class _MysqlAdapter:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _MysqlCursor:
        cursor = self._connection.cursor()
        cursor.execute(adapt_sql(sql, "mysql"), params)
        return _MysqlCursor(cursor)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class GatewayStore:
    """Serialized store. SQLite for tests; MySQL 8 when MYSQL_HOST is set."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.engine = "sqlite"
        self._connection: sqlite3.Connection | None = None
        self._queue: asyncio.Queue[Any] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._mysql_lock = asyncio.Lock()

    async def start(self, path: Path | None = None) -> None:
        if settings.uses_mysql:
            await self.stop()
            self.engine = "mysql"
            self.path = None
            self._apply_schema(self._mysql_connection())
            return
        resolved = (path or settings.gateway_db_path()).resolve()
        if self._worker is not None and self.path == resolved and self.engine == "sqlite":
            return
        await self.stop()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(resolved, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SQLITE_SCHEMA)
        if "owner_user_id" not in {row[1] for row in connection.execute("PRAGMA table_info(api_keys)")}:
            connection.execute("ALTER TABLE api_keys ADD COLUMN owner_user_id TEXT")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_api_keys_owner ON api_keys(owner_user_id)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_owner ON api_keys(owner_user_id)")
        self._ensure_video_job_columns_sqlite(connection)
        self._ensure_billing_schema_sqlite(connection)
        self._ensure_catalog_models_sqlite(connection)
        connection.commit()
        self.engine = "sqlite"
        self.path = resolved
        self._connection = connection
        self._queue = asyncio.Queue()
        self._worker = asyncio.create_task(self._run(), name="gateway-sqlite-writer")

    def _mysql_connection(self) -> Any:
        import pymysql
        from pymysql.cursors import DictCursor

        return pymysql.connect(
            host=settings.mysql_host.strip(),
            port=int(settings.mysql_port),
            user=settings.mysql_user,
            password=settings.mysql_password,
            database=settings.mysql_database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=DictCursor,
        )

    def _apply_schema(self, connection: Any) -> None:
        statements = [item.strip() for item in MYSQL_SCHEMA.split(";") if item.strip()]
        try:
            with connection.cursor() as cursor:
                for statement in statements:
                    try:
                        cursor.execute(statement)
                    except Exception as exc:  # duplicate index on rerun
                        message = str(exc).lower()
                        if "duplicate key name" in message or "already exists" in message:
                            continue
                        raise
                cursor.execute("SHOW COLUMNS FROM api_keys LIKE 'owner_user_id'")
                if cursor.fetchone() is None:
                    cursor.execute("ALTER TABLE api_keys ADD COLUMN owner_user_id VARCHAR(64)")
                cursor.execute("SHOW INDEX FROM api_keys WHERE Key_name='ix_api_keys_owner'")
                if cursor.fetchone() is None:
                    cursor.execute("CREATE INDEX ix_api_keys_owner ON api_keys(owner_user_id)")
                cursor.execute("SHOW INDEX FROM api_keys WHERE Key_name='uq_api_keys_owner'")
                if cursor.fetchone() is None:
                    cursor.execute("CREATE UNIQUE INDEX uq_api_keys_owner ON api_keys(owner_user_id)")
                self._ensure_video_job_columns_mysql(cursor)
                self._ensure_billing_schema_mysql(cursor)
                self._ensure_catalog_models_mysql(cursor)
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _ensure_video_job_columns_sqlite(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(video_jobs)")}
        for name, ddl in (
            ("upstream_id", "TEXT"),
            ("model", "TEXT"),
            ("seconds", "TEXT"),
            ("size", "TEXT"),
            ("status", "TEXT"),
            ("remixed_from_video_id", "TEXT"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE video_jobs ADD COLUMN {name} {ddl}")

    @staticmethod
    def _ensure_video_job_columns_mysql(cursor: Any) -> None:
        cursor.execute("SHOW COLUMNS FROM video_jobs")
        columns = {row["Field"] for row in cursor.fetchall()}
        for name, ddl in (
            ("upstream_id", "VARCHAR(191) NULL"),
            ("model", "VARCHAR(191) NULL"),
            ("seconds", "VARCHAR(16) NULL"),
            ("size", "VARCHAR(32) NULL"),
            ("status", "VARCHAR(32) NULL"),
            ("remixed_from_video_id", "VARCHAR(191) NULL"),
        ):
            if name not in columns:
                cursor.execute(f"ALTER TABLE video_jobs ADD COLUMN {name} {ddl}")

    @staticmethod
    def _ensure_billing_schema_sqlite(connection: sqlite3.Connection) -> None:
        call_columns = {row[1] for row in connection.execute("PRAGMA table_info(call_records)")}
        if "usd_charged" not in call_columns:
            connection.execute("ALTER TABLE call_records ADD COLUMN usd_charged TEXT")
        card_columns = {row[1] for row in connection.execute("PRAGMA table_info(card_keys)")}
        if "note" not in card_columns:
            connection.execute("ALTER TABLE card_keys ADD COLUMN note TEXT")
        connection.execute(
            """
            INSERT OR IGNORE INTO billing_settings(id,price_multiplier,enforced,new_user_usd,updated_at)
            VALUES(1,'0.12',0,'0.00',?)
            """,
            (iso_now(),),
        )

    @staticmethod
    def _ensure_billing_schema_mysql(cursor: Any) -> None:
        cursor.execute("SHOW COLUMNS FROM call_records LIKE 'usd_charged'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE call_records ADD COLUMN usd_charged DECIMAL(12,2) NULL")
        cursor.execute("SHOW COLUMNS FROM card_keys LIKE 'note'")
        if cursor.fetchone() is None:
            cursor.execute("ALTER TABLE card_keys ADD COLUMN note VARCHAR(200) NULL")
        cursor.execute(
            """
            INSERT IGNORE INTO billing_settings(id,price_multiplier,enforced,new_user_usd,updated_at)
            VALUES(1,0.12,0,0.00,%s)
            """,
            (iso_now(),),
        )

    @staticmethod
    def _ensure_catalog_models_sqlite(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS catalog_models (
                provider TEXT NOT NULL,
                model_id TEXT NOT NULL,
                model_type TEXT NOT NULL DEFAULT 'text',
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                PRIMARY KEY (provider, model_id)
            )
            """
        )

    @staticmethod
    def _ensure_catalog_models_mysql(cursor: Any) -> None:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS catalog_models (
                provider VARCHAR(32) NOT NULL,
                model_id VARCHAR(191) NOT NULL,
                model_type VARCHAR(16) NOT NULL DEFAULT 'text',
                source VARCHAR(16) NOT NULL DEFAULT 'manual',
                created_at VARCHAR(64) NOT NULL,
                PRIMARY KEY (provider, model_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    async def stop(self) -> None:
        if self._worker is not None and self._queue is not None:
            if not self._worker.done():
                future = asyncio.get_running_loop().create_future()
                await self._queue.put((None, future))
                await future
                await self._worker
            else:
                try:
                    await self._worker
                except Exception:  # noqa: BLE001, S110
                    pass
        if self._connection is not None:
            self._connection.close()
        self.path = None
        self._connection = None
        self._queue = None
        self._worker = None
        self.engine = "sqlite"

    async def _run(self) -> None:
        assert self._queue is not None
        assert self._connection is not None
        adapter = _SqliteAdapter(self._connection)
        while True:
            operation, future = await self._queue.get()
            if operation is None:
                adapter.commit()
                if not future.done():
                    future.set_result(None)
                self._queue.task_done()
                return
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = operation(adapter)
                adapter.commit()
            except BaseException as exc:  # noqa: BLE001
                adapter.rollback()
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self._queue.task_done()

    async def call(self, operation: Callable[[Any], T]) -> T:
        if self.engine == "mysql":
            async with self._mysql_lock:
                def run() -> T:
                    adapter = _MysqlAdapter(self._mysql_connection())
                    try:
                        result = operation(adapter)
                        adapter.commit()
                        return result
                    except BaseException:
                        adapter.rollback()
                        raise
                    finally:
                        adapter.close()

                return await asyncio.to_thread(run)
        if self._queue is None or self._worker is None:
            raise RuntimeError("Gateway store is not started")
        if self._worker.done():
            raise RuntimeError("Gateway store worker is not running")
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        await self._queue.put((operation, future))
        return await future

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        return await self.call(lambda db: db.execute(sql, params).rowcount)

    async def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return await self.call(lambda db: db.execute(sql, params).fetchone())

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return await self.call(lambda db: db.execute(sql, params).fetchall())

    async def upsert_account(
        self,
        account_id: str,
        credential_path: Path,
        expires_at: str | None,
        *,
        provider: str = "codex",
        label: str | None = None,
        auth_mode: str | None = None,
        capabilities: str | None = None,
    ) -> bool:
        existing = await self.one("SELECT account_id FROM accounts WHERE account_id=?", (account_id,))
        now = iso_now()
        await self.execute(
            """
            INSERT INTO accounts(
                account_id, credential_path, status, expires_at, created_at, updated_at,
                provider, label, auth_mode, capabilities
            )
            VALUES(?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                credential_path=excluded.credential_path,
                expires_at=excluded.expires_at,
                status='active',
                cooldown_until=NULL,
                network_failures=0,
                updated_at=excluded.updated_at,
                provider=excluded.provider,
                label=COALESCE(excluded.label, accounts.label),
                auth_mode=COALESCE(excluded.auth_mode, accounts.auth_mode),
                capabilities=COALESCE(excluded.capabilities, accounts.capabilities)
            """,
            (
                account_id,
                str(credential_path),
                expires_at,
                now,
                now,
                provider,
                label,
                auth_mode,
                capabilities,
            ),
        )
        return existing is None

    async def account_success_stats(
        self, account_ids: list[str], *, since: str
    ) -> dict[str, tuple[int, int]]:
        if not account_ids:
            return {}
        placeholders = ",".join("?" * len(account_ids))
        rows = await self.all(
            f"""
            SELECT account_id,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successes,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failures
            FROM call_records
            WHERE started_at>=? AND account_id IN ({placeholders})
            GROUP BY account_id
            """,
            (since, *account_ids),
        )
        return {
            str(row["account_id"]): (int(row["successes"] or 0), int(row["failures"] or 0))
            for row in rows
        }

    async def note_route_outcome(self, account_id: str | None, *, success: bool) -> None:
        if not account_id:
            return
        now = iso_now()
        cutoff = (utc_now() - dt.timedelta(hours=24)).isoformat().replace("+00:00", "Z")

        def operation(db: Any) -> None:
            row = db.execute(
                """
                SELECT route_successes, route_failures, route_window_started_at
                FROM accounts WHERE account_id=?
                """,
                (account_id,),
            ).fetchone()
            if row is None:
                return
            started = str(row.get("route_window_started_at") or "")
            successes = int(row.get("route_successes") or 0)
            failures = int(row.get("route_failures") or 0)
            if not started or started < cutoff:
                successes = 0
                failures = 0
                started = now
            if success:
                successes += 1
            else:
                failures += 1
            db.execute(
                """
                UPDATE accounts
                SET route_successes=?, route_failures=?, route_window_started_at=?
                WHERE account_id=?
                """,
                (successes, failures, started, account_id),
            )

        await self.call(operation)

    async def healthy_accounts(self, provider: str | None = None) -> list[dict[str, Any]]:
        now = iso_now()
        if provider:
            return await self.all(
                """SELECT * FROM accounts
                   WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)
                     AND COALESCE(provider, 'codex')=?
                   ORDER BY account_id""",
                (now, provider),
            )
        return await self.all(
            """SELECT * FROM accounts
               WHERE status='active' AND (cooldown_until IS NULL OR cooldown_until<=?)
               ORDER BY account_id""",
            (now,),
        )

    async def prune_admin_sessions(self) -> None:
        await self.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (iso_now(),))

    async def begin_call(
        self,
        *,
        request_id: str,
        key_id: str,
        key_name: str,
        endpoint: str,
        model: str,
        is_stream: bool,
        account_id: str | None = None,
    ) -> str:
        started_at = iso_now()
        await self.execute(
            """
            INSERT INTO call_records(
                request_id,started_at,key_id,key_name,account_id,endpoint,model,request_model,is_stream,status
            ) VALUES(?,?,?,?,?,?,?,?,?,'in_progress')
            """,
            (
                request_id,
                started_at,
                key_id,
                key_name,
                account_id,
                endpoint,
                model,
                model,
                int(is_stream),
            ),
        )
        return started_at

    async def update_call_progress(
        self,
        request_id: str,
        *,
        account_id: str | None = None,
        model: str | None = None,
        is_stream: bool | None = None,
        attempts: int | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[Any] = []
        if account_id is not None:
            assignments.append("account_id=?")
            values.append(account_id)
        if model is not None:
            assignments.append("model=?")
            assignments.append("request_model=?")
            values.extend([model, model])
        if is_stream is not None:
            assignments.append("is_stream=?")
            values.append(int(is_stream))
        if attempts is not None:
            assignments.append("attempts=?")
            values.append(max(0, attempts))
        if not assignments:
            return
        values.append(request_id)
        await self.execute(
            f"UPDATE call_records SET {', '.join(assignments)} WHERE request_id=? AND status='in_progress'",
            tuple(values),
        )

    async def finalize_call(
        self,
        *,
        request_id: str,
        ended_at: str,
        duration_ms: int,
        account_id: str | None,
        model: str,
        is_stream: bool,
        attempts: int,
        http_status: int,
        status: str,
        error_code: str | None,
        usage: dict[str, Any] | None,
        response_model: str | None = None,
    ) -> None:
        values = _usage_values(usage)
        returned_model = str(response_model or "").strip() or None

        def operation(db: Any) -> None:
            row = db.execute(
                "SELECT key_id,model,request_model FROM call_records WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return
            request_model = str(model or row["request_model"] or row["model"] or "")
            db.execute(
                """
                UPDATE call_records SET
                    ended_at=?,duration_ms=?,account_id=?,model=?,request_model=?,
                    response_model=?,is_stream=?,attempts=?,
                    http_status=?,status=?,error_code=?,input_tokens=?,output_tokens=?,
                    cached_tokens=?,reasoning_tokens=?,total_tokens=?,usage_unknown=?
                WHERE request_id=? AND status='in_progress'
                """,
                (
                    ended_at,
                    max(0, duration_ms),
                    account_id,
                    request_model,
                    request_model,
                    returned_model,
                    int(is_stream),
                    max(0, attempts),
                    http_status,
                    status,
                    error_code,
                    *values,
                    request_id,
                ),
            )
            if account_id:
                db.execute(
                    """
                    INSERT INTO usage_daily(
                        usage_date,key_id,account_id,model,input_tokens,output_tokens,
                        cached_tokens,reasoning_tokens,total_tokens,unknown_usage_count
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(usage_date,key_id,account_id,model) DO UPDATE SET
                        input_tokens=input_tokens+excluded.input_tokens,
                        output_tokens=output_tokens+excluded.output_tokens,
                        cached_tokens=cached_tokens+excluded.cached_tokens,
                        reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens,
                        total_tokens=total_tokens+excluded.total_tokens,
                        unknown_usage_count=unknown_usage_count+excluded.unknown_usage_count
                    """,
                    (usage_date_today(), str(row["key_id"]), account_id, request_model, *values),
                )

        await self.call(operation)

    async def recover_and_prune_calls(self, retention_days: int) -> None:
        now = utc_now()
        cutoff = now - dt.timedelta(days=max(1, int(retention_days)))
        now_text = now.isoformat().replace("+00:00", "Z")
        cutoff_text = cutoff.isoformat().replace("+00:00", "Z")

        def operation(db: Any) -> None:
            db.execute(
                """
                UPDATE call_records SET ended_at=?,duration_ms=0,http_status=499,
                    status='interrupted',error_code='process_interrupted'
                WHERE status='in_progress'
                """,
                (now_text,),
            )
            db.execute("DELETE FROM call_records WHERE started_at<?", (cutoff_text,))
            db.execute("DELETE FROM admin_sessions WHERE expires_at<=?", (now_text,))

        await self.call(operation)

    async def prune_calls(self, retention_days: int) -> None:
        cutoff = utc_now() - dt.timedelta(days=max(1, int(retention_days)))
        await self.execute(
            "DELETE FROM call_records WHERE started_at<?",
            (cutoff.isoformat().replace("+00:00", "Z"),),
        )
        await self.prune_admin_sessions()

    async def record_error_event(
        self,
        *,
        capacity: int,
        level: str,
        category: str,
        code: str,
        user_name: str | None = None,
        method: str | None = None,
        path: str | None = None,
        status: int | None = None,
        message: str = "",
    ) -> int:
        size = max(1, min(int(capacity), 10_000))

        def operation(db: Any) -> int:
            row = db.execute("SELECT COALESCE(MAX(sequence),0) AS max_seq FROM error_ring").fetchone()
            sequence = int((row or {}).get("max_seq") or 0) + 1
            slot = (sequence - 1) % size
            db.execute("DELETE FROM error_ring WHERE slot>=?", (size,))
            db.execute(
                """
                INSERT INTO error_ring(
                    slot,sequence,occurred_at,level,category,user_name,method,path,status,code,message
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slot) DO UPDATE SET
                    sequence=excluded.sequence,
                    occurred_at=excluded.occurred_at,
                    level=excluded.level,
                    category=excluded.category,
                    user_name=excluded.user_name,
                    method=excluded.method,
                    path=excluded.path,
                    status=excluded.status,
                    code=excluded.code,
                    message=excluded.message
                """,
                (
                    slot,
                    sequence,
                    iso_now(),
                    level,
                    category,
                    user_name,
                    method,
                    path,
                    status,
                    code,
                    message,
                ),
            )
            return sequence

        return await self.call(operation)

    async def recent_error_events(self, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        return await self.all(
            """
            SELECT sequence,occurred_at,level,category,user_name,method,path,status,code,message
            FROM error_ring ORDER BY sequence DESC LIMIT ?
            """,
            (safe_limit,),
        )


def _usage_values(usage: dict[str, Any] | None) -> tuple[int, int, int, int, int, int]:
    if not isinstance(usage, dict):
        return 0, 0, 0, 0, 0, 1
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0) if isinstance(input_details, dict) else 0
    reasoning_tokens = (
        int(output_details.get("reasoning_tokens") or 0) if isinstance(output_details, dict) else 0
    )
    total_tokens = int(usage.get("total_tokens") or input_tokens + output_tokens)
    if input_tokens == 0 and output_tokens == 0 and total_tokens == 0:
        return 0, 0, 0, 0, 0, 1
    return input_tokens, output_tokens, cached_tokens, reasoning_tokens, total_tokens, 0


gateway_store = GatewayStore()
