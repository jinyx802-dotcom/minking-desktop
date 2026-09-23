"""Structured harness recipes for the MinKing desktop client.

These describe how to merge MinKing into local tools without uploading
official tokens. The raw API key is never included here.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.config import settings

DESKTOP_PROTOCOL = "minking"


def public_v1_url() -> str:
    clean = settings.gateway_public_base_url.rstrip("/")
    if not clean:
        return ""
    return clean if clean.endswith("/v1") else f"{clean}/v1"


def public_root_url() -> str:
    endpoint = public_v1_url()
    return endpoint[:-3] if endpoint.endswith("/v1") else endpoint


def desktop_import_url(*, email: str) -> str:
    """Deep link for the Windows client. Never includes the API key."""
    return (
        f"{DESKTOP_PROTOCOL}://import"
        f"?base={quote(public_v1_url(), safe='')}"
        f"&email={quote(email, safe='')}"
    )


def _copy_openai(v1: str, *, navigation: list[str], extra_fields: dict[str, str] | None = None) -> dict[str, Any]:
    fields = {
        "API 格式": "OpenAI Compatible / Chat Completions",
        "完整 URL": "关闭（只填到 /v1，不要补 /chat/completions）",
        "Base URL / 自定义请求地址": v1,
        "API Key": "你的 MinKing API Key（只在本机填写，不要发到聊天或截图）",
        "模型 ID": "以 GET /v1/models 返回的 slug 为准",
    }
    if extra_fields:
        fields.update(extra_fields)
    return {
        "base_url": v1,
        "api_format": "openai",
        "full_url": False,
        "navigation": navigation,
        "fields": fields,
    }


def harness_catalog(*, messages_ready: bool) -> list[dict[str, Any]]:
    v1 = public_v1_url()
    root = public_root_url()
    return [
        {
            "id": "codex",
            "display_name": "Codex",
            "one_click": True,
            "protocol": "responses",
            "detect": {"windows": ["%USERPROFILE%\\.codex"], "posix": ["$HOME/.codex"]},
            "live_dir": {"windows": "%USERPROFILE%\\.codex", "posix": "$HOME/.codex"},
            "snapshot_files": ["config.toml", "auth.json", ".env", "codex-models.json"],
            "cloud": {
                "base_url": v1,
                "provider_id": "minkingapi",
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
            },
        },
        {
            "id": "grok",
            "display_name": "Grok",
            "one_click": True,
            "protocol": "responses",
            "detect": {
                "windows": ["%USERPROFILE%\\.grok", "%USERPROFILE%\\.grok\\bin\\grok.exe"],
                "posix": ["$HOME/.grok", "$HOME/.grok/bin/grok"],
            },
            "live_dir": {"windows": "%USERPROFILE%\\.grok", "posix": "$HOME/.grok"},
            "snapshot_files": ["config.toml"],
            "cloud": {
                "base_url": v1,
                "provider_id": "minkingapi",
                "env_key": "MINKING_API_KEY",
                "api_backend": "responses",
            },
        },
        {
            "id": "workbuddy",
            "display_name": "WorkBuddy",
            "one_click": True,
            "protocol": "chat_completions",
            "detect": {
                "windows": [
                    "%USERPROFILE%\\.workbuddy",
                    "%USERPROFILE%\\.codebuddy",
                    "%LOCALAPPDATA%\\CodeBuddyExtension",
                ],
                "posix": [
                    "$HOME/.workbuddy",
                    "$HOME/.codebuddy",
                    "$HOME/Library/Application Support/CodeBuddyExtension",
                ],
            },
            "live_dir": {"windows": "%USERPROFILE%\\.workbuddy", "posix": "$HOME/.workbuddy"},
            "sync_dirs": {
                "windows": ["%USERPROFILE%\\.codebuddy"],
                "posix": ["$HOME/.codebuddy"],
            },
            "snapshot_files": ["models.json"],
            "cloud": {
                "model_id": "gpt-5.6-luna",
                "name": "gpt-5.6-luna",
                "vendor": "user",
                "url": v1,
            },
        },
        {
            "id": "claude_code",
            "display_name": "Claude Code",
            "one_click": messages_ready,
            "protocol": "anthropic_messages",
            "detect": {"windows": ["%USERPROFILE%\\.claude"], "posix": ["$HOME/.claude"]},
            "live_dir": {"windows": "%USERPROFILE%\\.claude", "posix": "$HOME/.claude"},
            "snapshot_files": ["settings.json"],
            "cloud": {
                "base_url": root,
                "env_key": "ANTHROPIC_API_KEY",
                "base_url_env": "ANTHROPIC_BASE_URL",
                "opus": "gpt-5.6-sol",
                "sonnet": "grok-4.6",
                "haiku": "gemini-3.8-flash",
                "fable": "gpt-6-astra",
            },
        },
        {
            "id": "zcode",
            "display_name": "ZCode",
            "one_click": True,
            "protocol": "chat_completions",
            "detect": {
                "windows": ["%USERPROFILE%\\.zcode", "%LOCALAPPDATA%\\Programs\\ZCode"],
                "posix": ["$HOME/.zcode"],
            },
            "live_dir": {"windows": "%USERPROFILE%\\.zcode\\v2", "posix": "$HOME/.zcode/v2"},
            "snapshot_files": ["provider_config.json"],
            "cloud": {
                "base_url": v1,
                "api_type": "openai-chat-completions",
                "config_file": "provider_config.json",
            },
            "copy": _copy_openai(
                v1,
                extra_fields={"供应商 ID / 模型 ID": "与网关模型 id 相同，例如 gpt-6-astra"},
                navigation=[
                    "打开 ZCode → 工作区点击模型 → 管理模型（或设置 → Model providers）",
                    "每个要用的模型单独「添加供应商」",
                    "供应商名称和模型 ID 都填该模型 id，例如 gpt-6-astra",
                    "协议选 OpenAI / Chat Completions",
                    "OpenAI 端点 / Base URL 粘贴下方地址（必须以 /v1 结尾）",
                    "API Key 填你的 MinKing API Key，打开该供应商的启用开关",
                    "也可由 MinKing 客户端一键按勾选模型写入 %USERPROFILE%\\.zcode\\v2\\provider_config.json（不覆盖其他供应商）",
                ],
            ),
        },
    ]


def messages_route_ready() -> bool:
    return True
