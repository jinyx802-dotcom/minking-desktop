from __future__ import annotations

from pathlib import Path
from typing import Any

from app.providers.base import ModelInfo

# Codex on Windows does not expand %USERPROFILE%. ZIP is built on the server
# and cannot know the downloader's account name.
WINDOWS_CODEX_CATALOG_PLACEHOLDER = "C:/Users/你的用户名/.codex/codex-models.json"


def codex_catalog_toml_path(home: Path) -> str:
    return (Path(home) / ".codex" / "codex-models.json").as_posix()

_BASE_INSTRUCTIONS = (
    "You are a coding agent working in the user's workspace. "
    "Complete the requested work with tools, keep updates concise, "
    "and do not invent files or commands you have not inspected."
)

_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
)
# Catalog windows advertised to clients. Official list values still override these.
CODEX_CONTEXT_WINDOW = 2_560_000
GROK_CONTEXT_WINDOW = 50_000_000
DEFAULT_CONTEXT_WINDOW = 256_000
_CODEX_IMAGE_URL_OVERRIDES = frozenset({"b64", "b64_json", "base64", "raw", "json"})
_IMAGE_CAPABLE_PROVIDERS = frozenset({"codex", "grok"})


def context_window_for(model_id: str, provider: str | None = None) -> int:
    raw = (model_id or "").strip()
    prefix = (provider or "").strip().lower()
    name = raw
    if not prefix and "/" in raw:
        prefix, name = raw.split("/", 1)
        prefix = prefix.strip().lower()
    name = name.strip().lower()
    if prefix == "grok" or name.startswith("grok-") or name.startswith("grok_"):
        return GROK_CONTEXT_WINDOW
    if prefix in {"codex", "openai"} or name.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
        return CODEX_CONTEXT_WINDOW
    return DEFAULT_CONTEXT_WINDOW


def _picker_image_generation_tools(item: ModelInfo) -> list[str]:
    if item.type != "text":
        return []
    if item.provider in _IMAGE_CAPABLE_PROVIDERS:
        return ["image_generation"]
    if item.provider == "antigravity" and item.id.startswith("gemini-") and "flash-image" not in item.id:
        return ["image_generation"]
    return []


def is_codex_client_ua(user_agent: str | None) -> bool:
    agent = (user_agent or "").lower()
    if not agent:
        return False
    return (
        "codex_cli" in agent
        or "codex_exec" in agent
        or "codex/" in agent
        or "codex-desktop" in agent
        or "codex desktop" in agent
    )


def wants_codex_catalog(client_version: str | None, user_agent: str | None) -> bool:
    if client_version and client_version.strip():
        return True
    return is_codex_client_ua(user_agent)


def wants_codex_image_urls(user_agent: str | None, image_mode: str | None) -> bool:
    mode = (image_mode or "").strip().lower()
    if mode in _CODEX_IMAGE_URL_OVERRIDES:
        return False
    if mode == "url":
        return True
    return is_codex_client_ua(user_agent)


def display_name_for(model_id: str) -> str:
    aliases = {
        "gpt-6-astra": "GPT-6 Astra",
        "gpt-5.6-sol": "GPT-5.6 Sol",
        "gpt-5.6-terra": "GPT-5.6 Terra",
        "gpt-5.6-luna": "GPT-5.6 Luna",
        "gpt-5.5": "GPT-5.5",
        "gpt-reserve": "Grok 4.6",
        "grok-4.6": "Grok 4.6",
        "grok-4.5": "Grok 4.5",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3.7-flash": "Gemini 3.7 Flash",
        "gemini-3.1-pro": "Gemini 3.1 Pro",
        "claude-sonnet-4-6": "Claude Sonnet 4.6",
        "claude-opus-4-6-thinking": "Claude Opus 4.6 Thinking",
        "codex-auto-review": "Codex Auto Review",
        "workbuddy/glm-5.2": "GLM 5.2",
        "workbuddy/glm-5.1": "GLM 5.1",
        "workbuddy/glm-5v-turbo": "GLM 5V Turbo",
        "workbuddy/kimi-k2.7": "Kimi K2.7",
        "workbuddy/minimax-m3-pay": "MiniMax M3",
        "workbuddy/hy3": "Hunyuan 3",
        "workbuddy/hy3-preview": "Hunyuan 3 Preview",
        "workbuddy/hy3-preview-agent": "Hunyuan 3 Agent",
        "workbuddy/deepseek-v4-pro": "DeepSeek V4 Pro",
        "workbuddy/deepseek-v4-flash": "DeepSeek V4 Flash",
    }
    if model_id in aliases:
        return aliases[model_id]
    return model_id.replace("-", " ").title()


def picker_entry(item: ModelInfo, *, priority: int) -> dict[str, Any]:
    return {
        "slug": item.id,
        "display_name": display_name_for(item.id),
        "description": display_name_for(item.id),
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "context_window": context_window_for(item.id, item.provider),
        "max_context_window": context_window_for(item.id, item.provider),
        "effective_context_window_percent": 95,
        "default_reasoning_level": "medium",
        "default_reasoning_summary": "none",
        "supported_reasoning_levels": list(_REASONING_LEVELS),
        "default_verbosity": "low",
        "support_verbosity": True,
        "supports_parallel_tool_calls": True,
        "supports_reasoning_summaries": True,
        "supports_search_tool": True,
        "supports_image_detail_original": True,
        "input_modalities": ["text", "image"],
        "shell_type": "unified_exec" if item.provider == "codex" else "shell_command",
        "apply_patch_tool_type": "freeform",
        "experimental_supported_tools": _picker_image_generation_tools(item),
        "additional_speed_tiers": (["fast"] if item.provider == "grok" and item.type == "text" else []),
        "service_tiers": (
            [
                {
                    "id": "priority",
                    "name": "Fast",
                    "description": "Higher scheduling priority",
                }
            ]
            if item.provider == "grok" and item.type == "text"
            else []
        ),
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        # API-key clients do not install Codex web.run. Advertising lite makes
        # them drop hosted search. The gateway still sends the lite header upstream.
        "use_responses_lite": False,
        "availability_nux": None,
        "upgrade": None,
        "web_search_tool_type": "text_and_image",
        "base_instructions": _BASE_INSTRUCTIONS,
        "model_messages": {
            "instructions_template": _BASE_INSTRUCTIONS,
            "instructions_variables": None,
        },
        "comp_hash": "ts-catalog",
    }


_OFFICIAL_OVERLAY_KEYS = (
    "display_name",
    "description",
    "visibility",
    "supported_in_api",
    "context_window",
    "max_context_window",
    "effective_context_window_percent",
    "default_reasoning_level",
    "default_reasoning_summary",
    "supported_reasoning_levels",
    "default_verbosity",
    "support_verbosity",
    "supports_parallel_tool_calls",
    "supports_reasoning_summaries",
    "supports_search_tool",
    "supports_image_detail_original",
    "input_modalities",
    "shell_type",
    "apply_patch_tool_type",
    "experimental_supported_tools",
    "additional_speed_tiers",
    "service_tiers",
    "truncation_policy",
    "use_responses_lite",
    "web_search_tool_type",
    "base_instructions",
    "model_messages",
    "comp_hash",
    "tool_mode",
)


def picker_entry_for_id(
    model_id: str,
    *,
    provider: str = "codex",
    priority: int = 1,
    model_type: str = "text",
) -> dict[str, Any]:
    return picker_entry(
        ModelInfo(id=model_id, provider=provider, type=model_type, capabilities=()),
        priority=priority,
    )


def official_catalog_overlay(row: dict[str, Any]) -> dict[str, Any]:
    """Keep official Codex catalog fields. Drop anything that is not a catalog value."""
    overlay: dict[str, Any] = {}
    for key in _OFFICIAL_OVERLAY_KEYS:
        if key not in row or row[key] is None:
            continue
        value = row[key]
        if isinstance(value, str) and len(value) > 100_000:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)):
            overlay[key] = value
    return overlay
