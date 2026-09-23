from __future__ import annotations

from app.providers.antigravity import AntigravityAdapter
from app.providers.base import (
    CAP_VIDEO,
    ModelInfo,
    ProviderAdapter,
    canonical_image_model,
    default_model_for,
    looks_like_image_slug,
    looks_like_video_slug,
    resolve_provider_model,
)
from app.providers.codex import CodexAdapter
from app.providers.grok import GrokAdapter
from app.providers.workbuddy import WorkBuddyAdapter

_ADAPTERS: dict[str, ProviderAdapter] = {
    "codex": CodexAdapter(),
    "grok": GrokAdapter(),
    "antigravity": AntigravityAdapter(),
    "workbuddy": WorkBuddyAdapter(),
}


def get_adapter(provider: str) -> ProviderAdapter:
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise KeyError(provider)
    return adapter


def all_adapters() -> list[ProviderAdapter]:
    return list(_ADAPTERS.values())


def provider_catalog() -> list[ModelInfo]:
    models: list[ModelInfo] = []
    for adapter in all_adapters():
        models.extend(adapter.catalog())
    return models


def catalog_model(model_id: str) -> ModelInfo | None:
    raw = (model_id or "").strip()
    if not raw:
        return None
    provider, canonical = resolve_provider_model(raw)
    items = provider_catalog()
    alias_target: str | None = None
    matched: ModelInfo | None = None
    for item in items:
        if item.provider != provider or item.id not in {raw, canonical}:
            continue
        if item.alias_of:
            alias_target = item.alias_of
            matched = item
            continue
        return item
    if alias_target:
        for item in items:
            if item.provider == provider and item.id == alias_target:
                return item
    return matched


def is_image_model(model_id: str) -> bool:
    item = catalog_model(model_id)
    if item is not None:
        return item.type == "image"
    _provider, canonical = resolve_provider_model(model_id)
    return looks_like_image_slug(canonical)


def resolve_image_request_model(
    raw_model: str | None, *, default_provider: str
) -> tuple[str, str]:
    """Pick the provider from the request, then that provider's latest image model if needed."""
    provider, model = resolve_provider_model(
        raw_model,
        default_provider=default_provider,
        default_model=default_model_for(default_provider, "image"),
    )
    if not is_image_model(model):
        model = canonical_image_model(provider)
    return provider, model


def provider_supports_video(provider: str) -> bool:
    try:
        adapter = get_adapter(provider)
    except KeyError:
        return False
    if CAP_VIDEO in adapter.capabilities:
        return True
    return any(item.type == "video" for item in adapter.catalog())


def is_video_model(model_id: str) -> bool:
    item = catalog_model(model_id)
    if item is not None:
        return item.type == "video"
    _provider, canonical = resolve_provider_model(model_id)
    return looks_like_video_slug(canonical)


def resolve_video_request_model(
    raw_model: str | None, *, default_provider: str
) -> tuple[str, str]:
    """Pick a video-capable provider, then that provider's video model if the slug is text."""
    fallback = default_provider if provider_supports_video(default_provider) else "grok"
    default_model = default_model_for(fallback, "video")
    provider, model = resolve_provider_model(
        raw_model,
        default_provider=fallback,
        default_model=default_model or None,
    )
    if is_video_model(model):
        return provider, model
    if provider_supports_video(provider):
        video_model = default_model_for(provider, "video")
        if video_model:
            return provider, video_model
    return provider, model
