"""Harness recipes. Mirrors app/desktop_recipes.py without importing the gateway."""

from __future__ import annotations

from typing import Any

from minking_desktop.paths import public_root_url, public_v1_url


def harness_catalog(*, public_base: str, messages_ready: bool = True) -> list[dict[str, Any]]:
    v1 = public_v1_url(public_base)
    root = public_root_url(public_base)
    return [
        {
            "id": "codex",
            "display_name": "Codex",
            "one_click": True,
            "protocol": "responses",
            "detect": {"windows": [r"%USERPROFILE%\.codex"], "posix": ["$HOME/.codex"]},
            "live_dir": {"windows": r"%USERPROFILE%\.codex", "posix": "$HOME/.codex"},
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
                "windows": [r"%USERPROFILE%\.grok", r"%USERPROFILE%\.grok\bin\grok.exe"],
                "posix": ["$HOME/.grok", "$HOME/.grok/bin/grok"],
            },
            "live_dir": {"windows": r"%USERPROFILE%\.grok", "posix": "$HOME/.grok"},
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
                    r"%USERPROFILE%\.workbuddy",
                    r"%USERPROFILE%\.codebuddy",
                    r"%LOCALAPPDATA%\CodeBuddyExtension",
                ],
                "posix": [
                    "$HOME/.workbuddy",
                    "$HOME/.codebuddy",
                    "$HOME/Library/Application Support/CodeBuddyExtension",
                ],
            },
            "live_dir": {"windows": r"%USERPROFILE%\.workbuddy", "posix": "$HOME/.workbuddy"},
            "sync_dirs": {
                "windows": [r"%USERPROFILE%\.codebuddy"],
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
            "detect": {"windows": [r"%USERPROFILE%\.claude"], "posix": ["$HOME/.claude"]},
            "live_dir": {"windows": r"%USERPROFILE%\.claude", "posix": "$HOME/.claude"},
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
                "windows": [r"%USERPROFILE%\.zcode", r"%LOCALAPPDATA%\Programs\ZCode"],
                "posix": ["$HOME/.zcode"],
            },
            "live_dir": {"windows": r"%USERPROFILE%\.zcode\v2", "posix": "$HOME/.zcode/v2"},
            "snapshot_files": ["provider_config.json"],
            "cloud": {
                "base_url": v1,
                "api_type": "openai-chat-completions",
            },
            "copy": {"base_url": v1, "api_format": "openai"},
        },
    ]


def recipe_by_id(harness_id: str, *, public_base: str, messages_ready: bool = True) -> dict[str, Any]:
    for item in harness_catalog(public_base=public_base, messages_ready=messages_ready):
        if item["id"] == harness_id:
            return item
    raise KeyError(harness_id)
