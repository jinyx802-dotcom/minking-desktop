from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import settings

PROVIDER_IDS = ("codex", "grok", "antigravity", "workbuddy")
CAP_CHAT = "chat"
CAP_STREAM = "stream"
CAP_RESPONSES = "responses"
CAP_IMAGE = "image"
CAP_IMAGE_EDIT = "image_edit"
CAP_VIDEO = "video"

_CODEX_ALIASES = {
    "gpt-6": "gpt-6-astra",
    "gpt6": "gpt-6-astra",
    "gpt-image-2.5": "gpt-image-2.5-flare",
}
_GROK_ALIASES = {
    "gpt-reserve": "grok-4.6",
}


@dataclass(frozen=True, slots=True)
class ImportedAccount:
    account_id: str
    payload: dict[str, Any]
    expires_at: str | None
    label: str | None = None
    auth_mode: str = "oauth"
    capabilities: tuple[str, ...] = ()
    source_path: str | None = None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    provider: str
    type: str
    capabilities: tuple[str, ...]
    default: bool = False
    owned_by: str = "system"
    alias_of: str | None = None


@dataclass(slots=True)
class AdapterContext:
    account: dict[str, Any]
    credentials: Any
    key: dict[str, Any]
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderAdapter(Protocol):
    id: str
    display_name: str
    capabilities: set[str]
    ready: bool

    def parse_import(self, payload: Any, filename: str) -> list[ImportedAccount]:
        ...

    def catalog(self) -> list[ModelInfo]:
        ...


def account_provider(row: dict[str, Any] | None) -> str:
    if not row:
        return "codex"
    value = str(row.get("provider") or "codex").strip().lower()
    return value if value in PROVIDER_IDS else "codex"


def canonicalize_model(model: str, provider: str) -> str:
    name = model.strip()
    if provider == "codex":
        return _CODEX_ALIASES.get(name, name)
    if provider == "antigravity":
        from app.providers.antigravity import public_slug_for_variant

        folded = public_slug_for_variant(name)
        if folded:
            return folded
    return name


def resolve_provider_model(
    model: str | None,
    *,
    default_provider: str = "codex",
    default_model: str | None = None,
) -> tuple[str, str]:
    raw = (model or "").strip()
    provider = default_provider if default_provider in PROVIDER_IDS else "codex"
    if "/" in raw:
        prefix, rest = raw.split("/", 1)
        prefix = prefix.strip().lower()
        if prefix in PROVIDER_IDS:
            provider = prefix
            raw = rest.strip()
    elif raw.startswith(("grok-", "grok_")) or raw == "grok":
        provider = "grok"
    elif raw.startswith(("gemini-", "claude-")):
        provider = "antigravity"
    elif raw.startswith(("gpt-", "codex-", "o1", "o3", "o4")):
        provider = "codex"
    if raw in _GROK_ALIASES:
        provider = "grok"
        raw = _GROK_ALIASES[raw]

    if not raw:
        if default_model:
            raw = default_model
        elif provider == "grok":
            raw = settings.grok_default_model
        elif provider == "antigravity":
            raw = "gemini-3.8-flash"
        elif provider == "workbuddy":
            raw = "glm-5.2"
        else:
            raw = settings.codex_default_model
    return provider, canonicalize_model(raw, provider)


def default_model_for(provider: str, kind: str) -> str:
    if kind == "video":
        if provider == "grok":
            return settings.grok_video_model
        return ""
    if provider == "grok":
        if kind == "image":
            return settings.grok_image_model
        return settings.grok_default_model
    if provider == "antigravity":
        if kind == "image":
            return settings.antigravity_image_model
        return "gemini-3.8-flash"
    if provider == "workbuddy":
        if kind == "image":
            return settings.workbuddy_image_model
        return "glm-5.2"
    if kind == "image":
        return settings.codex_image_model
    return settings.codex_default_model


def canonical_image_model(provider: str) -> str:
    return default_model_for(provider, "image")


def looks_like_image_slug(model: str) -> bool:
    name = (model or "").strip().lower()
    if not name:
        return False
    return (
        name.startswith("gpt-image-")
        or "imagine-image" in name
        or "flash-image" in name
        or "hunyuan-image" in name
        or name.startswith("hy-image")
    )


def looks_like_video_slug(model: str) -> bool:
    name = (model or "").strip().lower()
    if not name:
        return False
    return (
        "imagine-video" in name
        or name.startswith("sora-")
        or "veo-" in name
        or name.endswith("-veo")
        or "hunyuan-video" in name
        or name.startswith("kling")
    )


def parse_capabilities(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item)]
        return [item.strip() for item in text.split(",") if item.strip()]
    return []
