from __future__ import annotations

import datetime as dt
import codecs
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.config import settings
from app.http_client import new_client
from app.providers.codex_protocol import iter_sse_blocks, parse_sse_event
from app.providers.base import (
    CAP_CHAT,
    CAP_IMAGE,
    CAP_IMAGE_EDIT,
    CAP_RESPONSES,
    CAP_STREAM,
    ImportedAccount,
    ModelInfo,
)

_BASE = {"cn": "https://copilot.tencent.com", "global": "https://www.workbuddy.ai"}
_ORIGIN = {"cn": "https://www.codebuddy.cn", "global": "https://www.workbuddy.ai"}
_CAPS = (CAP_CHAT, CAP_STREAM, CAP_RESPONSES, CAP_IMAGE, CAP_IMAGE_EDIT)
_REFRESH_WINDOW = dt.timedelta(minutes=5)
_MODELS = (
    "glm-5.2", "glm-5.1", "glm-5v-turbo", "kimi-k2.7",
    "minimax-m3-pay", "hy3", "hy3-preview", "hy3-preview-agent",
    "deepseek-v4-pro", "deepseek-v4-flash",
)


@dataclass(frozen=True, slots=True)
class WorkBuddyCredentials:
    access_token: str
    refresh_token: str | None
    expires_at: dt.datetime | None
    uid: str
    enterprise_id: str | None
    domain: str | None
    realm: str
    path: Path
    raw: dict[str, Any]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _expires_at(value: Any) -> dt.datetime | None:
    try:
        if isinstance(value, str) and not value.isdigit():
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed.astimezone(dt.UTC)
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def realm_for(domain: str | None, explicit: str | None = None) -> str:
    if explicit in _BASE:
        return explicit
    return "global" if _text(domain).lower().endswith("workbuddy.ai") else "cn"


def base_url(realm: str) -> str:
    return _BASE[realm]


def local_auth_path() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        root = str(Path.home() / "AppData" / "Local")
    return Path(root) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"


def load_credentials(payload: Any, *, path: Path) -> WorkBuddyCredentials:
    if not isinstance(payload, dict):
        raise ValueError("WorkBuddy credential must be a JSON object")
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else payload
    account = payload.get("account") if isinstance(payload.get("account"), dict) else payload
    access = _text(auth.get("accessToken") or auth.get("access_token"))
    uid = _text(account.get("uid") or account.get("user_id"))
    if not access or not uid:
        raise ValueError("WorkBuddy credential needs auth.accessToken and account.uid")
    domain = _text(auth.get("domain")) or None
    return WorkBuddyCredentials(
        access_token=access,
        refresh_token=_text(auth.get("refreshToken") or auth.get("refresh_token")) or None,
        expires_at=_expires_at(auth.get("expiresAt") or auth.get("expires_at")),
        uid=uid,
        enterprise_id=_text(account.get("enterpriseId") or account.get("enterprise_id")) or None,
        domain=domain,
        realm=realm_for(domain, _text(auth.get("realm") or payload.get("realm"))),
        path=path,
        raw=payload,
    )


def should_refresh(credentials: WorkBuddyCredentials, *, now: dt.datetime | None = None) -> bool:
    return bool(
        credentials.refresh_token
        and credentials.expires_at
        and credentials.expires_at - (now or dt.datetime.now(dt.UTC)) <= _REFRESH_WINDOW
    )


def common_headers(realm: str) -> dict[str, str]:
    origin = _ORIGIN[realm]
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CodeBuddy-Request": "1",
        "Origin": origin,
        "Referer": origin + "/",
        "Accept-Language": "en-US" if realm == "global" else "zh-CN",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    }


def chat_headers(credentials: WorkBuddyCredentials) -> dict[str, str]:
    headers = common_headers(credentials.realm)
    headers.update({
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {credentials.access_token}",
        "X-User-Id": credentials.uid,
    })
    if credentials.enterprise_id:
        headers["X-Enterprise-Id"] = credentials.enterprise_id
    else:
        headers["X-No-Enterprise-Id"] = "1"
    if credentials.realm == "global":
        headers["X-Domain"] = "www.workbuddy.ai"
    elif credentials.domain:
        headers["X-Domain"] = credentials.domain
    else:
        headers["X-No-Department-Info"] = "1"
    headers.update({
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "2.63.2",
        "X-Product": "WorkBuddy",
    })
    return headers


_HOSTED_TOOL_TYPES = frozenset({
    "file_search",
    "computer_use",
    "computer_use_preview",
    "mcp",
})
_HOSTED_FUNCTION_TOOLS = {
    "image_generation": {
        "name": "ImageGen",
        "description": "Generate an image from a text description. Supports text-to-image and image-to-image.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Image description"},
                "aspect_ratio": {"type": "string", "description": "Optional aspect ratio such as 1:1 or 16:9"},
            },
            "required": ["prompt"],
        },
    },
    "web_search": {
        "name": "WebSearch",
        "description": "Search the web for current information and return relevant snippets.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
}
_HOSTED_FUNCTION_TOOLS["web_search_preview"] = _HOSTED_FUNCTION_TOOLS["web_search"]
_HOSTED_CHOICE_NAMES = {
    "image_generation": "ImageGen",
    "web_search": "WebSearch",
    "web_search_preview": "WebSearch",
}
_HOSTED_CALL_TYPES = {
    "ImageGen": "image_generation_call",
    "image_generation": "image_generation_call",
    "WebSearch": "web_search_call",
    "web_search": "web_search_call",
}
_SKIP_INPUT_TYPES = frozenset({
    "reasoning",
    "additional_tools",
    "item_reference",
    "tool_search_output",
    "web_search_call",
    "file_search_call",
    "image_generation_call",
    "mcp_list_tools",
    "mcp_call",
    "mcp_approval_request",
    "mcp_approval_response",
})


def _function_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    tool_type = _text(tool.get("type")) or "function"
    mapped = _HOSTED_FUNCTION_TOOLS.get(tool_type)
    if mapped:
        return {"type": "function", "function": dict(mapped)}
    if tool_type in _HOSTED_TOOL_TYPES:
        return None
    nested = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    name = _text(tool.get("name")) or _text(nested.get("name"))
    if not name:
        return None
    description = _text(tool.get("description")) or _text(nested.get("description"))
    parameters = tool.get("parameters") if isinstance(tool.get("parameters"), dict) else None
    if parameters is None and isinstance(nested.get("parameters"), dict):
        parameters = nested["parameters"]
    function: dict[str, Any] = {"name": name, "parameters": parameters or {"type": "object", "properties": {}}}
    if description:
        function["description"] = description
    return {"type": "function", "function": function}


def _function_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    mapped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        item = _function_tool(tool)
        if item is None:
            continue
        name = item["function"]["name"]
        if name in seen:
            continue
        seen.add(name)
        mapped.append(item)
    return mapped


def _sanitize_tool_choice(choice: Any, tools: list[dict[str, Any]]) -> Any:
    if choice is None:
        return None
    names = {item["function"]["name"] for item in tools}
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        if choice == "required" and not names:
            return None
        return choice
    if isinstance(choice, dict):
        nested = choice.get("function") if isinstance(choice.get("function"), dict) else {}
        name = (
            _HOSTED_CHOICE_NAMES.get(_text(choice.get("type")))
            or _text(choice.get("name"))
            or _text(nested.get("name"))
        )
        if name and name in names:
            return {"type": "function", "function": {"name": name}}
    return None


def _workbuddy_role(role: str) -> str:
    # WorkBuddy rejects the Responses "developer" role as an unapproved channel.
    return "system" if role == "developer" else role


def prepare_chat_payload(payload: dict[str, Any], *, realm: str) -> dict[str, Any]:
    prepared = dict(payload)
    prepared["stream"] = True
    prepared.setdefault("stream_options", {"include_usage": True})
    messages = prepared.get("messages")
    if isinstance(messages, list):
        rewritten: list[Any] = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "developer":
                copied = dict(message)
                copied["role"] = "system"
                rewritten.append(copied)
            else:
                rewritten.append(message)
        prepared["messages"] = rewritten
    alias = prepared.pop("max_completion_tokens", None)
    if "max_tokens" not in prepared and isinstance(alias, int) and alias > 0:
        prepared["max_tokens"] = alias
    tools = _function_tools(prepared.get("tools"))
    if tools:
        prepared["tools"] = tools
    else:
        prepared.pop("tools", None)
    choice = _sanitize_tool_choice(prepared.get("tool_choice"), tools)
    if choice is None:
        prepared.pop("tool_choice", None)
    else:
        prepared["tool_choice"] = choice
    if realm == "global":
        messages = prepared.get("messages")
        if isinstance(messages, list) and messages and isinstance(messages[0], dict):
            if messages[0].get("role") != "system":
                prepared["messages"] = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    *messages,
                ]
    return prepared


def responses_to_chat(payload: dict[str, Any], *, model: str) -> dict[str, Any]:
    if payload.get("previous_response_id") or payload.get("conversation"):
        raise ValueError("WorkBuddy requires inline conversation history")
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        raw_items: list[Any] = [{"role": "user", "content": raw_input}]
    elif isinstance(raw_input, list):
        raw_items = raw_input
    else:
        raise ValueError("WorkBuddy Responses input must be text or a message list")
    messages: list[dict[str, Any]] = []
    instructions = _text(payload.get("instructions"))
    if instructions:
        messages.append({"role": "system", "content": instructions})
    for item in raw_items:
        if not isinstance(item, dict):
            raise ValueError("WorkBuddy Responses input item must be an object")
        item_type = _text(item.get("type"))
        if item_type in _SKIP_INPUT_TYPES:
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            messages.append({
                "role": "tool",
                "tool_call_id": _text(item.get("call_id")),
                "content": item.get("output") if isinstance(item.get("output"), str) else "",
            })
            continue
        if item_type in {"function_call", "custom_tool_call"}:
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": _text(item.get("call_id")), "type": "function",
                    "function": {
                        "name": _text(item.get("name")),
                        "arguments": _text(item.get("arguments") or item.get("input")) or "{}",
                    },
                }],
            })
            continue
        role = _workbuddy_role(_text(item.get("role")) or "user")
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError("Unsupported WorkBuddy Responses role")
        content = item.get("content")
        if isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = _text(part.get("type"))
                if part_type in {"input_text", "output_text", "text"}:
                    parts.append({"type": "text", "text": _text(part.get("text"))})
                elif part_type == "input_image":
                    image_url = part.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    if isinstance(image_url, str) and image_url and len(image_url) <= 400_000:
                        parts.append({"type": "image_url", "image_url": {"url": image_url}})
            content = parts
        if not isinstance(content, (str, list)):
            raise ValueError("Unsupported WorkBuddy Responses content")
        messages.append({"role": role, "content": content})
    chat: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
    tools = _function_tools(payload.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _sanitize_tool_choice(payload.get("tool_choice"), tools)
    if choice is not None:
        chat["tool_choice"] = choice
    if payload.get("temperature") is not None:
        chat["temperature"] = payload["temperature"]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    return chat


def chat_to_response(completion: dict[str, Any], *, model: str) -> dict[str, Any]:
    choices = completion.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    output: list[dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, str) and content:
        output.append({
            "id": "msg_" + os.urandom(12).hex(), "type": "message", "status": "completed",
            "role": "assistant", "content": [{"type": "output_text", "text": content, "annotations": []}],
        })
    calls = message.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            output.append({
                "id": "fc_" + os.urandom(12).hex(), "type": "function_call", "status": "completed",
                "call_id": _text(call.get("id")), "name": _text(function.get("name")),
                "arguments": _text(function.get("arguments")),
            })
    usage = completion.get("usage") if isinstance(completion.get("usage"), dict) else {}
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "id": "resp_" + os.urandom(12).hex(), "object": "response",
        "created_at": int(dt.datetime.now(dt.UTC).timestamp()), "status": "completed",
        "model": model, "output": output, "parallel_tool_calls": True,
        "usage": {
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
        },
    }


def response_sse(response: dict[str, Any]) -> list[bytes]:
    def event(name: str, body: dict[str, Any]) -> bytes:
        return f"event: {name}\ndata: {json.dumps({'type': name, **body}, ensure_ascii=False)}\n\n".encode()

    created = {**response, "status": "in_progress", "output": []}
    chunks = [event("response.created", {"response": created})]
    for index, item in enumerate(response["output"]):
        chunks.append(event("response.output_item.added", {"output_index": index, "item": item}))
        if item["type"] == "message":
            text = item["content"][0]["text"]
            chunks.append(event("response.output_text.delta", {
                "output_index": index, "content_index": 0, "item_id": item["id"], "delta": text,
            }))
            chunks.append(event("response.output_text.done", {
                "output_index": index, "content_index": 0, "item_id": item["id"], "text": text,
            }))
        elif item["type"] == "function_call":
            chunks.append(event("response.function_call_arguments.delta", {
                "output_index": index, "item_id": item["id"], "delta": item["arguments"],
            }))
        chunks.append(event("response.output_item.done", {"output_index": index, "item": item}))
    chunks.append(event("response.completed", {"response": response}))
    return chunks


async def stream_chat_as_responses(
    chunks: AsyncIterator[bytes],
    *,
    model: str,
    hosted_calls: list[dict[str, Any]] | None = None,
) -> AsyncIterator[bytes]:
    """Translate WorkBuddy chat deltas without waiting for the complete answer."""
    response = chat_to_response({"choices": []}, model=model)
    response["status"] = "in_progress"
    output: list[dict[str, Any]] = response["output"]
    yield _response_event("response.created", {"response": dict(response)})
    decoder = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    text_item: dict[str, Any] | None = None
    tool_items: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] | None = None

    async for chunk in chunks:
        buffer += decoder.decode(chunk)
        blocks, buffer = iter_sse_blocks(buffer)
        for block in blocks:
            _name, data = parse_sse_event(block)
            if not data or data == "[DONE]":
                continue
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("usage"), dict):
                usage = payload["usage"]
            choices = payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                continue
            delta = choices[0].get("delta")
            if not isinstance(delta, dict):
                delta = choices[0].get("message")
            if not isinstance(delta, dict):
                continue
            piece = delta.get("content")
            if isinstance(piece, str) and piece:
                if text_item is None:
                    text_item = {
                        "id": "msg_" + os.urandom(12).hex(), "type": "message",
                        "status": "in_progress", "role": "assistant",
                        "content": [{"type": "output_text", "text": "", "annotations": []}],
                    }
                    output.append(text_item)
                    index = len(output) - 1
                    yield _response_event("response.output_item.added", {
                        "output_index": index, "item": text_item,
                    })
                    yield _response_event("response.content_part.added", {
                        "output_index": index, "content_index": 0, "item_id": text_item["id"],
                        "part": text_item["content"][0],
                    })
                text_item["content"][0]["text"] += piece
                yield _response_event("response.output_text.delta", {
                    "output_index": output.index(text_item), "content_index": 0,
                    "item_id": text_item["id"], "delta": piece,
                })
            calls = delta.get("tool_calls")
            if isinstance(calls, list):
                for position, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    call_index = call.get("index") if isinstance(call.get("index"), int) else position
                    item = tool_items.get(call_index)
                    if item is None:
                        item = {
                            "id": "fc_" + os.urandom(12).hex(), "type": "function_call",
                            "status": "in_progress", "call_id": "", "name": "", "arguments": "",
                        }
                        tool_items[call_index] = item
                        output.append(item)
                        yield _response_event("response.output_item.added", {
                            "output_index": len(output) - 1, "item": item,
                        })
                    if isinstance(call.get("id"), str):
                        item["call_id"] = call["id"]
                    function = call.get("function")
                    if isinstance(function, dict):
                        if isinstance(function.get("name"), str):
                            item["name"] += function["name"]
                            hosted = _HOSTED_CALL_TYPES.get(item["name"])
                            if hosted:
                                item["type"] = hosted
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            item["arguments"] += arguments
                            if item["type"] == "function_call":
                                yield _response_event("response.function_call_arguments.delta", {
                                    "output_index": output.index(item), "item_id": item["id"],
                                    "delta": arguments,
                                })

    for index, item in enumerate(output):
        item["status"] = "completed"
        if item["type"] == "message":
            part = item["content"][0]
            yield _response_event("response.output_text.done", {
                "output_index": index, "content_index": 0,
                "item_id": item["id"], "text": part["text"],
            })
            yield _response_event("response.content_part.done", {
                "output_index": index, "content_index": 0,
                "item_id": item["id"], "part": part,
            })
        elif item["type"] == "function_call":
            yield _response_event("response.function_call_arguments.done", {
                "output_index": index, "item_id": item["id"],
                "arguments": item["arguments"],
            })
        elif item["type"] == "web_search_call":
            query = _tool_arg(item.get("arguments"), "query") or _text(item.get("arguments"))
            item["action"] = {"type": "search", "query": query}
            yield _response_event("response.web_search_call.completed", {
                "output_index": index, "item_id": item["id"], "action": item["action"],
            })
        yield _response_event("response.output_item.done", {"output_index": index, "item": item})
    if usage:
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        response["usage"] = {
            "input_tokens": prompt, "output_tokens": completion,
            "total_tokens": int(usage.get("total_tokens") or prompt + completion),
        }
    response["status"] = "completed"
    hosted = [item for item in output if item.get("type") in {"image_generation_call", "web_search_call"}]
    if hosted_calls is not None:
        hosted_calls.extend(hosted)
    if hosted and hosted_calls is not None:
        return
    yield _response_event("response.completed", {"response": response})


def _tool_arg(raw: Any, key: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(payload, dict):
        return _text(payload.get(key))
    return ""


def _response_event(name: str, body: dict[str, Any]) -> bytes:
    data = json.dumps({"type": name, **body}, ensure_ascii=False)
    return f"event: {name}\ndata: {data}\n\n".encode()


def _data(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(502, "WorkBuddy returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(502, "WorkBuddy returned invalid JSON")
    if response.status_code >= 400:
        raise HTTPException(response.status_code, "WorkBuddy request failed")
    if body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise HTTPException(502, "WorkBuddy request was rejected")
    return body["data"]


def _quota_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _package_remain_used(pkg: dict[str, Any]) -> tuple[float, float, float]:
    cycle_size = _quota_number(pkg.get("CycleCapacitySize"))
    if cycle_size is not None and cycle_size > 0:
        remain = _quota_number(pkg.get("CycleCapacityRemain"))
        used = _quota_number(pkg.get("CycleCapacityUsed"))
        size = cycle_size
    else:
        remain = _quota_number(
            pkg.get("CapacityRemainPrecise")
            if pkg.get("CapacityRemainPrecise") is not None
            else pkg.get("CapacityRemain")
        )
        used = _quota_number(pkg.get("CapacityUsed"))
        size = _quota_number(pkg.get("CapacitySize")) or 0.0
    if size <= 0:
        size = (remain or 0.0) + (used or 0.0)
    if remain is None and used is not None and size > 0:
        remain = max(0.0, size - used)
    if used is None and remain is not None and size > 0:
        used = max(0.0, size - remain)
    return remain or 0.0, used or 0.0, size


def _package_end(pkg: dict[str, Any]) -> dt.datetime | None:
    raw = pkg.get("CycleEndTime") or pkg.get("PackageEndTime") or pkg.get("DeductionEndTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def parse_quota_snapshot(payload: Any) -> dict[str, Any] | None:
    """Map WorkBuddy billing packages into the admin quota progress bar."""
    if not isinstance(payload, dict):
        return None
    data: Any = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if payload.get("code") not in (0, None, "0") and "Accounts" not in data and "Response" not in data:
        return None
    if isinstance(data.get("Response"), dict):
        data = data["Response"]
    if isinstance(data.get("Data"), dict):
        data = data["Data"]
    accounts = data.get("Accounts")
    if not isinstance(accounts, list):
        return None
    remain = used = size = 0.0
    resets_at: int | None = None
    for pkg in accounts:
        if not isinstance(pkg, dict):
            continue
        pkg_remain, pkg_used, pkg_size = _package_remain_used(pkg)
        remain += pkg_remain
        used += pkg_used
        size += pkg_size
        end = _package_end(pkg)
        if end is not None:
            stamp = int(end.timestamp())
            if resets_at is None or stamp < resets_at:
                resets_at = stamp
    if size <= 0:
        size = remain + used
    used_percent = 0.0 if size <= 0 else max(0.0, min(100.0, used / size * 100.0))
    remaining_percent = 0.0 if size <= 0 else max(0.0, min(100.0, remain / size * 100.0))
    return {
        "plan_type": "credits",
        "limits": [
            {
                "limit_id": "workbuddy",
                "limit_name": None,
                "primary": {
                    "used_percent": used_percent,
                    "remaining_percent": remaining_percent,
                    "remaining_amount": remain,
                    "total_amount": size,
                    "window_minutes": None,
                    "window_label": "积分",
                    "resets_at": resets_at,
                },
                "secondary": None,
            }
        ],
        "next_reset_at": resets_at,
        "reset_credits": {"available_count": 0, "credits": []},
        "quota_kind": "credits",
        "message": None,
    }


async def start_authorization(*, realm: str = "cn") -> tuple[str, str, httpx.AsyncClient]:
    if realm not in _BASE:
        raise ValueError("Unknown WorkBuddy realm")
    client = new_client(timeout=20.0, follow_redirects=False)
    try:
        response = await client.post(
            base_url(realm) + "/v2/plugin/auth/state?platform=CLI",
            headers=common_headers(realm),
            json={},
        )
        data = _data(response)
        state = _text(data.get("state"))
        url = _text(data.get("authUrl"))
        if not state or not url.startswith("https://"):
            raise HTTPException(502, "WorkBuddy returned no authorization URL")
        return state, url, client
    except BaseException:
        await client.aclose()
        raise


async def poll_authorization(
    state: str, client: httpx.AsyncClient, *, realm: str = "cn"
) -> dict[str, Any] | None:
    response = await client.get(
        base_url(realm) + "/v2/plugin/auth/token?state=" + quote(state, safe=""),
        headers=common_headers(realm),
        timeout=20.0,
    )
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(502, "WorkBuddy login returned invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(502, "WorkBuddy login returned invalid JSON")
    if response.status_code >= 500:
        raise HTTPException(502, "WorkBuddy login status is unavailable")
    if body.get("code") != 0:
        message = _text(body.get("msg") or body.get("message")).lower()
        if response.status_code < 400 and (body.get("code") == 11217 or "login ing" in message):
            return None
        raise HTTPException(422, "WorkBuddy authorization was rejected or expired")
    token = body.get("data")
    if not isinstance(token, dict) or not _text(token.get("accessToken")):
        return None
    headers = common_headers(realm)
    headers["Authorization"] = "Bearer " + token["accessToken"]
    account_response = await client.get(
        base_url(realm) + "/v2/plugin/login/account?state=" + quote(state, safe=""),
        headers=headers,
        timeout=20.0,
    )
    account = _data(account_response)
    expires_in = token.get("expiresIn")
    try:
        expires_at = int(dt.datetime.now(dt.UTC).timestamp()) + int(expires_in)
    except (TypeError, ValueError):
        expires_at = None
    return {
        "account": account,
        "auth": {
            "accessToken": token["accessToken"],
            "refreshToken": token.get("refreshToken"),
            "domain": token.get("domain"),
            "realm": realm,
            "expiresAt": expires_at,
        },
    }


async def refresh_credentials(
    credentials: WorkBuddyCredentials, client: httpx.AsyncClient
) -> WorkBuddyCredentials:
    if not credentials.refresh_token:
        raise HTTPException(401, "WorkBuddy login expired")
    headers = common_headers(credentials.realm)
    headers["X-Refresh-Token"] = credentials.refresh_token
    headers["X-Auth-Refresh-Source"] = "plugin"
    if credentials.enterprise_id:
        headers["X-Enterprise-Id"] = credentials.enterprise_id
    response = await client.post(
        base_url(credentials.realm) + "/v2/plugin/auth/token/refresh",
        headers=headers,
        timeout=20.0,
    )
    data = _data(response)
    token = _text(data.get("accessToken"))
    if not token:
        raise HTTPException(401, "WorkBuddy login expired")
    raw = dict(credentials.raw)
    auth = dict(raw.get("auth")) if isinstance(raw.get("auth"), dict) else dict(raw)
    auth["accessToken"] = token
    if _text(data.get("refreshToken")):
        auth["refreshToken"] = data["refreshToken"]
    if _text(data.get("domain")):
        auth["domain"] = data["domain"]
    try:
        auth["expiresAt"] = int(dt.datetime.now(dt.UTC).timestamp()) + int(data.get("expiresIn"))
    except (TypeError, ValueError):
        auth.pop("expiresAt", None)
    if isinstance(raw.get("auth"), dict):
        raw["auth"] = auth
    else:
        raw.update(auth)
    return load_credentials(raw, path=credentials.path)


class WorkBuddyAdapter:
    id = "workbuddy"
    display_name = "WorkBuddy"
    capabilities = set(_CAPS)
    ready = True

    def parse_import(self, payload: Any, filename: str) -> list[ImportedAccount]:
        credentials = load_credentials(payload, path=Path(filename))
        account = payload.get("account") if isinstance(payload.get("account"), dict) else payload
        label = _text(account.get("nickname")) or credentials.uid
        expires = credentials.expires_at
        return [ImportedAccount(
            account_id=f"workbuddy:{credentials.realm}:{credentials.uid}",
            payload=credentials.raw,
            expires_at=expires.isoformat().replace("+00:00", "Z") if expires else None,
            label=label,
            capabilities=_CAPS,
        )]

    def catalog(self) -> list[ModelInfo]:
        models = [ModelInfo(
            id="workbuddy/" + name,
            provider=self.id,
            type="text",
            capabilities=_CAPS,
            default=name == "glm-5.2",
            owned_by="workbuddy",
        ) for name in _MODELS]
        models.extend(
            ModelInfo(
                id="workbuddy/" + name,
                provider=self.id,
                type="image",
                capabilities=(CAP_IMAGE, CAP_IMAGE_EDIT),
                owned_by="workbuddy",
            )
            for name in (settings.workbuddy_image_model,)
        )
        from app.providers.catalog_extra import extras_for

        seen = {item.id for item in models}
        for extra in extras_for(self.id):
            if extra.model_id in seen:
                continue
            seen.add(extra.model_id)
            models.append(
                ModelInfo(
                    id=extra.model_id,
                    provider=self.id,
                    type=extra.model_type,
                    capabilities=(CAP_IMAGE, CAP_IMAGE_EDIT) if extra.model_type == "image" else _CAPS,
                    owned_by="workbuddy",
                )
            )
        return models
