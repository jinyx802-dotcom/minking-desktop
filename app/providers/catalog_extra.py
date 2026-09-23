"""Models learned from an upstream list or typed in by an admin.

Static catalogs stay in each adapter. This module only holds the extra rows,
so a new id can show up without a code change. Manual rows win over a later
upstream refresh of the same id.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.providers.base import looks_like_image_slug, looks_like_video_slug

PROVIDERS = ("codex", "grok", "antigravity", "workbuddy")
MODEL_TYPES = ("text", "image", "video")


@dataclass(frozen=True, slots=True)
class ExtraModel:
    provider: str
    model_id: str
    model_type: str
    source: str


_items: dict[tuple[str, str], ExtraModel] = {}


def infer_model_type(model_id: str) -> str:
    if looks_like_video_slug(model_id):
        return "video"
    if looks_like_image_slug(model_id):
        return "image"
    return "text"


def normalize_model_id(provider: str, model_id: str) -> str:
    text = (model_id or "").strip()
    if provider == "workbuddy" and text and not text.startswith("workbuddy/"):
        return "workbuddy/" + text
    return text


def extras_for(provider: str) -> list[ExtraModel]:
    return [item for item in _items.values() if item.provider == provider]


def source_for(provider: str, model_id: str) -> str | None:
    item = _items.get((provider, model_id))
    return item.source if item else None


def replace_all(rows: list[ExtraModel]) -> None:
    global _items
    _items = {(row.provider, row.model_id): row for row in rows}


def set_upstream(provider: str, rows: list[tuple[str, str]]) -> None:
    """Replace this provider's upstream extras. Manual rows stay."""
    global _items
    kept = {
        key: item
        for key, item in _items.items()
        if not (key[0] == provider and item.source == "upstream")
    }
    for model_id, model_type in rows:
        key = (provider, model_id)
        current = kept.get(key)
        if current is not None and current.source == "manual":
            continue
        if model_type not in MODEL_TYPES:
            model_type = infer_model_type(model_id)
        kept[key] = ExtraModel(provider, model_id, model_type, "upstream")
    _items = kept


def upsert_manual(provider: str, model_id: str, model_type: str) -> ExtraModel:
    row = ExtraModel(provider, model_id, model_type, "manual")
    _items[(provider, model_id)] = row
    return row


def remove(provider: str, model_id: str) -> ExtraModel | None:
    return _items.pop((provider, model_id), None)


def parse_model_ids(payload: object) -> list[str]:
    """Read ids from an OpenAI list or a `{models: [...]}` payload."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("data")
    if not isinstance(raw, list):
        raw = payload.get("models")
    if isinstance(raw, dict):
        raw = list(raw.keys())
    if not isinstance(raw, list):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for item in raw:
        model_id = ""
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            for key in ("id", "slug", "name", "model"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    model_id = value
                    break
        model_id = model_id.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        found.append(model_id)
    return found


def parse_antigravity_model_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if isinstance(models, dict):
        found = [
            key.strip()
            for key in models
            if isinstance(key, str) and key.strip() and not key.startswith(("tab_", "chat_"))
        ]
    else:
        found = parse_model_ids(payload)
    from app.providers.antigravity import public_slug_for_variant

    return [item for item in found if not public_slug_for_variant(item)]
