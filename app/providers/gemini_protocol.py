"""Gemini generateContent <-> Responses translation for Antigravity clients."""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.codex_gateway import GatewayError


def _part_text(part: dict[str, Any]) -> str:
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _system_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = value.get("parts")
        if isinstance(parts, list):
            return "".join(_part_text(item) for item in parts if isinstance(item, dict))
        return _part_text(value)
    return ""


def _inline_image(part: dict[str, Any]) -> dict[str, Any] | None:
    blob = part.get("inlineData") or part.get("inline_data")
    if not isinstance(blob, dict) or not isinstance(blob.get("data"), str):
        return None
    mime = blob.get("mimeType") or blob.get("mime_type") or "image/png"
    return {"type": "input_image", "image_url": f"data:{mime};base64,{blob['data']}"}


def _function_args(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "{}"


def _gemini_tools(value: Any) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return tools
    for item in value:
        if not isinstance(item, dict):
            continue
        declarations = item.get("functionDeclarations") or item.get("function_declarations") or []
        if not isinstance(declarations, list):
            continue
        for decl in declarations:
            if not isinstance(decl, dict) or not decl.get("name"):
                continue
            parameters = decl.get("parameters") if isinstance(decl.get("parameters"), dict) else {"type": "object"}
            tools.append(
                {
                    "type": "function",
                    "name": str(decl["name"]),
                    "description": str(decl.get("description") or ""),
                    "parameters": parameters,
                }
            )
    return tools


def gemini_to_responses(body: dict[str, Any], *, default_model: str) -> dict[str, Any]:
    model = body.get("model") if isinstance(body.get("model"), str) and body.get("model") else default_model
    if not model:
        raise GatewayError(422, "model is required", code="invalid_request")
    items: list[dict[str, Any]] = []
    system_parts = [_system_text(body.get("systemInstruction") or body.get("system_instruction"))]
    contents = body.get("contents")
    if not isinstance(contents, list) or not contents:
        raise GatewayError(422, "contents is required", code="invalid_request")
    for content in contents:
        if not isinstance(content, dict):
            continue
        role = str(content.get("role") or "user").lower()
        parts = content.get("parts")
        if not isinstance(parts, list):
            parts = [{"text": str(content.get("text") or "")}]
        if role == "system":
            system_parts.append(
                "".join(_part_text(part) for part in parts if isinstance(part, dict))
            )
            continue
        if role == "model":
            text_chunks: list[str] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                call = part.get("functionCall") or part.get("function_call")
                if isinstance(call, dict) and call.get("name"):
                    call_id = str(call.get("id") or f"fc_{uuid.uuid4().hex[:12]}")
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": str(call["name"]),
                            "arguments": _function_args(call.get("args") or call.get("arguments")),
                        }
                    )
                    continue
                text_chunks.append(_part_text(part))
            text = "".join(text_chunks)
            if text:
                items.append({"role": "assistant", "content": [{"type": "output_text", "text": text}]})
            continue
        user_blocks: list[dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            response = part.get("functionResponse") or part.get("function_response")
            if isinstance(response, dict):
                payload = response.get("response")
                output = payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False)
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(response.get("id") or f"fc_{uuid.uuid4().hex[:12]}"),
                        "output": output,
                    }
                )
                continue
            image = _inline_image(part)
            if image:
                user_blocks.append(image)
                continue
            text = _part_text(part)
            if text:
                user_blocks.append({"type": "input_text", "text": text})
        if user_blocks:
            items.append({"role": "user", "content": user_blocks})
    if not items:
        raise GatewayError(422, "at least one user or model turn is required", code="invalid_request")
    payload: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": "\n\n".join(part for part in system_parts if part),
        "input": items,
    }
    tools = _gemini_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
    config = body.get("generationConfig") or body.get("generation_config")
    if isinstance(config, dict):
        max_tokens = config.get("maxOutputTokens") or config.get("max_output_tokens")
        if isinstance(max_tokens, int) and max_tokens > 0:
            payload["max_output_tokens"] = max_tokens
    return payload


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0}
    prompt = int(value.get("input_tokens") or value.get("prompt_tokens") or 0)
    output = int(value.get("output_tokens") or value.get("completion_tokens") or 0)
    return {
        "promptTokenCount": prompt,
        "candidatesTokenCount": output,
        "totalTokenCount": prompt + output,
    }


def encode_gemini_sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _candidate(*, text: str = "", function_call: dict[str, Any] | None = None, finish: str | None = None) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"text": text})
    if function_call:
        parts.append({"functionCall": function_call})
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts or [{"text": ""}]}}
    if finish:
        candidate["finishReason"] = finish
    return {"candidates": [candidate]}


def convert_sse_to_gemini(event_name: str, data: str, state: dict[str, Any]) -> list[bytes]:
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
        state.update({"model": str(response.get("model") or ""), "text": "", "tools": []})
        return []
    if event_type == "response.output_text.delta":
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        state["text"] = str(state.get("text") or "") + delta
        return [encode_gemini_sse(_candidate(text=delta))]
    if event_type == "response.output_item.added":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            state.setdefault("tools", []).append(
                {
                    "id": str(item.get("call_id") or item.get("id") or ""),
                    "name": str(item.get("name") or ""),
                    "arguments": str(item.get("arguments") or ""),
                }
            )
        return []
    if event_type == "response.function_call_arguments.delta":
        delta = payload.get("delta")
        tools = state.setdefault("tools", [])
        if isinstance(delta, str) and tools:
            tools[-1]["arguments"] = str(tools[-1].get("arguments") or "") + delta
        return []
    if event_type == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            tools = state.setdefault("tools", [])
            call_id = str(item.get("call_id") or "")
            for entry in tools:
                if entry.get("id") == call_id or not call_id:
                    if isinstance(item.get("arguments"), str):
                        entry["arguments"] = item["arguments"]
                    if isinstance(item.get("name"), str):
                        entry["name"] = item["name"]
                    break
        return []
    if event_type in {"response.completed", "response.incomplete"}:
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        state["completed"] = response or payload
        tools = state.get("tools") or []
        finish = "STOP" if event_type == "response.completed" else "MAX_TOKENS"
        chunks: list[bytes] = []
        for tool in tools:
            raw = tool.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            chunks.append(
                encode_gemini_sse(
                    _candidate(
                        function_call={"name": str(tool.get("name") or ""), "args": args},
                        finish="STOP",
                    )
                )
            )
        if not tools:
            usage = _usage(response.get("usage") if isinstance(response, dict) else None)
            body = _candidate(text="", finish=finish)
            body["usageMetadata"] = usage
            chunks.append(encode_gemini_sse(body))
        state["done"] = True
        return chunks
    return []


def finalize_gemini_response(state: dict[str, Any]) -> dict[str, Any]:
    tools = state.get("tools") or []
    parts: list[dict[str, Any]] = []
    text = str(state.get("text") or "")
    if text:
        parts.append({"text": text})
    for tool in tools:
        raw = tool.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        parts.append({"functionCall": {"name": str(tool.get("name") or ""), "args": args}})
    completed = state.get("completed") if isinstance(state.get("completed"), dict) else {}
    usage = _usage(completed.get("usage") if isinstance(completed, dict) else None)
    finish = "STOP"
    if tools:
        finish = "STOP"
    elif state.get("stop_reason") == "max_tokens":
        finish = "MAX_TOKENS"
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts or [{"text": ""}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": usage,
        "modelVersion": str(state.get("model") or completed.get("model") or ""),
    }
