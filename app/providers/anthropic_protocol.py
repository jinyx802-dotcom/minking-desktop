"""Anthropic Messages API <-> Responses translation for Claude Code."""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.codex_gateway import GatewayError


def _text_from_blocks(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {None, "text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text_from_blocks(value)
    return ""


def _image_block(block: dict[str, Any]) -> dict[str, Any] | None:
    source = block.get("source")
    if not isinstance(source, dict):
        url = block.get("url") or block.get("image_url")
        if isinstance(url, str) and url:
            return {"type": "input_image", "image_url": url}
        return None
    kind = source.get("type")
    if kind == "url" and isinstance(source.get("url"), str):
        return {"type": "input_image", "image_url": source["url"]}
    if kind == "base64" and isinstance(source.get("data"), str):
        mime = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
        return {"type": "input_image", "image_url": f"data:{mime};base64,{source['data']}"}
    return None


def _user_content(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": ""}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "input_text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {None, "text"} and isinstance(item.get("text"), str):
            blocks.append({"type": "input_text", "text": item["text"]})
        elif kind == "image":
            image = _image_block(item)
            if image:
                blocks.append(image)
    return blocks or [{"type": "input_text", "text": ""}]


def _tool_result_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _text_from_blocks(content)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _anthropic_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise GatewayError(422, "each tool must be an object", code="invalid_request")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GatewayError(422, "tool must include a name", code="invalid_request")
        parameters = item.get("input_schema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        tool: dict[str, Any] = {"type": "function", "name": name.strip(), "parameters": parameters}
        if isinstance(item.get("description"), str):
            tool["description"] = item["description"]
        tools.append(tool)
    return tools


def _tool_choice(value: Any) -> Any:
    if value in {None, "auto", "any", "required"}:
        return "auto" if value in {None, "any"} else value
    if value == "none":
        return "none"
    if isinstance(value, dict) and value.get("type") == "tool" and isinstance(value.get("name"), str):
        return {"type": "function", "name": value["name"]}
    return "auto"


def anthropic_to_responses(body: dict[str, Any], *, default_model: str) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise GatewayError(422, "request must be a JSON object", code="invalid_request")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GatewayError(422, "messages must be a non-empty array", code="invalid_request")
    model = str(body.get("model") or default_model)
    system_parts: list[str] = []
    top_system = _system_text(body.get("system"))
    if top_system:
        system_parts.append(top_system)
    items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError(422, "each message must be an object", code="invalid_request")
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            text = _system_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            if isinstance(content, list):
                pending_text: list[dict[str, Any]] = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        if pending_text:
                            items.append({"type": "message", "role": "user", "content": pending_text})
                            pending_text = []
                        call_id = block.get("tool_use_id")
                        if not isinstance(call_id, str) or not call_id:
                            raise GatewayError(422, "tool_result must include tool_use_id", code="invalid_request")
                        items.append(
                            {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": _tool_result_output(block.get("content")),
                            }
                        )
                    elif isinstance(block, str):
                        pending_text.append({"type": "input_text", "text": block})
                    elif isinstance(block, dict):
                        pending_text.extend(_user_content([block]))
                if pending_text:
                    items.append({"type": "message", "role": "user", "content": pending_text})
            else:
                items.append({"type": "message", "role": "user", "content": _user_content(content)})
        elif role == "assistant":
            if isinstance(content, str) and content:
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            elif isinstance(content, list):
                text_blocks: list[dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind in {None, "text"} and isinstance(block.get("text"), str):
                        text_blocks.append({"type": "output_text", "text": block["text"]})
                    elif kind == "tool_use":
                        if text_blocks:
                            items.append({"type": "message", "role": "assistant", "content": text_blocks})
                            text_blocks = []
                        call_id = block.get("id")
                        name = block.get("name")
                        if not isinstance(call_id, str) or not call_id:
                            raise GatewayError(422, "tool_use must include id", code="invalid_request")
                        if not isinstance(name, str) or not name:
                            raise GatewayError(422, "tool_use must include name", code="invalid_request")
                        arguments = block.get("input")
                        if not isinstance(arguments, str):
                            arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": name,
                                "arguments": arguments,
                            }
                        )
                if text_blocks:
                    items.append({"type": "message", "role": "assistant", "content": text_blocks})
        else:
            raise GatewayError(422, f"unsupported message role: {role}", code="invalid_request")
    if not items:
        raise GatewayError(422, "at least one non-system message is required", code="invalid_request")
    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": "\n\n".join(system_parts),
        "input": items,
        "tool_choice": _tool_choice(body.get("tool_choice")),
        "parallel_tool_calls": bool(body.get("disable_parallel_tool_use") is False),
    }
    tools = _anthropic_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        payload["max_output_tokens"] = max_tokens
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "enabled":
        payload["reasoning"] = {"effort": "high"}
    return payload


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"input_tokens": 0, "output_tokens": 0}
    input_tokens = int(value.get("input_tokens") or value.get("prompt_tokens") or 0)
    output_tokens = int(value.get("output_tokens") or value.get("completion_tokens") or 0)
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


def encode_anthropic_sse(payload: dict[str, Any]) -> bytes:
    event = str(payload.get("type") or "message")
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _close_open_block(state: dict[str, Any]) -> list[bytes]:
    index = state.get("open_index")
    if not isinstance(index, int):
        return []
    state["open_index"] = None
    return [encode_anthropic_sse({"type": "content_block_stop", "index": index})]


def _open_text(state: dict[str, Any]) -> list[bytes]:
    if state.get("open_kind") == "text":
        return []
    chunks = _close_open_block(state)
    index = int(state.get("next_index") or 0)
    state["next_index"] = index + 1
    state["open_index"] = index
    state["open_kind"] = "text"
    chunks.append(
        encode_anthropic_sse(
            {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}}
        )
    )
    return chunks


def _open_tool(state: dict[str, Any], *, call_id: str, name: str) -> list[bytes]:
    chunks = _close_open_block(state)
    index = int(state.get("next_index") or 0)
    state["next_index"] = index + 1
    state["open_index"] = index
    state["open_kind"] = "tool"
    state.setdefault("tools", []).append({"id": call_id, "name": name, "index": index, "arguments": ""})
    chunks.append(
        encode_anthropic_sse(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
            }
        )
    )
    return chunks


def convert_sse_to_anthropic(event_name: str, data: str, state: dict[str, Any]) -> list[bytes]:
    if not data or data == "[DONE]":
        return []
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    event_type = str(payload.get("type") or event_name)
    if event_type == "response.created":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        message_id = str(response.get("id") or state.get("id") or f"msg_{uuid.uuid4().hex}")
        model = str(response.get("model") or state.get("model") or "")
        state.update(
            {
                "id": message_id,
                "model": model,
                "next_index": 0,
                "open_index": None,
                "open_kind": None,
                "text": "",
                "tools": [],
            }
        )
        return [
            encode_anthropic_sse(
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [],
                        "model": model,
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                }
            )
        ]
    if event_type == "response.output_text.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        state["text"] = str(state.get("text") or "") + delta
        chunks = _open_text(state)
        chunks.append(
            encode_anthropic_sse(
                {
                    "type": "content_block_delta",
                    "index": state["open_index"],
                    "delta": {"type": "text_delta", "text": delta},
                }
            )
        )
        return chunks
    if event_type == "response.output_item.added":
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return []
        call_id = str(item.get("call_id") or item.get("id") or f"toolu_{uuid.uuid4().hex}")
        name = str(item.get("name") or "")
        return _open_tool(state, call_id=call_id, name=name)
    if event_type == "response.function_call_arguments.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        tools = state.setdefault("tools", [])
        if tools:
            tools[-1]["arguments"] = str(tools[-1].get("arguments") or "") + delta
        index = state.get("open_index")
        if not isinstance(index, int):
            return []
        return [
            encode_anthropic_sse(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": delta},
                }
            )
        ]
    if event_type == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            tools = state.setdefault("tools", [])
            call_id = str(item.get("call_id") or "")
            for entry in tools:
                if entry["id"] == call_id or not call_id:
                    if isinstance(item.get("arguments"), str):
                        entry["arguments"] = item["arguments"]
                    if isinstance(item.get("name"), str):
                        entry["name"] = item["name"]
                    break
        return []
    if event_type in {"response.completed", "response.incomplete"}:
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        state["completed"] = response or payload
        chunks = _close_open_block(state)
        tools = state.get("tools") or []
        stop = "tool_use" if tools else "end_turn"
        if event_type == "response.incomplete":
            stop = "max_tokens"
        usage = _usage(response.get("usage") if isinstance(response, dict) else None)
        chunks.append(
            encode_anthropic_sse(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {"output_tokens": usage["output_tokens"]},
                }
            )
        )
        chunks.append(encode_anthropic_sse({"type": "message_stop"}))
        state["done"] = True
        state["stop_reason"] = stop
        state["usage"] = usage
        return chunks
    if event_type in {"response.failed", "error"}:
        response = payload.get("response") if isinstance(payload.get("response"), dict) else payload
        error = response.get("error") if isinstance(response, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        state["error"] = message or "upstream request failed"
        state["done"] = True
        return [
            encode_anthropic_sse(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": state["error"]},
                }
            )
        ]
    return []


def finalize_anthropic_message(state: dict[str, Any]) -> dict[str, Any]:
    if state.get("error"):
        raise GatewayError(502, str(state["error"]), code="upstream_error")
    content: list[dict[str, Any]] = []
    text = str(state.get("text") or "")
    if text:
        content.append({"type": "text", "text": text})
    for tool in state.get("tools") or []:
        raw = tool.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        content.append(
            {
                "type": "tool_use",
                "id": str(tool["id"]),
                "name": str(tool.get("name") or ""),
                "input": parsed,
            }
        )
    usage = state.get("usage") if isinstance(state.get("usage"), dict) else {"input_tokens": 0, "output_tokens": 0}
    completed = state.get("completed") if isinstance(state.get("completed"), dict) else {}
    if isinstance(completed.get("usage"), dict):
        usage = _usage(completed["usage"])
    stop = str(state.get("stop_reason") or ("tool_use" if any(item.get("type") == "tool_use" for item in content) else "end_turn"))
    return {
        "id": str(state.get("id") or completed.get("id") or f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": str(state.get("model") or completed.get("model") or ""),
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage,
    }


def anthropic_error_body(status: int, message: str) -> dict[str, Any]:
    mapping = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "invalid_request_error",
        422: "invalid_request_error",
        429: "rate_limit_error",
    }
    return {"type": "error", "error": {"type": mapping.get(status, "api_error"), "message": message}}
