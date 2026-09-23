"""Shared official image protocol, extracted unchanged from the production gateway."""
from __future__ import annotations

import base64
import json
from typing import Any
from app.config import settings


def _image_request_assets(
    *,
    json_body: dict[str, Any] | None,
    files: list[Any] | None,
) -> tuple[dict[str, Any], list[str], str | None]:
    values: dict[str, Any] = dict(json_body or {})
    images: list[str] = []
    mask: str | None = None
    if files:
        for name, part in files:
            if not isinstance(part, tuple) or len(part) < 2:
                continue
            filename, content = part[0], part[1]
            content_type = part[2] if len(part) > 2 else None
            if filename is None:
                values[name] = content
                continue
            if not isinstance(content, bytes):
                continue
            encoded = _image_data_url(content, str(content_type or "application/octet-stream"))
            if name == "mask":
                mask = encoded
            elif name in {"image", "image[]"}:
                images.append(encoded)

    if json_body:
        raw_images = json_body.get("image")
        if isinstance(raw_images, str):
            images.append(raw_images)
        elif isinstance(raw_images, list):
            images.extend(item for item in raw_images if isinstance(item, str))
        if isinstance(json_body.get("mask"), str):
            mask = str(json_body["mask"])
    return values, images, mask


def _image_responses_payload(
    kind: str,
    *,
    json_body: dict[str, Any] | None,
    files: list[Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    values, images, mask = _image_request_assets(json_body=json_body, files=files)
    image_model = str(values.get("model") or settings.codex_image_model)
    tool: dict[str, Any] = {
        "type": "image_generation",
        "action": "generate" if kind == "generation" else "edit",
        "model": image_model,
    }
    option_fields = (
        "size",
        "quality",
        "background",
        "output_format",
        "output_compression",
        "partial_images",
        "moderation",
    )
    for field in option_fields:
        if field in values:
            tool[field] = values[field]
    if kind == "edit" and "input_fidelity" in values:
        tool["input_fidelity"] = values["input_fidelity"]
    if mask:
        tool["input_image_mask"] = {"image_url": mask}

    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": str(values.get("prompt") or "")}
    ]
    content.extend({"type": "input_image", "image_url": image} for image in images)
    request_body = {
        "model": settings.codex_default_model,
        "store": False,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": content}],
        "tools": [tool],
        "tool_choice": {"type": "image_generation"},
        "parallel_tool_calls": False,
        "include": ["reasoning.encrypted_content"],
    }
    return request_body, tool


def _image_results_from_sse(
    events: list[tuple[str, str]], completed: dict[str, Any]
) -> list[dict[str, str]]:
    """Collect final image data from both streaming item events and completion output."""
    final_results: list[str] = []
    last_partial: str | None = None

    def add_final(value: Any) -> None:
        if isinstance(value, str) and value and value not in final_results:
            final_results.append(value)

    for event_name, data in events:
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or event_name)
        if event_type == "response.output_item.done":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "image_generation_call":
                add_final(item.get("result"))
        elif event_type == "response.image_generation_call.completed":
            add_final(payload.get("result"))
        elif event_type == "response.image_generation_call.partial_image":
            partial = payload.get("partial_image_b64")
            if isinstance(partial, str) and partial:
                last_partial = partial

    output = completed.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "image_generation_call":
                add_final(item.get("result"))

    results = final_results or ([last_partial] if last_partial else [])
    return [{"b64_json": result} for result in results]


def _image_data_url(raw: bytes, content_type: str) -> str:
    mime = content_type if content_type in {"image/png", "image/jpeg", "image/webp"} else "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
