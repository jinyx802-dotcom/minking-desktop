from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlsplit

from app.config import settings
from app.providers.base import default_model_for, looks_like_video_slug

OPENAI_VIDEO_STATUSES = frozenset({"queued", "in_progress", "completed", "failed"})
OPENAI_CONTENT_VARIANTS = frozenset({"video", "thumbnail", "spritesheet"})

_GROK_VIDEO_KEEP = (
    "model",
    "prompt",
    "duration",
    "aspect_ratio",
    "resolution",
    "image",
    "images",
    "last_frame",
    "reference_images",
    "n",
    "user",
    "generate_audio",
)
_OPENAI_SIZE_TO_GROK = {
    "1280x720": ("16:9", "720p"),
    "1920x1080": ("16:9", "1080p"),
    "854x480": ("16:9", "480p"),
    "640x360": ("16:9", "480p"),
    "720x1280": ("9:16", "720p"),
    "1080x1920": ("9:16", "1080p"),
    "1024x1792": ("9:16", "720p"),
    "1792x1024": ("16:9", "720p"),
    "720x720": ("1:1", "720p"),
    "1024x1024": ("1:1", "720p"),
    "1080x1080": ("1:1", "1080p"),
    "480x480": ("1:1", "480p"),
}
_GROK_TO_OPENAI_SIZE = {
    ("16:9", "480p"): "854x480",
    ("16:9", "720p"): "1280x720",
    ("16:9", "1080p"): "1920x1080",
    ("9:16", "480p"): "480x854",
    ("9:16", "720p"): "720x1280",
    ("9:16", "1080p"): "1080x1920",
    ("1:1", "480p"): "480x480",
    ("1:1", "720p"): "720x720",
    ("1:1", "1080p"): "1080x1080",
    ("4:3", "720p"): "1280x960",
    ("3:4", "720p"): "960x1280",
}
_GROK_STATUS = {
    "": "queued",
    "queued": "queued",
    "pending": "in_progress",
    "processing": "in_progress",
    "running": "in_progress",
    "in_progress": "in_progress",
    "done": "completed",
    "completed": "completed",
    "succeeded": "completed",
    "success": "completed",
    "failed": "failed",
    "error": "failed",
    "expired": "failed",
    "cancelled": "failed",
    "canceled": "failed",
}


def canonical_video_model(provider: str) -> str:
    return default_model_for(provider, "video") or settings.grok_video_model


def unix_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())


def openai_video_object(
    *,
    video_id: str,
    model: str,
    status: str,
    created_at: int,
    completed_at: int | None = None,
    progress: int = 0,
    seconds: str | None = None,
    size: str | None = None,
    error: dict[str, Any] | None = None,
    remixed_from_video_id: str | None = None,
) -> dict[str, Any]:
    mapped = status if status in OPENAI_VIDEO_STATUSES else "in_progress"
    payload: dict[str, Any] = {
        "id": video_id,
        "object": "video",
        "created_at": created_at,
        "completed_at": completed_at if mapped == "completed" else None,
        "status": mapped,
        "model": model,
        "progress": 100 if mapped == "completed" else max(0, min(100, int(progress))),
        "error": error if mapped == "failed" else None,
        "remixed_from_video_id": remixed_from_video_id,
    }
    if seconds not in {None, ""}:
        payload["seconds"] = str(seconds)
    if size:
        payload["size"] = str(size)
    return payload


def openai_video_list(items: list[dict[str, Any]], *, has_more: bool) -> dict[str, Any]:
    return {
        "object": "list",
        "data": items,
        "first_id": items[0]["id"] if items else None,
        "last_id": items[-1]["id"] if items else None,
        "has_more": has_more,
    }


def openai_video_deleted(video_id: str) -> dict[str, Any]:
    return {"id": video_id, "deleted": True, "object": "video.deleted"}


def grok_edit_video_model(model: str | None) -> str:
    name = (model or "").strip()
    if not name or "1.5" in name:
        return settings.grok_video_edit_model
    if looks_like_video_slug(name):
        return name
    return settings.grok_video_edit_model


def map_provider_video_status(provider: str, raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if provider == "grok":
        return _GROK_STATUS.get(value, "in_progress" if value else "queued")
    if value in OPENAI_VIDEO_STATUSES:
        return value
    return "queued" if not value else "in_progress"


def duration_seconds(value: Any) -> int | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    try:
        number = int(text)
    except ValueError:
        try:
            number = int(float(text))
        except ValueError:
            return None
    return number if number > 0 else None


def openai_size_for_grok(aspect_ratio: str | None, resolution: str | None) -> str | None:
    aspect = (aspect_ratio or "").strip()
    res = (resolution or "").strip().lower()
    if (aspect, res) in _GROK_TO_OPENAI_SIZE:
        return _GROK_TO_OPENAI_SIZE[(aspect, res)]
    if res in {"480p", "720p", "1080p"} and not aspect:
        return _GROK_TO_OPENAI_SIZE.get(("16:9", res))
    return None


def grok_size_from_openai(size: str) -> tuple[str | None, str | None]:
    raw = size.strip().lower().replace(" ", "")
    if raw in {"480p", "720p", "1080p"}:
        return None, raw
    if raw in _OPENAI_SIZE_TO_GROK:
        return _OPENAI_SIZE_TO_GROK[raw]
    if "x" not in raw:
        return None, None
    width_text, height_text = raw.split("x", 1)
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError:
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    ratio = width / height
    aspects = (
        (1.0, "1:1"),
        (16 / 9, "16:9"),
        (9 / 16, "9:16"),
        (4 / 3, "4:3"),
        (3 / 4, "3:4"),
        (3 / 2, "3:2"),
        (2 / 3, "2:3"),
    )
    aspect = min(aspects, key=lambda item: abs(item[0] - ratio))[1]
    longest = max(width, height)
    if longest >= 1600:
        resolution = "1080p"
    elif longest >= 900:
        resolution = "720p"
    else:
        resolution = "480p"
    return aspect, resolution


def _image_ref(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value.strip():
        return {"url": value.strip()}
    if not isinstance(value, dict):
        return None
    file_id = value.get("file_id")
    if isinstance(file_id, str) and file_id.strip():
        return {"file_id": file_id.strip()}
    url = value.get("url") or value.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url.strip():
        return {"url": url.strip()}
    return None


def shape_grok_video_body(body: dict[str, Any]) -> dict[str, Any]:
    payload = {key: body[key] for key in _GROK_VIDEO_KEEP if key in body}
    if "duration" not in payload:
        duration = duration_seconds(body.get("duration", body.get("seconds")))
        if duration is not None:
            payload["duration"] = duration
    if "resolution" not in payload or "aspect_ratio" not in payload:
        size = body.get("size")
        if isinstance(size, str) and size.strip():
            aspect, resolution = grok_size_from_openai(size)
            if aspect and "aspect_ratio" not in payload:
                payload["aspect_ratio"] = aspect
            if resolution and "resolution" not in payload:
                payload["resolution"] = resolution
    image = _image_ref(payload.get("image") if "image" in payload else body.get("input_reference") or body.get("image"))
    if image is not None:
        payload["image"] = image
    elif "image" in payload and not isinstance(payload.get("image"), dict):
        payload.pop("image", None)
    return payload


def requested_video_fields(body: dict[str, Any]) -> tuple[str | None, str | None]:
    duration = duration_seconds(body.get("seconds", body.get("duration")))
    seconds = str(duration) if duration is not None else None
    size = body.get("size")
    if isinstance(size, str) and size.strip():
        if "x" in size.lower():
            return seconds, size.strip()
        aspect, resolution = grok_size_from_openai(size)
        mapped = openai_size_for_grok(aspect, resolution)
        return seconds, mapped or size.strip()
    aspect = body.get("aspect_ratio") if isinstance(body.get("aspect_ratio"), str) else None
    resolution = body.get("resolution") if isinstance(body.get("resolution"), str) else None
    return seconds, openai_size_for_grok(aspect, resolution)


def grok_video_id(payload: dict[str, Any]) -> str:
    for key in ("id", "request_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def grok_video_progress(payload: dict[str, Any]) -> int:
    progress = payload.get("progress")
    if isinstance(progress, bool) or progress is None:
        return 0
    try:
        return max(0, min(100, int(progress)))
    except (TypeError, ValueError):
        return 0


def grok_video_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "video_generation_failed")
        message = str(error.get("message") or "Video generation failed")[:300]
        return {"code": code, "message": message}
    if isinstance(error, str) and error.strip():
        return {"code": "video_generation_failed", "message": error.strip()[:300]}
    return None


def grok_result_seconds(payload: dict[str, Any], fallback: str | None) -> str | None:
    video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    duration = duration_seconds(video.get("duration", payload.get("duration")))
    if duration is not None:
        return str(duration)
    return fallback


def grok_result_size(payload: dict[str, Any], fallback: str | None) -> str | None:
    video = payload.get("video") if isinstance(payload.get("video"), dict) else {}
    size = video.get("size") or payload.get("size")
    if isinstance(size, str) and size.strip():
        return size.strip()
    aspect = video.get("aspect_ratio") or payload.get("aspect_ratio")
    resolution = video.get("resolution") or payload.get("resolution")
    mapped = openai_size_for_grok(
        aspect if isinstance(aspect, str) else None,
        resolution if isinstance(resolution, str) else None,
    )
    return mapped or fallback


def grok_content_url(payload: dict[str, Any]) -> str | None:
    video = payload.get("video") if isinstance(payload.get("video"), dict) else None
    candidates = []
    if isinstance(video, dict):
        candidates.extend([video.get("url"), video.get("video_url")])
    candidates.extend([payload.get("url"), payload.get("video_url")])
    for item in candidates:
        if isinstance(item, str) and item.startswith(("http://", "https://")):
            return item
    return None


def video_download_headers(url: str, grok_headers: dict[str, str]) -> dict[str, str]:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("x.ai") or host.endswith("grok.com"):
        headers = dict(grok_headers)
        headers.pop("Content-Type", None)
        headers["Accept"] = "*/*"
        return headers
    return {"Accept": "*/*"}


def _video_ref(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith(("http://", "https://", "data:")):
            return {"url": text}
        return {"id": text}
    if not isinstance(value, dict):
        return None
    url = value.get("url") or value.get("video_url")
    if isinstance(url, dict):
        url = url.get("url")
    if isinstance(url, str) and url.strip():
        return {"url": url.strip()}
    file_id = value.get("file_id")
    if isinstance(file_id, str) and file_id.strip():
        return {"file_id": file_id.strip()}
    video_id = value.get("id") or value.get("video_id")
    if isinstance(video_id, str) and video_id.strip():
        return {"id": video_id.strip()}
    return None


def source_video_id(body: dict[str, Any]) -> str | None:
    ref = _video_ref(body.get("video"))
    if ref and ref.get("id"):
        return ref["id"]
    return None


def shape_grok_video_edit_body(body: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    prompt = body.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        payload["prompt"] = prompt.strip()
    video = _video_ref(body.get("video"))
    if video:
        payload["video"] = {key: video[key] for key in ("url", "file_id") if key in video}
        if not payload["video"]:
            payload.pop("video", None)
    return payload


def shape_grok_video_extend_body(body: dict[str, Any]) -> dict[str, Any]:
    payload = shape_grok_video_edit_body(body)
    duration = duration_seconds(body.get("duration", body.get("seconds")))
    if duration is not None:
        payload["duration"] = duration
    return payload
