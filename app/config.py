from __future__ import annotations

from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8787
    app_root_path: str = ""
    log_level: str = "info"
    access_log_enabled: bool = True
    gateway_request_logging_enabled: bool = False
    gateway_diagnostic_logging_enabled: bool = False
    gateway_codebuddy_connection_close_enabled: bool = True
    admin_initial_password: str = ""
    admin_session_hours: int = 12
    admin_login_window_minutes: int = 15
    admin_login_max_failures: int = 5
    routing_secret: str = ""
    wallet_credit_secret: str = ""
    antigravity_oauth_client_id: str = ""
    antigravity_oauth_client_secret: str = ""
    data_dir: Path = Path("./data")
    mysql_host: str = ""
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = ""
    mysql_database: str = ""

    gateway_max_import_files: int = 50
    gateway_max_auth_file_bytes: int = 200_000
    gateway_session_ttl_days: int = 30
    gateway_response_ttl_days: int = 30
    gateway_account_max_concurrency: int = 24
    gateway_queue_timeout_seconds: float = 5.0
    gateway_request_body_timeout_seconds: float = 120.0
    gateway_cooldown_seconds: int = 600
    gateway_circuit_breaker_seconds: int = 600
    gateway_network_error_threshold: int = 3
    gateway_max_connections: int = 100
    gateway_max_keepalive_connections: int = 40
    gateway_pool_timeout_seconds: float = 5.0
    gateway_pool_drain_seconds: float = 330.0
    gateway_error_ring_capacity: int = 200
    gateway_call_retention_days: int = 90
    gateway_skip_call_recovery_on_startup: bool = False
    gateway_public_base_url: str = ""
    portal_enabled: bool = False
    portal_smtp_host: str = ""
    portal_smtp_port: int = 587
    portal_smtp_username: str = ""
    portal_smtp_password: str = ""
    portal_smtp_from: str = ""
    portal_smtp_ssl: bool = False
    gateway_file_input_ttl_hours: int = 24
    gateway_image_output_ttl_minutes: int = 60
    gateway_image_artifact_max_file_bytes: int = 25 * 1024 * 1024
    gateway_image_artifact_max_request_bytes: int = 50 * 1024 * 1024
    gateway_input_image_max_edge: int = 1280
    gateway_input_image_max_bytes: int = 250 * 1024
    gateway_image_cleanup_seconds: int = 600
    gateway_stream_bootstrap_seconds: float = 0.75
    gateway_first_token_timeout_seconds: float = 45.0
    gateway_stream_idle_timeout_seconds: float = 300.0
    gateway_stream_bootstrap_max_bytes: int = 256 * 1024
    gateway_sse_max_event_bytes: int = 64 * 1024 * 1024
    gateway_quota_cache_seconds: int = 600
    gateway_quota_max_concurrency: int = 3
    gateway_quota_timeout_seconds: float = 15.0

    codex_upstream_url: str = "https://chatgpt.com/backend-api/codex"
    codex_chatgpt_backend_url: str = "https://chatgpt.com/backend-api"
    codex_platform_upstream_url: str = "https://api.openai.com/v1"
    codex_refresh_url: str = "https://auth.openai.com/oauth/token"
    codex_client_id: str = "app_EMoamEEZ73f0CkXaXp7hrann"
    # Seed until npm @openai/codex latest replaces it. Only stable major.minor.patch is kept.
    codex_client_version: str = "0.156.0"
    codex_originator: str = "codex_cli_rs"
    codex_default_model: str = "gpt-6-astra"
    codex_image_model: str = "gpt-image-2.5-flare"
    codex_service_tier: str | None = None
    codex_max_concurrency: int = 4
    codex_queue_timeout_seconds: float = 10.0
    codex_chat_timeout_seconds: float = 300.0
    codex_image_timeout_seconds: float = 300.0
    codex_responses_lite_models: str = (
        "gpt-6-astra,gpt-6-sol,gpt-6-luna,gpt-5.6-sol,gpt-5.6-sol-wm,gpt-5.6-terra,gpt-5.6-luna,codex-auto-review"
    )

    grok_oauth_base_url: str = "https://cli-chat-proxy.grok.com/v1"
    grok_api_base_url: str = "https://api.x.ai/v1"
    grok_refresh_url: str = "https://auth.x.ai/oauth2/token"
    grok_cli_version: str = "1.0.30"
    grok_default_model: str = "grok-4.6"
    grok_image_model: str = "grok-imagine-image-2.0"
    grok_video_model: str = "grok-imagine-video-1.5"
    grok_video_edit_model: str = "grok-imagine-video"
    grok_chat_timeout_seconds: float = 300.0
    grok_image_timeout_seconds: float = 300.0
    grok_video_timeout_seconds: float = 300.0
    grok_quota_timeout_seconds: float = 5.0
    grok_home: str = ""
    gateway_import_local_enabled: bool = True

    antigravity_image_model: str = "gemini-3.1-flash-image"
    antigravity_image_timeout_seconds: float = 300.0
    workbuddy_image_model: str = "hunyuan-image-v3.0"

    @field_validator("gateway_public_base_url")
    @classmethod
    def _normalize_gateway_public_base_url(cls, value: str) -> str:
        val = value.strip().rstrip("/")
        if val.endswith("/v1"):
            val = val[:-3].rstrip("/")
        return val

    @field_validator("app_root_path")
    @classmethod
    def _normalize_app_root_path(cls, value: str) -> str:
        path = value.strip()
        if path in {"", "/"}:
            return ""
        if not path.startswith("/"):
            raise ValueError("APP_ROOT_PATH must start with /")
        if "?" in path or "#" in path:
            raise ValueError("APP_ROOT_PATH must contain only a URL path")
        return path.rstrip("/")

    @model_validator(mode="after")
    def _resolve_paths(self) -> Settings:
        self.data_dir = self.data_dir.expanduser().resolve()
        return self

    @property
    def is_public_bind(self) -> bool:
        host = self.host.strip().lower()
        return host not in {"127.0.0.1", "localhost", "::1"}

    def gateway_credential_dir(self) -> Path:
        path = self.data_dir / "gateway-credentials"
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def gateway_db_path(self) -> Path:
        return self.data_dir / "gateway.sqlite3"

    @property
    def uses_mysql(self) -> bool:
        return bool(self.mysql_host.strip() and self.mysql_database.strip())

    def gateway_image_artifact_dir(self) -> Path:
        path = self.data_dir / "image-artifacts"
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def lite_models(self) -> set[str]:
        return {
            item.strip()
            for item in self.codex_responses_lite_models.split(",")
            if item.strip()
        }


settings = Settings()
