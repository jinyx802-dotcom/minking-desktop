from __future__ import annotations

import json
import time
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException

from app.config import settings

RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"


def codex_failed_code(status: int) -> str:
    """Map HTTP status onto Codex's retry whitelist.

    400/401/403/422 become permanent ``invalid_prompt`` so Codex surfaces the
    error and stops. 429 stays retryable. Everything else is a transient
    ``server_error``.
    """
    if status in {400, 401, 403, 422}:
        return "invalid_prompt"
    if status == 429:
        return "rate_limit_exceeded"
    return "server_error"


def responses_failed_sse(*, response_id: str, code: str, message: str) -> str:
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "output": [],
        },
    }
    failed = {
        "type": "response.failed",
        "sequence_number": 1,
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "output": [],
            "error": {"code": code, "message": message},
        },
    }
    return (
        f"event: response.created\ndata: {json.dumps(created, ensure_ascii=False)}\n\n"
        f"event: response.failed\ndata: {json.dumps(failed, ensure_ascii=False)}"
    )


def responses_lite_enabled(model: str) -> bool:
    names = settings.lite_models()
    return any(model == name or model.startswith(f"{name}-") for name in names)


def _developer_text(item: Any) -> str:
    if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "developer":
        return ""
    return _content_to_text(item.get("content"))


def _tool_key(tool: Any) -> str:
    try:
        return json.dumps(tool, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(tool)


def _visible_text(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return bool(value)
    return False


def _readable_compact(value: str) -> bool:
    from app.providers.grok import COMPACT_SUMMARY_PREFIX

    return value.startswith(COMPACT_SUMMARY_PREFIX)


def _drop_foreign_encrypted_content(item: dict[str, Any]) -> None:
    """Remove ciphertext bound to another login. Readable compact summaries stay."""
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted.strip() or _readable_compact(encrypted):
        return
    item.pop("encrypted_content", None)


def _has_replay_body(item: dict[str, Any]) -> bool:
    """True when the item still carries content the upstream can read without a stored id."""
    if isinstance(item.get("call_id"), str) and item["call_id"].strip():
        return True
    for key in ("content", "summary", "encrypted_content", "arguments", "output", "tools", "text", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def strip_bare_reasoning(payload: dict[str, Any]) -> None:
    """Drop history the store=false Codex upstream cannot replay.

    Bare reasoning ids 404. A stored item id on any replayed item is an invalid
    value, so it is removed. Tool pairing stays on call_id. Ciphertext from
    another account cannot be decrypted, so it is removed. A visible summary,
    message content, or readable compact summary stays. An item that is only a
    stored id is dropped.
    """
    items = payload.get("input")
    if not isinstance(items, list):
        return
    kept: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        _drop_foreign_encrypted_content(item)
        if item.get("type") in {"reasoning", "compaction", "context_compaction"}:
            if not (
                _visible_text(item.get("encrypted_content"))
                or _visible_text(item.get("summary"))
                or _visible_text(item.get("content"))
            ):
                continue
        if not _has_replay_body(item):
            continue
        item.pop("id", None)
        kept.append(item)
    payload["input"] = kept


def apply_responses_lite_contract(payload: dict[str, Any]) -> None:
    """Make a Codex body acceptable to the Responses Lite endpoint.

    Safe to call twice. Does not change reasoning effort.
    """
    if not responses_lite_enabled(str(payload.get("model") or "")):
        return
    payload.pop("max_output_tokens", None)
    payload["parallel_tool_calls"] = False
    reasoning = payload.get("reasoning")
    reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
    reasoning["context"] = "all_turns"
    payload["reasoning"] = reasoning

    items = list(payload.get("input")) if isinstance(payload.get("input"), list) else []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        if not any(_developer_text(item) == instructions for item in items):
            items = [_message_item("developer", instructions), *items]
        payload["instructions"] = ""
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        merged = False
        rewritten: list[Any] = []
        for item in items:
            if not merged and isinstance(item, dict) and item.get("type") == "additional_tools":
                existing = item.get("tools") if isinstance(item.get("tools"), list) else []
                seen = {_tool_key(tool) for tool in existing}
                extra = [tool for tool in tools if _tool_key(tool) not in seen]
                copied = dict(item)
                copied["tools"] = [*existing, *extra]
                rewritten.append(copied)
                merged = True
            else:
                rewritten.append(item)
        if not merged:
            rewritten = [
                {"type": "additional_tools", "role": "developer", "tools": list(tools)},
                *rewritten,
            ]
        items = rewritten
        payload.pop("tools", None)
    payload["input"] = items


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _message_item(role: str, text: str, *, output: bool = False) -> dict[str, Any]:
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "output_text" if output else "input_text", "text": text}],
    }


def _image_url_from_block(item: dict[str, Any]) -> str | None:
    image = item.get("image_url")
    if isinstance(image, dict):
        image = image.get("url")
    if not isinstance(image, str):
        image = item.get("url")
    if isinstance(image, str) and image:
        return image
    return None


def _thought_signature_from_mapping(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("thought_signature", "thoughtSignature"):
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip()
    extra = value.get("extra_content")
    if isinstance(extra, dict):
        google = extra.get("google") if isinstance(extra.get("google"), dict) else extra
        for key in ("thought_signature", "thoughtSignature"):
            found = google.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
    function = value.get("function")
    if isinstance(function, dict):
        for key in ("thought_signature", "thoughtSignature"):
            found = function.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
    return None


def _chat_extra_content(signature: str | None) -> dict[str, Any] | None:
    if not isinstance(signature, str) or not signature.strip():
        return None
    return {"google": {"thought_signature": signature.strip()}}


def _message_content(content: Any, *, output: bool = False) -> list[dict[str, Any]]:
    text_type = "output_text" if output else "input_text"
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        return [{"type": text_type, "text": _content_to_text(content)}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": text_type, "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"text", "input_text", "output_text"}:
            text = item.get("text") or item.get("content")
            if isinstance(text, str):
                blocks.append({"type": text_type, "text": text})
        elif item_type in {"image_url", "input_image"} and not output:
            image = _image_url_from_block(item)
            if image:
                blocks.append({"type": "input_image", "image_url": image})
    return blocks or [{"type": text_type, "text": ""}]


def _tool_message_output(content: Any) -> Any:
    if isinstance(content, str) or content is None or not isinstance(content, list):
        return _content_to_text(content)
    blocks: list[dict[str, Any]] = []
    has_image = False
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append({"type": "input_text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"image_url", "input_image", "output_image"}:
            image = _image_url_from_block(item)
            if image:
                blocks.append({"type": "input_image", "image_url": image})
                has_image = True
            continue
        text = item.get("text") or item.get("content")
        if isinstance(text, str) and text:
            blocks.append({"type": "input_text", "text": text})
    if has_image:
        return blocks
    return _content_to_text(content)


def _function_call_item_from_chat(tool_call: dict[str, Any], *, index: int) -> dict[str, Any]:
    function = tool_call.get("function")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str):
        raise HTTPException(status_code=422, detail="assistant tool call must include a function name")
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
    call_id = tool_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = f"call_{index}"
    item: dict[str, Any] = {
        "type": "function_call",
        "call_id": call_id,
        "name": function["name"],
        "arguments": arguments,
    }
    signature = _thought_signature_from_mapping(tool_call)
    if signature:
        item["thought_signature"] = signature
        item["thoughtSignature"] = signature
    return item


def chat_messages_to_input(messages: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=422, detail="messages must be a non-empty array")
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=422, detail="each message must be an object")
        role = message.get("role")
        text = _content_to_text(message.get("content"))
        if role == "system":
            if text:
                instructions.append(text)
            continue
        if role == "developer":
            items.append({"type": "message", "role": "developer", "content": _message_content(message.get("content"))})
        elif role == "assistant":
            if message.get("content") is not None and message.get("content") != "":
                items.append({"type": "message", "role": "assistant", "content": _message_content(message.get("content"), output=True)})
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                for index, tool_call in enumerate(tool_calls):
                    if not isinstance(tool_call, dict):
                        raise HTTPException(status_code=422, detail="assistant tool_calls must contain objects")
                    items.append(_function_call_item_from_chat(tool_call, index=index))
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": _message_content(message.get("content"))})
        elif role in {"tool", "function"}:
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                name = message.get("name")
                call_id = name if role == "function" and isinstance(name, str) and name else ""
            if not isinstance(call_id, str) or not call_id:
                raise HTTPException(status_code=422, detail="tool message must include tool_call_id")
            item = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": _tool_message_output(message.get("content")),
            }
            name = message.get("name")
            if isinstance(name, str) and name.strip():
                item["name"] = name.strip()
            items.append(item)
        else:
            raise HTTPException(status_code=422, detail=f"unsupported message role: {role}")
    if not items:
        raise HTTPException(status_code=422, detail="at least one non-system message is required")
    return "\n\n".join(instructions), items


def normalize_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for tool in value:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=422, detail="each tool must be an object")
        item = dict(tool)
        if item.get("type") == "function":
            function = item.pop("function", None)
            if isinstance(function, dict):
                item["name"] = function.get("name")
                for field in ("description", "parameters", "strict"):
                    if field in function:
                        item[field] = function[field]
            if not isinstance(item.get("name"), str) or not item["name"]:
                raise HTTPException(status_code=422, detail="function tool must include a name")
        normalized.append(item)
    return normalized


def normalize_tool_choice(value: Any) -> Any:
    if not isinstance(value, dict) or value.get("type") != "function":
        return value
    choice = dict(value)
    function = choice.pop("function", None)
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        choice["name"] = function["name"]
    return choice


def normalize_responses_input(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                items.append(_message_item("user", item))
            elif isinstance(item, dict):
                normalized = dict(item)
                if normalized.get("type") == "additional_tools":
                    normalized["tools"] = normalize_tools(normalized.get("tools"))
                items.append(normalized)
            else:
                raise HTTPException(status_code=422, detail="Input must be a list of objects")
        if not items:
            raise HTTPException(status_code=422, detail="Input must be a list")
        return items
    if isinstance(value, str) and value:
        return [_message_item("user", value)]
    raise HTTPException(status_code=422, detail="Input must be a list")


def to_responses_payload(body: dict[str, Any], *, default_model: str) -> tuple[dict[str, Any], bool]:
    model = str(body.get("model") or default_model)
    lite = responses_lite_enabled(model)
    if isinstance(body.get("messages"), list):
        instructions, items = chat_messages_to_input(body["messages"])
    elif "input" in body:
        instructions = str(body.get("instructions") or "")
        items = normalize_responses_input(body.get("input"))
    else:
        raise HTTPException(status_code=422, detail="request must include messages or input")

    tools = normalize_tools(body.get("tools"))
    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": instructions,
        "input": items,
        "tool_choice": normalize_tool_choice(body.get("tool_choice")) or "auto",
        "parallel_tool_calls": bool(body.get("parallel_tool_calls", False)),
        "include": body.get("include") or ["reasoning.encrypted_content"],
    }
    # Preserve client negotiation and cache hints used by newer Codex clients.
    # These fields are safe metadata and do not contain prompts or credentials.
    for field in ("client_metadata", "prompt_cache_key", "metadata", "truncation"):
        if field in body and body[field] is not None:
            payload[field] = body[field]
    if tools:
        payload["tools"] = tools

    reasoning_effort = body.get("reasoning_effort") or body.get("reasoning")
    reasoning: dict[str, Any] = {}
    if isinstance(reasoning_effort, str) and reasoning_effort:
        reasoning["effort"] = reasoning_effort
    elif isinstance(reasoning_effort, dict):
        reasoning.update(reasoning_effort)
    if reasoning:
        payload["reasoning"] = reasoning
    max_output_tokens = body.get("max_output_tokens")
    if max_output_tokens is None:
        max_output_tokens = body.get("max_completion_tokens")
    if max_output_tokens is None:
        max_output_tokens = body.get("max_tokens")
    # The internal Responses Lite endpoint rejects max_output_tokens outright.
    # Keep accepting all public Chat/Responses aliases so callers remain
    # compatible, but omit the unsupported field for Codex subscription models.
    if isinstance(max_output_tokens, int) and max_output_tokens > 0:
        payload["max_output_tokens"] = max_output_tokens
    response_format = body.get("response_format")
    if isinstance(response_format, dict) and isinstance(response_format.get("type"), str):
        format_type = response_format["type"]
        if format_type == "json_schema" and isinstance(response_format.get("json_schema"), dict):
            schema = response_format["json_schema"]
            text_format = {"type": "json_schema"}
            for field in ("name", "description", "schema", "strict"):
                if field in schema:
                    text_format[field] = schema[field]
            payload["text"] = {"format": text_format}
        elif format_type in {"json_object", "text"}:
            payload["text"] = {"format": {"type": format_type}}
    service_tier = body.get("service_tier") or settings.codex_service_tier
    if service_tier:
        payload["service_tier"] = service_tier
    if lite:
        apply_responses_lite_contract(payload)
    return payload, lite


def ensure_response_created_at(response: dict[str, Any], *, created_at: int) -> dict[str, Any]:
    value = response.get("created_at")
    if isinstance(value, bool) or value is None:
        response["created_at"] = created_at
    elif isinstance(value, float):
        response["created_at"] = int(value)
    elif isinstance(value, str) and value.isdigit():
        response["created_at"] = int(value)
    elif not isinstance(value, int):
        response["created_at"] = created_at
    if not response.get("object"):
        response["object"] = "response"
    return response


def stamp_sse_created_at(piece: str, *, created_at: int) -> str:
    event, data = parse_sse_event(piece)
    if not data or data == "[DONE]":
        return piece
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return piece
    if not isinstance(payload, dict):
        return piece
    nested = payload.get("response")
    target = nested if isinstance(nested, dict) else payload if payload.get("object") == "response" else None
    if not isinstance(target, dict):
        return piece
    before = target.get("created_at")
    ensure_response_created_at(target, created_at=created_at)
    if target.get("created_at") == before and (before is not None):
        return piece
    encoded = json.dumps(payload, ensure_ascii=False)
    if event and event != "message":
        return f"event: {event}\ndata: {encoded}"
    if piece.lstrip().startswith("event:"):
        return f"event: {event}\ndata: {encoded}"
    return f"data: {encoded}"


def parse_sse_event(raw: str) -> tuple[str, str]:
    event_name = "message"
    data_lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return event_name, "\n".join(data_lines)


def iter_sse_blocks(buffer: str) -> tuple[list[str], str]:
    buffer = buffer.replace("\r\n", "\n")
    blocks: list[str] = []
    while "\n\n" in buffer:
        raw, buffer = buffer.split("\n\n", 1)
        if raw.strip():
            blocks.append(raw)
    return blocks, buffer


def encode_sse_json(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _chat_chunk(
    *,
    response_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _chat_usage(usage: dict[str, Any]) -> dict[str, Any]:
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    result: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(usage.get("total_tokens") or prompt + completion),
    }
    input_details = usage.get("input_tokens_details")
    if isinstance(input_details, dict):
        result["prompt_tokens_details"] = input_details
    output_details = usage.get("output_tokens_details")
    if isinstance(output_details, dict):
        result["completion_tokens_details"] = output_details
    return result


def _tool_state(state: dict[str, Any], item: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    tools = state.setdefault("tool_calls", [])
    item_id = str(item.get("id") or item.get("call_id") or f"call_{len(tools)}")
    signature = _thought_signature_from_mapping(item)
    for index, existing in enumerate(tools):
        if existing["item_id"] == item_id or (
            item.get("call_id") and existing["call_id"] == item.get("call_id")
        ):
            if signature:
                existing["thought_signature"] = signature
            return index, existing
    entry = {
        "item_id": item_id,
        "call_id": str(item.get("call_id") or item_id),
        "name": str(item.get("name") or ""),
        "arguments": str(item.get("arguments") or ""),
        "thought_signature": signature or "",
    }
    tools.append(entry)
    return len(tools) - 1, entry


def _chat_tool_call_payload(entry: dict[str, Any], *, index: int, arguments: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "index": index,
        "id": entry["call_id"],
        "type": "function",
        "function": {
            "name": entry["name"],
            "arguments": entry["arguments"] if arguments is None else arguments,
        },
    }
    extra = _chat_extra_content(str(entry.get("thought_signature") or "") or None)
    if extra:
        payload["extra_content"] = extra
    return payload


def convert_sse_to_chat_chunks(event_name: str, data: str, state: dict[str, Any]) -> list[bytes]:
    if not data or data == "[DONE]":
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []

    event_type = str(payload.get("type") or event_name)
    created = int(state.setdefault("created", int(time.time())))
    model = str(state.get("model") or settings.codex_default_model)
    response_id = str(state.get("id") or "codex-response")

    if event_type == "response.created":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        state["id"] = str(response.get("id") or response_id)
        state["model"] = str(response.get("model") or model)
        state["text"] = ""
        return [
            encode_sse_json(
                _chat_chunk(
                    response_id=str(state["id"]),
                    model=str(state["model"]),
                    created=created,
                    delta={"role": "assistant"},
                    finish_reason=None,
                )
            )
        ]

    if event_type == "response.output_text.delta":
        delta_text = payload.get("delta")
        if not isinstance(delta_text, str) or not delta_text:
            return []
        state["text"] = str(state.get("text") or "") + delta_text
        return [
            encode_sse_json(
                _chat_chunk(
                    response_id=str(state.get("id") or response_id),
                    model=str(state.get("model") or model),
                    created=created,
                    delta={"content": delta_text},
                    finish_reason=None,
                )
            )
        ]

    if event_type == "response.refusal.delta":
        refusal = payload.get("delta")
        if not isinstance(refusal, str) or not refusal:
            return []
        return [encode_sse_json(_chat_chunk(
            response_id=str(state.get("id") or response_id),
            model=str(state.get("model") or model),
            created=created,
            delta={"refusal": refusal},
            finish_reason=None,
        ))]

    if event_type == "response.output_item.added":
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return []
        index, entry = _tool_state(state, item)
        return [encode_sse_json(_chat_chunk(
            response_id=str(state.get("id") or response_id),
            model=str(state.get("model") or model),
            created=created,
            delta={"tool_calls": [_chat_tool_call_payload(entry, index=index, arguments="")]},
            finish_reason=None,
        ))]

    if event_type == "response.function_call_arguments.delta":
        item = {
            "id": payload.get("item_id"),
            "call_id": payload.get("call_id"),
            "name": payload.get("name"),
        }
        index, entry = _tool_state(state, item)
        delta = payload.get("delta")
        if not isinstance(delta, str):
            return []
        entry["arguments"] += delta
        return [encode_sse_json(_chat_chunk(
            response_id=str(state.get("id") or response_id),
            model=str(state.get("model") or model),
            created=created,
            delta={"tool_calls": [{
                "index": index,
                "function": {"arguments": delta},
            }]},
            finish_reason=None,
        ))]

    if event_type == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            _index, entry = _tool_state(state, item)
            if isinstance(item.get("arguments"), str):
                entry["arguments"] = item["arguments"]
            if isinstance(item.get("name"), str):
                entry["name"] = item["name"]
        return []

    if event_type == "response.completed":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        state["completed"] = response or payload
        usage = response.get("usage") if isinstance(response, dict) else None
        finish_reason = "tool_calls" if state.get("tool_calls") else "stop"
        chunk = _chat_chunk(
            response_id=str(state.get("id") or response.get("id") or response_id),
            model=str(state.get("model") or response.get("model") or model),
            created=created,
            delta={},
            finish_reason=finish_reason,
        )
        if isinstance(usage, dict):
            chunk["usage"] = _chat_usage(usage)
        state["done"] = True
        return [encode_sse_json(chunk), b"data: [DONE]\n\n"]

    if event_type == "response.incomplete":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        state["completed"] = response or payload
        reason = ((response.get("incomplete_details") or {}).get("reason")
                  if isinstance(response.get("incomplete_details"), dict) else None)
        finish_reason = "content_filter" if reason == "content_filter" else "length"
        state["done"] = True
        return [encode_sse_json(_chat_chunk(
            response_id=str(state.get("id") or response.get("id") or response_id),
            model=str(state.get("model") or response.get("model") or model),
            created=created,
            delta={},
            finish_reason=finish_reason,
        )), b"data: [DONE]\n\n"]

    if event_type in {"response.failed", "error"}:
        response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        error = response.get("error") if isinstance(response, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        state["error"] = message or "Codex upstream request failed"
        state["done"] = True
        return [
            encode_sse_json({"error": {"message": state["error"], "type": "upstream_error"}}),
            b"data: [DONE]\n\n",
        ]
    return []


def finalize_chat_completion(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("error"):
        raise HTTPException(status_code=502, detail="Codex upstream request failed")
    completed = state.get("completed")
    model = str(state.get("model") or settings.codex_default_model)
    response_id = str(state.get("id") or "codex-response")
    text = str(state.get("text") or "")
    usage = None
    if isinstance(completed, dict):
        response_id = str(completed.get("id") or response_id)
        model = str(completed.get("model") or model)
        usage = completed.get("usage")
        if not text:
            text = _text_from_completed(completed)
        if not state.get("tool_calls") and isinstance(completed.get("output"), list):
            for item in completed["output"]:
                if isinstance(item, dict) and item.get("type") == "function_call":
                    _tool_state(state, item)
    if not text and not completed:
        raise HTTPException(status_code=502, detail="Codex upstream returned no completion")
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    tools = state.get("tool_calls")
    if isinstance(tools, list) and tools:
        message["tool_calls"] = []
        for item in tools:
            call: dict[str, Any] = {
                "id": str(item["call_id"]),
                "type": "function",
                "function": {"name": str(item["name"]), "arguments": str(item["arguments"])},
            }
            extra = _chat_extra_content(str(item.get("thought_signature") or "") or None)
            if extra:
                call["extra_content"] = extra
            message["tool_calls"].append(call)
    payload: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion",
        "created": int(state.get("created") or int(time.time())),
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": "tool_calls" if tools else "stop"}
        ],
    }
    if isinstance(usage, dict):
        payload["usage"] = _chat_usage(usage)
    return payload


def _text_from_completed(completed: dict[str, Any]) -> str:
    if isinstance(completed.get("output_text"), str):
        return completed["output_text"]
    output = completed.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "".join(parts)


def completed_response_from_sse(events: Iterable[tuple[str, str]]) -> dict[str, Any]:
    completed: dict[str, Any] | None = None
    for event_name, data in events:
        if not data:
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or event_name)
        if event_type in {"response.completed", "response.incomplete"} and isinstance(payload.get("response"), dict):
            completed = payload["response"]
        elif event_type in {"response.failed", "error"}:
            raise HTTPException(status_code=502, detail="Codex upstream request failed")
    if completed is None:
        raise HTTPException(status_code=502, detail="Codex upstream returned no completion")
    return completed
